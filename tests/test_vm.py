"""Tests for QemuVM: QEMU/KVM virtual machine management.

Requires: /dev/kvm accessible, qemu-system-x86_64 installed in sandbox image.
These tests install QEMU via apt-get on first run (~2 min).

Run with: python -m pytest tests/test_vm.py -v
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from nitrobox import Sandbox, SandboxConfig
from nitrobox.vm import QemuVM

TEST_IMAGE = os.environ.get("LITE_SANDBOX_TEST_IMAGE", "ubuntu:22.04")


def _skip_if_no_kvm():
    if not os.path.exists("/dev/kvm"):
        pytest.skip("/dev/kvm not available")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("no read/write access to /dev/kvm")


def _skip_if_root():
    if os.geteuid() == 0:
        pytest.skip("userns test must run as non-root")


def _requires_docker():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("requires Docker")


@pytest.fixture(scope="module")
def vm_sandbox(tmp_path_factory, shared_cache_dir):
    """Sandbox with /dev/kvm and QEMU installed (module-scoped for speed)."""
    _skip_if_root()
    _skip_if_no_kvm()
    _requires_docker()

    tmp = tmp_path_factory.mktemp("vm")
    vm_dir = tmp / "vms"
    vm_dir.mkdir()

    config = SandboxConfig(
        image=TEST_IMAGE,
        devices=["/dev/kvm"],
        volumes=[f"{vm_dir}:/vm:rw"],
        env_base_dir=str(tmp / "envs"),
        rootfs_cache_dir=shared_cache_dir,
    )
    sb = Sandbox(config, name="vm-test")

    # Install QEMU if not available
    out, ec = sb.run("which qemu-system-x86_64 2>/dev/null || echo notfound")
    if "notfound" in out:
        _, ec = sb.run(
            "apt-get update -qq 2>/dev/null && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "--no-install-recommends qemu-system-x86 qemu-utils 2>/dev/null "
            "| tail -1",
            timeout=300,
        )
        if ec != 0:
            sb.delete()
            pytest.skip("failed to install qemu-system-x86")

    out, ec = sb.run("qemu-system-x86_64 --version 2>&1 | head -1")
    if ec != 0:
        sb.delete()
        pytest.skip("qemu-system-x86_64 not available in sandbox")

    # Create test disk
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(vm_dir / "test.qcow2"), "64M"],
        capture_output=True,
    )

    yield sb, str(vm_dir)
    sb.delete()


class TestQemuVM:
    """QEMU/KVM VM management tests."""

    def test_check_available(self):
        """QemuVM.check_available() returns True when /dev/kvm exists."""
        _skip_if_no_kvm()
        assert QemuVM.check_available() is True

    def test_start_stop(self, vm_sandbox):
        """VM starts and stops cleanly."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        assert vm.running
        vm.stop()
        assert not vm.running

    def test_query_status(self, vm_sandbox):
        """QMP query-status returns running state."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            resp = vm.qmp("query-status")
            assert resp["return"]["status"] == "running"
        finally:
            vm.stop()

    def test_savevm_loadvm(self, vm_sandbox):
        """savevm/loadvm round-trip works."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("test_snap")
            info = vm.info_snapshots()
            assert "test_snap" in info

            vm.loadvm("test_snap")
            # VM should still be running after loadvm
            resp = vm.qmp("query-status")
            assert resp["return"]["status"] == "running"
        finally:
            vm.stop()

    def test_delvm(self, vm_sandbox):
        """delvm removes a snapshot."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("to_delete")
            assert "to_delete" in vm.info_snapshots()
            vm.delvm("to_delete")
            assert "to_delete" not in vm.info_snapshots()
        finally:
            vm.stop()

    def test_multiple_snapshots(self, vm_sandbox):
        """Multiple savevm/loadvm cycles work."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            vm.savevm("snap_a")
            vm.savevm("snap_b")
            info = vm.info_snapshots()
            assert "snap_a" in info
            assert "snap_b" in info

            vm.loadvm("snap_a")
            vm.loadvm("snap_b")
            vm.loadvm("snap_a")
            assert vm.running
        finally:
            vm.stop()

    def test_hmp_command(self, vm_sandbox):
        """HMP commands work via QMP human-monitor-command."""
        sb, vm_dir = vm_sandbox
        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1)
        vm.start(timeout=30)
        try:
            info = vm.hmp("info version")
            assert info.strip(), "info version should return non-empty"
        finally:
            vm.stop()

    def test_build_cmd(self, vm_sandbox):
        """_build_cmd generates correct QEMU command line."""
        sb, _ = vm_sandbox
        vm = QemuVM(sb, disk="/vm/disk.qcow2", memory="4G", cpus=4,
                    extra_args=["-vnc", ":0"])
        cmd = vm._build_cmd()
        assert "-enable-kvm" in cmd
        assert "-m 4G" in cmd
        assert "-smp 4" in cmd
        assert "/vm/disk.qcow2" in cmd
        assert "-vnc :0" in cmd

    def test_build_cmd_override(self, vm_sandbox):
        """cmd_override replaces the default QEMU command."""
        sb, _ = vm_sandbox
        override = "qemu-system-x86_64 -enable-kvm -m 8G -drive file=/my/disk.qcow2"
        vm = QemuVM(sb, cmd_override=override)
        cmd = vm._build_cmd()
        # cmd_override used verbatim with -qmp appended
        assert cmd.startswith(override)
        assert "-qmp unix:" in cmd
        # Default args should NOT be present
        assert "-smp" not in cmd
        assert "-display" not in cmd

    def test_build_cmd_override_preserves_qmp_socket(self, vm_sandbox):
        """cmd_override + custom qmp_socket works."""
        sb, _ = vm_sandbox
        override = "qemu-system-x86_64 -m 4G"
        vm = QemuVM(sb, cmd_override=override, qmp_socket="/storage/.qmp.sock")
        cmd = vm._build_cmd()
        assert "-qmp unix:/storage/.qmp.sock,server,nowait" in cmd

    def test_repr(self, vm_sandbox):
        """repr shows useful info."""
        sb, _ = vm_sandbox
        vm = QemuVM(sb, disk="/vm/disk.qcow2", memory="2G", cpus=2)
        r = repr(vm)
        assert "disk=" in r
        assert "stopped" in r


class TestRustQMP:
    """Tests for the Rust QMP client binding."""

    def test_binding_importable(self):
        """py_qmp_send is importable from _core."""
        from nitrobox._core import py_qmp_send
        assert callable(py_qmp_send)

    def test_nonexistent_socket_raises(self):
        """Connecting to a non-existent socket raises OSError."""
        from nitrobox._core import py_qmp_send
        with pytest.raises(OSError):
            py_qmp_send("/tmp/nonexistent_qmp_socket_12345.sock", '{"execute":"query-status"}')

    def test_invalid_socket_path_raises(self):
        """Empty socket path raises OSError."""
        from nitrobox._core import py_qmp_send
        with pytest.raises(OSError):
            py_qmp_send("", '{"execute":"query-status"}')

    def test_qmp_via_rust_binding_on_volume(self, vm_sandbox, tmp_path):
        """Rust QMP binding works when QMP socket is on a volume mount."""
        sb, vm_dir = vm_sandbox

        # Place QMP socket on a host-accessible volume path.
        # Sockets on overlayfs are not connectable from the host side.
        qmp_dir = tmp_path / "qmp"
        qmp_dir.mkdir()
        # The volume was already set up when vm_sandbox was created,
        # but /vm is already a volume mount, so use that path.
        qmp_path = "/vm/.nbx_qmp_test.sock"

        vm = QemuVM(sb, disk="/vm/test.qcow2", memory="128M", cpus=1,
                    qmp_socket=qmp_path)
        vm.start(timeout=30)
        try:
            from nitrobox._core import py_qmp_send
            # /vm is bind-mounted to vm_dir on host
            host_sock = Path(vm_dir) / ".nbx_qmp_test.sock"
            if not host_sock.exists():
                pytest.skip("QMP socket not found on host volume")
            msg = json.dumps({"execute": "query-status"})
            result = py_qmp_send(str(host_sock), msg, 10)
            parsed = json.loads(result)
            assert "return" in parsed
            assert parsed["return"]["status"] == "running"
        finally:
            vm.stop()


# ====================================================================== #
#  Mock QGA server                                                        #
# ====================================================================== #

class _MockQGAServer:
    """Minimal QGA protocol mock for unit testing."""

    def __init__(self, sock_path: str):
        self._path = sock_path
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.exec_output = b"mock output\n"
        self.exec_exitcode = 0
        self.file_content = b"mock file content"
        self._written: list[bytes] = []

    def start(self):
        self._thread.start()

    def stop(self):
        self._srv.close()

    @property
    def written_data(self) -> bytes:
        return b"".join(self._written)

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        f = conn.makefile("rb")
        try:
            for raw in f:
                line = raw.lstrip(b"\xff").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                cmd = req.get("execute", "")
                args = req.get("arguments", {})

                if cmd == "guest-sync-delimited":
                    resp = {"return": args["id"]}
                elif cmd == "guest-ping":
                    resp = {"return": {}}
                elif cmd == "guest-exec":
                    resp = {"return": {"pid": 42}}
                elif cmd == "guest-exec-status":
                    resp = {"return": {
                        "exited": True,
                        "exitcode": self.exec_exitcode,
                        "out-data": base64.b64encode(self.exec_output).decode(),
                    }}
                elif cmd == "guest-file-open":
                    resp = {"return": 1}
                elif cmd == "guest-file-read":
                    resp = {"return": {
                        "count": len(self.file_content),
                        "buf-b64": base64.b64encode(self.file_content).decode(),
                        "eof": True,
                    }}
                elif cmd == "guest-file-write":
                    data = base64.b64decode(args.get("buf-b64", ""))
                    self._written.append(data)
                    resp = {"return": {"count": len(data)}}
                elif cmd == "guest-file-close":
                    resp = {"return": {}}
                else:
                    resp = {"error": {"class": "CommandNotFound", "desc": cmd}}

                conn.sendall(json.dumps(resp).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            f.close()
            conn.close()


@pytest.fixture
def mock_qga(tmp_path):
    """QemuVM wired to a mock QGA server (no KVM needed)."""
    sock_path = str(tmp_path / "qga.sock")
    server = _MockQGAServer(sock_path)
    server.start()

    vm = QemuVM.__new__(QemuVM)
    vm._sb = None
    vm._qga_path = sock_path
    # _resolve_host_socket checks _host_qga_path first
    vm._host_qga_path = sock_path

    yield vm, server
    server.stop()


class TestQGAProtocol:
    """QGA client protocol tests against mock server."""

    def test_guest_ping(self, mock_qga):
        vm, _ = mock_qga
        assert vm.guest_ping(timeout=3) is True

    def test_guest_ping_timeout(self, tmp_path):
        """guest_ping returns False when nothing is listening."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        sock = str(tmp_path / "dead.sock")
        vm._qga_path = sock
        vm._host_qga_path = sock
        assert vm.guest_ping(timeout=1) is False

    def test_guest_exec(self, mock_qga):
        vm, server = mock_qga
        server.exec_output = b"hello world\n"
        server.exec_exitcode = 0
        output, ec = vm.guest_exec("echo hello world", timeout=5)
        assert ec == 0
        assert "hello world" in output

    def test_guest_exec_nonzero_exit(self, mock_qga):
        vm, server = mock_qga
        server.exec_output = b"error\n"
        server.exec_exitcode = 1
        output, ec = vm.guest_exec("false", timeout=5)
        assert ec == 1
        assert "error" in output

    def test_guest_file_read(self, mock_qga):
        vm, server = mock_qga
        server.file_content = b"secret data"
        data = vm.guest_file_read("/etc/secret")
        assert data == b"secret data"

    def test_guest_file_write(self, mock_qga):
        vm, server = mock_qga
        vm.guest_file_write("/tmp/out.txt", b"written data")
        assert server.written_data == b"written data"

    def test_wait_guest_ready(self, mock_qga):
        vm, _ = mock_qga
        # Should return immediately since mock always responds
        vm.wait_guest_ready(timeout=3)

    def test_build_cmd_includes_qga(self):
        """_build_cmd includes QGA chardev + virtio-serial device."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        vm._disk = "/vm/disk.qcow2"
        vm._memory = "2G"
        vm._cpus = 2
        vm._display = "none"
        vm._extra_args = []
        vm._qmp_path = "/tmp/.qmp.sock"
        vm._qga_path = "/tmp/.qga.sock"
        vm._cmd_override = None
        cmd = vm._build_cmd()
        assert "-chardev socket,id=nbxqga,path=/tmp/.qga.sock" in cmd
        assert "virtio-serial-pci" in cmd
        assert "org.qemu.guest_agent.0" in cmd

    def test_build_cmd_override_includes_qga(self):
        """cmd_override also gets QGA args appended."""
        vm = QemuVM.__new__(QemuVM)
        vm._sb = None
        vm._qmp_path = "/tmp/.qmp.sock"
        vm._qga_path = "/tmp/.qga.sock"
        vm._cmd_override = "qemu-system-x86_64 -m 4G"
        cmd = vm._build_cmd()
        assert cmd.startswith("qemu-system-x86_64 -m 4G")
        assert "nbxqga" in cmd
        assert "org.qemu.guest_agent.0" in cmd


# ====================================================================== #
#  QGA integration test (real VM)                                         #
# ====================================================================== #

def _find_host_kernel() -> str | None:
    """Find a bootable kernel on the host."""
    release = os.uname().release
    import glob
    candidates = [
        f"/boot/vmlinuz-{release}",
        "/boot/vmlinuz-linux",       # Arch
        "/boot/vmlinuz",             # some distros
    ]
    # Also try wildcard matches (CachyOS, custom kernels)
    candidates.extend(sorted(glob.glob("/boot/vmlinuz-*"), reverse=True))
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.R_OK):
            return p
    return None


@pytest.fixture(scope="module")
def qga_vm(tmp_path_factory, shared_cache_dir):
    """Boot a minimal Linux VM with qemu-ga for integration testing."""
    _skip_if_root()
    _skip_if_no_kvm()
    _requires_docker()

    kernel = _find_host_kernel()
    if not kernel:
        pytest.skip("no readable host kernel found")

    tmp = tmp_path_factory.mktemp("qga")
    vm_dir = tmp / "vms"
    vm_dir.mkdir()

    # Copy kernel into vm_dir (single-file volume mounts may not work)
    import shutil
    shutil.copy2(kernel, str(vm_dir / "vmlinuz"))

    config = SandboxConfig(
        image=TEST_IMAGE,
        devices=["/dev/kvm"],
        volumes=[f"{vm_dir}:/vm:rw"],
        env_base_dir=str(tmp / "envs"),
        rootfs_cache_dir=shared_cache_dir,
    )
    sb = Sandbox(config, name="qga-integ")

    # Install packages
    _, ec = sb.run(
        "apt-get update -qq 2>/dev/null && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "--no-install-recommends qemu-system-x86 qemu-guest-agent "
        "busybox-static cpio 2>/dev/null | tail -1",
        timeout=300,
    )
    if ec != 0:
        sb.delete()
        pytest.skip("failed to install qemu/qemu-ga/busybox")

    # Build minimal initramfs: busybox + qemu-ga + libs
    sb.run("rm -rf /tmp/ir && mkdir -p /tmp/ir/{bin,sbin,lib,lib64,dev,proc,sys,tmp,run}")
    sb.run("cp /bin/busybox /tmp/ir/bin/")
    sb.run("cp /usr/sbin/qemu-ga /tmp/ir/sbin/")
    # Copy shared libs for qemu-ga
    sb.run(
        "ldd /usr/sbin/qemu-ga 2>/dev/null | grep -oP '/\\S+' | "
        "while read lib; do d=/tmp/ir$(dirname $lib); "
        "mkdir -p $d; cp $lib $d/; done"
    )
    sb.run("test -f /lib64/ld-linux-x86-64.so.2 && "
           "mkdir -p /tmp/ir/lib64 && "
           "cp /lib64/ld-linux-x86-64.so.2 /tmp/ir/lib64/ 2>/dev/null; true")

    init_script = (
        "#!/bin/busybox sh\n"
        "/bin/busybox mount -t proc none /proc\n"
        "/bin/busybox mount -t sysfs none /sys\n"
        "/bin/busybox mount -t devtmpfs none /dev\n"
        "/bin/busybox --install -s /bin\n"
        "sleep 1\n"
        # Use /dev/vport0p1 directly (no udev for /dev/virtio-ports/).
        # LD_LIBRARY_PATH needed: no ld.so.cache in minimal initramfs.
        "export LD_LIBRARY_PATH=/lib/x86_64-linux-gnu:/lib\n"
        "/sbin/qemu-ga -m virtio-serial -p /dev/vport0p1 -t /tmp -d 2>/dev/null\n"
        "while true; do sleep 3600; done\n"
    )
    sb.write_file("/tmp/ir/init", init_script)
    sb.run("chmod +x /tmp/ir/init")
    _, ec = sb.run(
        "cd /tmp/ir && find . 2>/dev/null | "
        "cpio -o --quiet -H newc 2>/dev/null | gzip > /vm/initrd.img"
    )
    if ec != 0:
        sb.delete()
        pytest.skip("failed to build initramfs")

    # Create a dummy disk (QEMU requires -drive but kernel boots from -kernel)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(vm_dir / "dummy.qcow2"), "64M"],
        capture_output=True,
    )

    vm = QemuVM(
        sb,
        disk="/vm/dummy.qcow2",
        memory="256M",
        cpus=1,
        display="none",
        extra_args=[
            "-kernel", "/vm/vmlinuz",
            "-initrd", "/vm/initrd.img",
            "-append", "console=ttyS0 quiet",
            "-nographic", "-no-reboot",
        ],
        qmp_socket="/vm/.qmp.sock",
        qga_socket="/vm/.qga.sock",
    )
    try:
        vm.start(timeout=60)
    except Exception as e:
        sb.delete()
        pytest.skip(f"VM failed to start: {e}")

    try:
        vm.wait_guest_ready(timeout=30)
    except TimeoutError:
        vm.stop()
        sb.delete()
        pytest.skip("qemu-ga did not start in guest")

    yield vm, sb
    vm.stop()
    sb.delete()


class TestQGAIntegration:
    """End-to-end QGA tests with a real Linux VM."""

    def test_guest_exec_echo(self, qga_vm):
        vm, _ = qga_vm
        output, ec = vm.guest_exec("echo integration-test-ok")
        assert ec == 0
        assert "integration-test-ok" in output

    def test_guest_exec_exit_code(self, qga_vm):
        vm, _ = qga_vm
        _, ec = vm.guest_exec("false")
        assert ec != 0

    def test_guest_exec_multiline(self, qga_vm):
        vm, _ = qga_vm
        output, ec = vm.guest_exec("echo line1; echo line2")
        assert ec == 0
        assert "line1" in output
        assert "line2" in output

    def test_guest_file_write_read_roundtrip(self, qga_vm):
        vm, _ = qga_vm
        payload = b"hello from host\n"
        vm.guest_file_write("/tmp/test_roundtrip.txt", payload)
        data = vm.guest_file_read("/tmp/test_roundtrip.txt")
        assert data == payload

    def test_guest_exec_sees_written_file(self, qga_vm):
        vm, _ = qga_vm
        vm.guest_file_write("/tmp/exec_test.txt", b"visible\n")
        output, ec = vm.guest_exec("cat /tmp/exec_test.txt")
        assert ec == 0
        assert "visible" in output

    def test_guest_ping_after_loadvm(self, qga_vm):
        """QGA remains responsive after loadvm."""
        vm, _ = qga_vm
        vm.savevm("qga_test")
        try:
            vm.loadvm("qga_test")
            # QGA should resume immediately
            assert vm.guest_ping(timeout=10)
            output, ec = vm.guest_exec("echo post-loadvm")
            assert ec == 0
            assert "post-loadvm" in output
        finally:
            vm.delvm("qga_test")

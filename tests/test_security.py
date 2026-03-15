"""Tests for security hardening: seccomp, Landlock, rootless mode.

seccomp and Landlock tests require root (applied inside sandbox child).
Rootless tests run without root (that's the point).

Run with: sudo python -m pytest tests/test_security.py -v
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentdocker_lite import Sandbox, SandboxConfig

TEST_IMAGE = os.environ.get("LITE_SANDBOX_TEST_IMAGE", "ubuntu:22.04")


def _requires_root():
    if os.geteuid() != 0:
        pytest.skip("requires root")


def _requires_docker():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("requires Docker")


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #


@pytest.fixture
def root_sandbox(tmp_path):
    """Standard root sandbox with seccomp enabled (default)."""
    _requires_root()
    _requires_docker()
    config = SandboxConfig(
        image=TEST_IMAGE,
        working_dir="/workspace",
        env_base_dir=str(tmp_path / "envs"),
        rootfs_cache_dir=str(tmp_path / "cache"),
        seccomp=True,
    )
    sb = Sandbox(config, name="sec-test")
    yield sb
    sb.delete()


@pytest.fixture
def rootless_sandbox(tmp_path):
    """Rootless sandbox — skipped if running as root."""
    if os.geteuid() == 0:
        pytest.skip("rootless test must run as non-root")
    wd = tmp_path / "rootless-ws"
    config = SandboxConfig(
        working_dir=str(wd),
        env_base_dir=str(tmp_path / "envs"),
    )
    sb = Sandbox(config, name="rootless-test")
    yield sb
    sb.delete()


@pytest.fixture
def landlock_sandbox(tmp_path):
    """Rootless sandbox with explicit Landlock paths."""
    if os.geteuid() == 0:
        pytest.skip("Landlock tests run in rootless mode")
    wd = tmp_path / "ll-ws"
    config = SandboxConfig(
        working_dir=str(wd),
        env_base_dir=str(tmp_path / "envs"),
        landlock_read=["/"],
        landlock_write=[str(wd), "/tmp", "/dev"],
    )
    sb = Sandbox(config, name="ll-test")
    yield sb
    sb.delete()


# ------------------------------------------------------------------ #
#  seccomp tests (root mode)                                           #
# ------------------------------------------------------------------ #


class TestSeccomp:
    """Verify seccomp blocks dangerous operations inside sandbox."""

    def test_normal_commands_work(self, root_sandbox):
        """Normal commands should not be affected by seccomp."""
        output, ec = root_sandbox.run("echo hello && ls / > /dev/null && cat /etc/hostname")
        assert ec == 0
        assert "hello" in output

    def test_fork_works(self, root_sandbox):
        """Regular fork/exec should work (clone without NS flags)."""
        output, ec = root_sandbox.run("bash -c 'echo from_child'")
        assert ec == 0
        assert "from_child" in output

    def test_seccomp_blocks_in_rootfs_with_python(self, tmp_path):
        """seccomp blocks mount/unshare when rootfs has python3 (e.g. Kali)."""
        _requires_root()
        _requires_docker()
        config = SandboxConfig(
            image=TEST_IMAGE,
            working_dir="/workspace",
            env_base_dir=str(tmp_path / "envs"),
            rootfs_cache_dir=str(tmp_path / "cache"),
            seccomp=True,
        )
        sb = Sandbox(config, name="seccomp-py-test")
        try:
            # Check if python3 available — seccomp only works if it is
            _, ec = sb.run("which python3")
            if ec != 0:
                pytest.skip("rootfs has no python3 — seccomp helper can't run")
            output, ec = sb.run("mount -t tmpfs none /mnt 2>&1 || echo BLOCKED")
            assert "BLOCKED" in output or "Operation not permitted" in output or ec != 0
        finally:
            sb.delete()


# ------------------------------------------------------------------ #
#  Landlock tests (root mode)                                          #
# ------------------------------------------------------------------ #


class TestLandlock:
    """Verify Landlock restricts filesystem access."""

    def test_write_allowed_path(self, landlock_sandbox):
        """Writing to allowed path should work."""
        output, ec = landlock_sandbox.run("echo test > test.txt && cat test.txt")
        assert ec == 0
        assert "test" in output

    def test_write_tmp_allowed(self, landlock_sandbox):
        """Writing to /tmp should work."""
        output, ec = landlock_sandbox.run("echo tmp > /tmp/test.txt && cat /tmp/test.txt")
        assert ec == 0
        assert "tmp" in output

    def test_write_denied_path(self, landlock_sandbox):
        """Writing outside allowed paths should be denied."""
        output, ec = landlock_sandbox.run("echo bad > /etc/test 2>&1")
        assert ec != 0 or "Permission denied" in output

    def test_read_allowed(self, landlock_sandbox):
        """Reading should work (read=['/'] allows everything)."""
        output, ec = landlock_sandbox.run("cat /etc/hostname")
        assert ec == 0
        assert len(output.strip()) > 0


# ------------------------------------------------------------------ #
#  Rootless mode tests (non-root)                                      #
# ------------------------------------------------------------------ #


class TestRootless:
    """Verify rootless sandbox works without root."""

    def test_basic_command(self, rootless_sandbox):
        """Basic echo should work."""
        output, ec = rootless_sandbox.run("echo hello rootless")
        assert ec == 0
        assert "hello rootless" in output

    def test_working_directory(self, rootless_sandbox):
        """Should start in the configured working directory."""
        output, ec = rootless_sandbox.run("pwd")
        assert ec == 0
        assert "rootless-ws" in output

    def test_write_cwd_allowed(self, rootless_sandbox):
        """Writing to working directory should be allowed."""
        output, ec = rootless_sandbox.run("echo data > test.txt && cat test.txt")
        assert ec == 0
        assert "data" in output

    def test_write_tmp_allowed(self, rootless_sandbox):
        """Writing to /tmp should be allowed."""
        output, ec = rootless_sandbox.run("echo tmp > /tmp/rootless-test-file && cat /tmp/rootless-test-file")
        assert ec == 0
        assert "tmp" in output

    def test_write_etc_denied(self, rootless_sandbox):
        """Writing to /etc should be denied by Landlock."""
        output, ec = rootless_sandbox.run("echo bad > /etc/rootless-test 2>&1")
        assert ec != 0 or "Permission denied" in output

    def test_read_everywhere(self, rootless_sandbox):
        """Reading should work everywhere (default: read=['/'])."""
        output, ec = rootless_sandbox.run("cat /etc/hostname")
        assert ec == 0
        assert len(output.strip()) > 0

    def test_no_image_required(self, tmp_path):
        """Rootless mode should not require an image."""
        if os.geteuid() == 0:
            pytest.skip("rootless test must run as non-root")
        wd = tmp_path / "no-image-ws"
        config = SandboxConfig(
            working_dir=str(wd),
            env_base_dir=str(tmp_path / "envs"),
        )
        sb = Sandbox(config, name="no-image")
        output, ec = sb.run("echo works")
        assert ec == 0
        assert "works" in output
        sb.delete()

    def test_reset_is_noop(self, rootless_sandbox):
        """reset() should be a no-op in rootless mode."""
        rootless_sandbox.run("echo data > test.txt")
        rootless_sandbox.reset()
        # File should still exist (no overlayfs to clear)
        output, ec = rootless_sandbox.run("cat test.txt")
        assert ec == 0
        assert "data" in output

    def test_sequential_commands(self, rootless_sandbox):
        """Multiple sequential commands should work."""
        for i in range(5):
            output, ec = rootless_sandbox.run(f"echo iter-{i}")
            assert ec == 0
            assert f"iter-{i}" in output

    def test_custom_landlock(self, tmp_path):
        """Custom Landlock paths should be respected."""
        if os.geteuid() == 0:
            pytest.skip("rootless test must run as non-root")
        wd = tmp_path / "custom-ll-ws"
        config = SandboxConfig(
            working_dir=str(wd),
            env_base_dir=str(tmp_path / "envs"),
            landlock_read=[str(wd), "/usr", "/lib", "/dev"],
            landlock_write=[str(wd), "/dev"],
        )
        sb = Sandbox(config, name="custom-ll")
        try:
            # Write to cwd should work
            output, ec = sb.run("echo ok > test.txt && cat test.txt")
            assert ec == 0
            assert "ok" in output
            # Write to /tmp should be denied (not in write list)
            output, ec = sb.run("echo bad > /tmp/denied 2>&1")
            assert ec != 0 or "Permission denied" in output
        finally:
            sb.delete()


# ------------------------------------------------------------------ #
#  Device passthrough tests (root mode)                                #
# ------------------------------------------------------------------ #


class TestDevices:
    """Verify device passthrough."""

    def test_dev_null_accessible(self, root_sandbox):
        """/dev/null should work (created in sandbox init)."""
        output, ec = root_sandbox.run("echo test > /dev/null && echo ok")
        assert ec == 0
        assert "ok" in output

    def test_device_passthrough(self, tmp_path):
        """Passed-through device should be accessible."""
        _requires_root()
        _requires_docker()
        config = SandboxConfig(
            image=TEST_IMAGE,
            working_dir="/workspace",
            env_base_dir=str(tmp_path / "envs"),
            rootfs_cache_dir=str(tmp_path / "cache"),
            devices=["/dev/null"],  # /dev/null exists on all Linux
        )
        sb = Sandbox(config, name="dev-test")
        try:
            output, ec = sb.run("test -e /dev/null && echo exists")
            assert ec == 0
            assert "exists" in output
        finally:
            sb.delete()

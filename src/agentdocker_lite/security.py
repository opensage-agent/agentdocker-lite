"""Kernel-level security hardening: seccomp-bpf + Landlock via ctypes.

Applied inside the sandbox child process before executing user commands.
No external libraries needed — direct syscall interface.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os

logger = logging.getLogger(__name__)

# ======================================================================
# libc
# ======================================================================

_libc_name = ctypes.util.find_library("c")
_libc = ctypes.CDLL(_libc_name, use_errno=True) if _libc_name else None


# ======================================================================
# seccomp-bpf: block dangerous syscalls
# ======================================================================

# prctl constants
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# BPF constants
BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_K = 0x00
BPF_RET = 0x06
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

# Dangerous syscalls to block (x86_64 numbers)
_BLOCKED_SYSCALLS_X86_64 = {
    101: "ptrace",
    165: "mount",
    166: "umount2",
    246: "kexec_load",
    304: "open_by_handle_at",
    310: "process_vm_readv",
    311: "process_vm_writev",
    321: "bpf",
    # unshare/setns — prevent sandbox escape
    272: "unshare",
    308: "setns",
}

# aarch64 syscall numbers
_BLOCKED_SYSCALLS_AARCH64 = {
    117: "ptrace",
    40: "mount",
    39: "umount2",
    104: "kexec_load",
    265: "open_by_handle_at",
    270: "process_vm_readv",
    271: "process_vm_writev",
    280: "bpf",
    97: "unshare",
    268: "setns",
}


class _SockFilterInsn(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilterInsn)),
    ]


def _bpf_stmt(code: int, k: int) -> _SockFilterInsn:
    return _SockFilterInsn(code=code, jt=0, jf=0, k=k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> _SockFilterInsn:
    return _SockFilterInsn(code=code, jt=jt, jf=jf, k=k)


def _get_arch_and_syscalls() -> tuple[int, dict[int, str]]:
    machine = os.uname().machine
    if machine == "x86_64":
        return AUDIT_ARCH_X86_64, _BLOCKED_SYSCALLS_X86_64
    elif machine in ("aarch64", "arm64"):
        return AUDIT_ARCH_AARCH64, _BLOCKED_SYSCALLS_AARCH64
    else:
        logger.warning("seccomp: unsupported arch %s, skipping", machine)
        return 0, {}


def apply_seccomp_filter() -> bool:
    """Apply a seccomp-bpf filter that blocks dangerous syscalls.

    Returns True if applied, False if skipped (unsupported arch, no libc, etc.).
    """
    if not _libc:
        logger.warning("seccomp: libc not found, skipping")
        return False

    arch, blocked = _get_arch_and_syscalls()
    if not blocked:
        return False

    # Build BPF program:
    # 1. Load syscall number (offset 0 in seccomp_data)
    # 2. For each blocked syscall: if match → return EPERM
    # 3. Default: allow
    insns = []

    # Load architecture (offset 4 in seccomp_data)
    insns.append(_bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4))
    # Check architecture, skip all if wrong
    insns.append(_bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, arch, 0, len(blocked) + 1))

    # Load syscall number (offset 0)
    insns.append(_bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0))

    # For each blocked syscall: jump to EPERM if match
    sorted_blocked = sorted(blocked.keys())
    for i, nr in enumerate(sorted_blocked):
        remaining = len(sorted_blocked) - i - 1
        insns.append(_bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, nr, remaining, 0))

    # Default: allow
    insns.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW))

    # Block: return EPERM (errno 1)
    insns.append(_bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | 1))

    # Build filter array
    arr = (_SockFilterInsn * len(insns))(*insns)
    prog = _SockFprog(len=len(insns), filter=arr)

    # Must set NO_NEW_PRIVS first
    ret = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        logger.warning("seccomp: prctl(NO_NEW_PRIVS) failed")
        return False

    ret = _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog))
    if ret != 0:
        logger.warning("seccomp: prctl(SET_SECCOMP) failed: errno=%d", ctypes.get_errno())
        return False

    logger.debug("seccomp: blocked %d syscalls (%s)", len(blocked),
                 ", ".join(blocked.values()))
    return True


# ======================================================================
# Landlock: filesystem path restrictions
# ======================================================================

# Landlock ABI constants
LANDLOCK_CREATE_RULESET = 444  # x86_64
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446

# Access flags (ABI v1)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

# Common permission sets
FS_READ = (
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR
)
FS_READ_WRITE = (
    FS_READ |
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_REMOVE_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_CHAR |
    LANDLOCK_ACCESS_FS_MAKE_DIR |
    LANDLOCK_ACCESS_FS_MAKE_REG |
    LANDLOCK_ACCESS_FS_MAKE_SOCK |
    LANDLOCK_ACCESS_FS_MAKE_FIFO |
    LANDLOCK_ACCESS_FS_MAKE_BLOCK |
    LANDLOCK_ACCESS_FS_MAKE_SYM
)

# Landlock ABI v3+ (kernel 6.2): TCP access
LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1

LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2  # ABI v4+


def _syscall(nr: int, *args) -> int:
    if not _libc:
        return -1
    return _libc.syscall(nr, *[ctypes.c_ulong(a) for a in args])


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
    ]


class _LandlockNetPortAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("port", ctypes.c_uint64),
    ]


def apply_landlock(
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    allowed_tcp_ports: list[int] | None = None,
) -> bool:
    """Apply Landlock filesystem + network restrictions.

    Args:
        read_paths: Paths allowed for read + execute.
        write_paths: Paths allowed for read + write + execute.
        allowed_tcp_ports: TCP ports allowed for connect (None = no restriction).

    Returns True if applied, False if kernel doesn't support Landlock.
    """
    if not _libc:
        return False

    handled_fs = FS_READ_WRITE  # We handle all FS access types
    handled_net = 0
    if allowed_tcp_ports is not None:
        handled_net = LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP

    attr = _LandlockRulesetAttr(
        handled_access_fs=handled_fs,
        handled_access_net=handled_net,
    )

    # Create ruleset
    ruleset_fd = _syscall(
        LANDLOCK_CREATE_RULESET,
        ctypes.addressof(attr),
        ctypes.sizeof(attr),
        0,
    )
    if ruleset_fd < 0:
        logger.debug("Landlock not supported (kernel < 5.13 or disabled)")
        return False

    try:
        # Add read-only path rules
        for path in (read_paths or []):
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _LandlockPathBeneathAttr(
                    allowed_access=FS_READ,
                    parent_fd=fd,
                )
                _syscall(
                    LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.addressof(rule),
                    0,
                )
            finally:
                os.close(fd)

        # Add read-write path rules
        for path in (write_paths or []):
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _LandlockPathBeneathAttr(
                    allowed_access=FS_READ_WRITE,
                    parent_fd=fd,
                )
                _syscall(
                    LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.addressof(rule),
                    0,
                )
            finally:
                os.close(fd)

        # Add TCP port rules
        for port in (allowed_tcp_ports or []):
            rule = _LandlockNetPortAttr(
                allowed_access=LANDLOCK_ACCESS_NET_CONNECT_TCP,
                port=port,
            )
            _syscall(
                LANDLOCK_ADD_RULE,
                ruleset_fd,
                LANDLOCK_RULE_NET_PORT,
                ctypes.addressof(rule),
                0,
            )

        # Must set NO_NEW_PRIVS before restricting
        _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

        # Enforce
        ret = _syscall(LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if ret < 0:
            logger.warning("landlock_restrict_self failed: errno=%d", ctypes.get_errno())
            return False

    finally:
        os.close(ruleset_fd)

    n_read = len(read_paths or [])
    n_write = len(write_paths or [])
    n_ports = len(allowed_tcp_ports or [])
    logger.debug("Landlock: %d read, %d write paths, %d TCP ports", n_read, n_write, n_ports)
    return True

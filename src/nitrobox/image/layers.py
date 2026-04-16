"""Layer management — overlayfs layers via BuildKit.

All image builds and pulls go through the embedded BuildKit server.
Layers are stored in BuildKit's snapshotter and accessed directly
as overlay diff directories.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# ====================================================================== #
#  Public API                                                              #
# ====================================================================== #


def prepare_rootfs_layers_from_docker(
    image_name: str,
    cache_dir: Path,
    pull: bool = True,
) -> list[Path]:
    """Get image layers as directories for overlayfs stacking.

    Uses BuildKit's embedded server for both build and pull.
    Layer resolution via BuildKit's cache manager API.

    Args:
        image_name: Image reference (e.g. ``"ubuntu:22.04"``).
        cache_dir: Unused (kept for API compatibility).
        pull: If True, pull from registry when image not cached.

    Returns:
        Ordered list of layer directories (bottom to top).
    """
    from nitrobox.image.buildkit import BuildKitManager
    bk = BuildKitManager.get()

    # 1. Check containerd image store (persistent, survives restarts)
    cached = bk.check(image_name)
    if cached and cached.get("layer_paths"):
        paths = [Path(p) for p in cached["layer_paths"]]
        logger.info("Layer cache ready for %s: %d layers (buildkit)",
                     image_name, len(paths))
        return paths

    # 2. Pull from registry via BuildKit (no-cache → always checks registry)
    if pull:
        logger.info("Pulling %s via BuildKit", image_name)
        result = bk.pull(image_name)
        if result.get("layer_paths"):
            paths = [Path(p) for p in result["layer_paths"]]
            logger.info("Layer cache ready for %s: %d layers (buildkit pull)",
                         image_name, len(paths))
            return paths

    raise RuntimeError(
        f"Failed to pull {image_name!r}. "
        f"Check network connectivity and image name."
    )


# ====================================================================== #
#  Layer locking (for concurrent sandbox safety)                           #
# ====================================================================== #


def rmtree_mapped(path: str | Path) -> None:
    """Remove a directory that may contain files with mapped UIDs.

    Sandbox overlay upper dirs have files owned by mapped UIDs
    (e.g. host uid 493316). Regular rmtree fails on these —
    we fork into a userns with the same UID mapping to delete as root.
    """
    path = Path(path)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    _rmtree_in_userns(path)


def _rmtree_in_userns(path: Path) -> None:
    """Enter userns and rm -rf as mapped root."""
    from nitrobox.config import detect_subuid_range
    subuid = detect_subuid_range()
    if not subuid:
        shutil.rmtree(path, ignore_errors=True)
        return

    outer_uid, sub_start, sub_count = subuid
    outer_gid = os.getgid()

    userns_r, userns_w = os.pipe()
    go_r, go_w = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(userns_r)
        os.close(go_w)
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.unshare(0x10000000) != 0:  # CLONE_NEWUSER
            os._exit(1)
        os.write(userns_w, b"R")
        os.close(userns_w)
        os.read(go_r, 1)
        os.close(go_r)
        os.execvp("rm", ["rm", "-rf", str(path)])
        os._exit(127)

    os.close(userns_w)
    os.close(go_r)
    os.read(userns_r, 1)
    os.close(userns_r)

    subprocess.run(
        ["newuidmap", str(pid), "0", str(outer_uid), "1",
         "1", str(sub_start), str(sub_count)],
        capture_output=True,
    )
    subprocess.run(
        ["newgidmap", str(pid), "0", str(outer_gid), "1",
         "1", str(sub_start), str(sub_count)],
        capture_output=True,
    )

    os.write(go_w, b"G")
    os.close(go_w)
    os.waitpid(pid, 0)


def acquire_layer_locks(layer_dirs: list[Path]) -> list[int]:
    """Acquire shared (read) locks on layer directories."""
    fds: list[int] = []
    for d in layer_dirs:
        lock_path = d.parent / f".{d.name}.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH)
        fds.append(fd)
    return fds


def release_layer_locks(fds: list[int]) -> None:
    """Release shared locks acquired by :func:`acquire_layer_locks`."""
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

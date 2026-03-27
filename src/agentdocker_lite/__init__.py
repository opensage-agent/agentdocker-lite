"""agentdocker-lite: Lightweight Linux namespace sandbox for high-frequency workloads."""

from agentdocker_lite._errors import (
    SandboxConfigError,
    SandboxError,
    SandboxInitError,
    SandboxKernelError,
    SandboxTimeoutError,
)
from agentdocker_lite.config import SandboxConfig
from agentdocker_lite.sandbox import Sandbox
from agentdocker_lite.checkpoint import CheckpointManager
from agentdocker_lite.rootfs import get_image_config
from agentdocker_lite.vm import QemuVM

try:
    from agentdocker_lite.compose import ComposeProject, SharedNetwork
except ImportError:
    ComposeProject = None  # type: ignore[assignment,misc]
    SharedNetwork = None  # type: ignore[assignment,misc]

__all__ = [
    "Sandbox",
    "SandboxConfig",
    "SandboxError",
    "SandboxInitError",
    "SandboxTimeoutError",
    "SandboxKernelError",
    "SandboxConfigError",
    "CheckpointManager",
    "ComposeProject",
    "SharedNetwork",
    "QemuVM",
    "get_image_config",
]
__version__ = "0.0.5"

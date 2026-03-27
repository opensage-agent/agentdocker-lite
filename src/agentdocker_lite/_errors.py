"""Structured error types for agentdocker-lite."""


class SandboxError(Exception):
    """Base exception for all agentdocker-lite errors."""


class SandboxInitError(SandboxError):
    """Sandbox failed to initialize (image, rootfs, shell startup)."""


class SandboxTimeoutError(SandboxError):
    """Command or operation timed out."""


class SandboxKernelError(SandboxError):
    """Required kernel feature is unavailable (overlayfs, userns, Landlock, etc.)."""


class SandboxConfigError(SandboxError):
    """Invalid sandbox configuration."""

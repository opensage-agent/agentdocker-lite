# Quick Start

## Install

```bash
pip install -e .
```

Requires Linux, root (for namespace/mount), and `util-linux` (`unshare`).
Docker is only needed if you pass a Docker image name (auto-exports and caches the rootfs).

## Basic usage

```python
from agentdocker_lite import Sandbox, SandboxConfig

config = SandboxConfig(
    image="ubuntu:22.04",          # Docker image or path to rootfs dir
    working_dir="/workspace",
)
sb = Sandbox(config, name="worker-0")

# Run commands (~42ms each via persistent shell)
output, ec = sb.run("echo hello world")
print(output)  # "hello world\n"

# File I/O (direct rootfs access, bypasses shell)
sb.write_file("/workspace/payload.py", "print('hello')")
content = sb.read_file("/workspace/payload.py")

# Reset filesystem to initial state (~27ms, clears overlayfs upper)
sb.reset()

# Cleanup
sb.delete()
```

## Volumes

Three mount modes:

```python
config = SandboxConfig(
    image="ubuntu:22.04",
    volumes=[
        "/host/data:/data:ro",          # read-only bind mount
        "/host/project:/workspace:rw",  # read-write bind mount
        "/host/project:/workspace:cow", # copy-on-write (overlayfs)
    ],
)
```

`cow` mode lets the sandbox freely modify files without touching the host filesystem — writes go to an overlayfs upper layer that gets discarded on `reset()` or `delete()`.

## Background processes

```python
# Start a long-running process
handle = sb.run_background("python3 -m http.server 8080")

# Check if still running
output, running = sb.check_background(handle)

# Stop it
sb.stop_background(handle)
```

## Interactive processes (stdio pipes)

```python
# Launch a process with stdin/stdout pipes (e.g. LSP server)
proc = sb.popen("pyright --stdio")
proc.stdin.write(b'{"jsonrpc":"2.0",...}\n')
proc.stdin.flush()
response = proc.stdout.readline()
proc.terminate()
```

## Resource limits (cgroup v2)

```python
config = SandboxConfig(
    image="ubuntu:22.04",
    cpu_max="50000 100000",    # 50% of one CPU
    memory_max="536870912",    # 512MB
    pids_max="256",            # max 256 processes
)
```

## Concurrent sandboxes

All sandboxes share the same base rootfs (read-only lowerdir). Each gets its own overlayfs upper, PID namespace, and mount namespace.

```python
sandboxes = []
for i in range(32):
    config = SandboxConfig(image="ubuntu:22.04", working_dir="/workspace")
    sb = Sandbox(config, name=f"worker-{i}")
    sandboxes.append(sb)

# Run in parallel — fully isolated from each other
for sb in sandboxes:
    sb.run("apt-get update")  # writes only to this sandbox's upper layer

# Reset all
for sb in sandboxes:
    sb.reset()

# Cleanup
for sb in sandboxes:
    sb.delete()
```

## Performance comparison

| | Docker | agentdocker-lite |
|---|---|---|
| Create | ~500ms | ~4ms |
| Delete | ~500ms | ~6ms |
| Per command | ~330ms | ~42ms |
| Filesystem reset | recreate container | ~27ms |
| Isolation | full container | PID + mount namespace + chroot |
| Dependency | Docker daemon | `unshare` (util-linux) |
| Root required | no | yes |

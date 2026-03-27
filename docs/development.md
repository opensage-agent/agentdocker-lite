# Development

## Rust core

Security primitives (seccomp, capabilities, Landlock), namespace spawning, overlayfs mounting, and cgroup management are implemented in Rust via PyO3. The `_core` extension module is built by maturin.

```bash
pip install maturin
maturin develop --release        # build Rust core + install in-place
pytest tests/                    # run tests
```

To regenerate type stubs after changing Rust bindings:

```bash
cargo run --bin stub_gen --release
```

## Vendored binaries

The pip package bundles static binaries in `src/agentdocker_lite/_vendor/`:

| Binary | Purpose | Size | Source |
|---|---|---|---|
| `pasta` / `pasta.avx2` | NAT'd networking + port mapping | ~1.3MB | [passt](https://passt.top/) |
| `criu` | Process checkpoint/restore | ~2.8MB | [seqeralabs/criu-static](https://github.com/seqeralabs/criu-static/releases) v4.2 |

### Regenerating protobuf

```bash
protoc --python_out=src/agentdocker_lite/_vendor/ rpc.proto
```

## Running tests

```bash
sudo python -m pytest tests/ -v                    # all tests
sudo python -m pytest tests/test_checkpoint.py -v   # CRIU tests
python -m pytest tests/test_security.py -v -k "UserNamespace"  # rootless
```

## Architecture

```
Sandbox(config, name)
  __init__:
    if root → _init_rootful()      # direct mount/cgroup
    else   → _init_userns()        # user namespace (kernel 5.11+)

  _init_rootful / _init_userns:
    resolve rootfs (OCI layer cache)
    mount overlayfs / btrfs
    setup cgroup v2 (rootful) or systemd delegation (rootless)
    py_spawn_sandbox() → Rust init chain:
      fork → unshare(PID|MNT|UTS|IPC|USER|NET)
      mount overlayfs + volumes + /proc + /dev
      pivot_root → security (cap drop + mask + seccomp + Landlock)
      exec shell
    ← PersistentShell (stdin/stdout pipes + signal fd)

  run(cmd) → write to shell stdin, read stdout, signal fd returns exit code
  reset()  → kill shell, O(1) rename upper/work dirs, restart shell
  delete() → kill shell, unmount, cleanup cgroup, rm dirs
```

## Project structure

```
src/agentdocker_lite/
├── config.py           SandboxConfig + parsers + Docker compat
├── sandbox.py          Sandbox class (single unified implementation)
├── _errors.py          Structured error types (SandboxError hierarchy)
├── _shell.py           PersistentShell + SpawnConfig TypedDict
├── _core.pyi           Rust bindings type stubs (auto-generated)
├── rootfs.py           OCI image management + layer cache
├── _registry.py        Pure-Python OCI registry client
├── checkpoint.py       CRIU checkpoint/restore
├── vm.py               QEMU/KVM VM manager
├── cli.py              CLI commands (adl ps/kill/cleanup)
├── compose/            Docker Compose compatibility
│   ├── _parse.py       YAML parsing + service definitions
│   ├── _network.py     SharedNetwork + health checks
│   └── _project.py     ComposeProject orchestrator
└── _vendor/            Vendored binaries (pasta, criu)
```

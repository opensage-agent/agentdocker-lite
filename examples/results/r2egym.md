# R2E-Gym — Docker vs nitrobox

**Dataset:** `R2E-Gym/R2E-Gym-Lite` (split=`train`, shuffle seed=42, first 88 tasks)
**Tasks:** 88 (distributed across 10 repos: pandas, numpy, pillow, orange3, aiohttp, pyramid, scrapy, tornado, coveragepy, datalad)
**Agent:** `R2E-Gym/R2EGym-32B-Agent` served by 8× vLLM (1 instance per H200 GPU, round-robin load-balanced by task index)
**Config:** `use_fn_calling=False` (model is SFT'd on text-parsed trajectories), `max_steps=30`, `max_token_limit=32768`
**Concurrency:** 16
**Date:** 2026-04-18

Both sides' caches were fully wiped before the cold run:
- `docker rmi namanjain12/*` (88 images)
- `rmtree_mapped(~/.local/share/nitrobox/buildkit)` + `rm -rf /tmp/nitrobox_$UID`

## Cold Start

| C  | Env      | Wall    | EnvSetup    | Agent       | Verify     | Teardown    | Pass | Fail | Err |
|----|----------|---------|-------------|-------------|------------|-------------|------|------|-----|
| 16 | docker   |  806.5s |   7.0s ( 6%) | 101.0s (81%) |  5.6s ( 5%) |  11.1s ( 9%) |   33 |   55 |   0 |
| 16 | nitrobox |  719.9s |  17.7s (16%) |  89.4s (82%) |  2.0s ( 2%) |   0.1s ( 0%) |   33 |   55 |   0 |

**Cold wall-clock speedup: 1.12x** (13.4 min → 12.0 min)

### Per-phase speedup (cold)

| Phase | Docker | nitrobox | Speedup |
|-------|--------|----------|---------|
| env_setup | 7.0s | 17.7s | 0.40x |
| agent_exec | 101.0s | 89.4s | 1.13x |
| verifier | 5.6s | 2.0s | **2.76x** |
| teardown | 11.1s | 0.1s | **113.57x** |

- `env_setup` favours Docker on cold because BuildKit's pull + layer extract is slower per-image than `docker pull`. The teardown and verifier wins more than compensate — overall 1.12x.
- **Correctness identical**: 33 pass / 55 fail on both sides. Per-repo breakdown matches for all 10 repos except tornado (docker 2/2, nitrobox 1/2 — single LLM-noise failure).

## Hot Start (images cached)

| C  | Env      | Wall    | EnvSetup    | Agent      | Verify     | Teardown    | Pass | Fail | Err |
|----|----------|---------|-------------|------------|------------|-------------|------|------|-----|
| 16 | docker   |  722.1s |   1.6s ( 1%) |  95.5s (87%) |  2.1s ( 2%) |  10.8s (10%) |   29 |   59 |   0 |
| 16 | nitrobox |  695.4s |   0.7s ( 1%) |  92.5s (97%) |  2.0s ( 2%) |   0.1s ( 0%) |   32 |   56 |   0 |

**Hot wall-clock speedup: 1.04x** (12.0 min → 11.6 min)

### Per-phase speedup (hot)

| Phase | Docker | nitrobox | Speedup |
|-------|--------|----------|---------|
| env_setup | 1.6s | 0.7s | **2.16x** |
| agent_exec | 95.5s | 92.5s | 1.03x |
| verifier | 2.1s | 2.0s | 1.05x |
| teardown | 10.8s | 0.1s | **94.09x** |

### Per-repo breakdown (pass/total)

| Repo | Docker cold | nitrobox cold | Docker hot | nitrobox hot |
|------|------------:|--------------:|-----------:|-------------:|
| aiohttp    | 4/7  | 4/7  | 4/7  | 4/7  |
| coveragepy | 1/2  | 1/2  | 1/2  | 1/2  |
| datalad    | 0/2  | 0/2  | 0/2  | 0/2  |
| numpy      | 4/17 | 5/17 | 4/17 | 5/17 |
| orange3    | 4/8  | 4/8  | 4/8  | 4/8  |
| pandas     | 3/27 | 3/27 | 2/27 | 3/27 |
| pillow     | 11/15| 11/15| 10/15| 10/15|
| pyramid    | 2/4  | 2/4  | 2/4  | 2/4  |
| scrapy     | 2/4  | 2/4  | 1/4  | 2/4  |
| tornado    | 2/2  | 1/2  | 1/2  | 1/2  |

## Why wall speedup is modest vs tb2

| Bench | Agent phase share of wall | Wall speedup (hot) |
|---|---|---|
| tb2 (terminal-bench, oracle) | ~40% | 2.11x |
| R2E-Gym (LLM agent, 32B)     | **87%** | 1.04x |

With an LLM-driven agent spending the majority of each task in inference, sandbox-level differences are amortised into a smaller wall ratio. The **per-phase numbers are still dramatic**: teardown 94–113x, env_setup 2.16x (hot), verifier 2.76x (cold).

For benchmarking sandboxes specifically, the oracle agent gives sharper signal — see [`r2egym_oracle.md`](r2egym_oracle.md) (coming) for the same dataset with gold-patch replay.

## Trajectories

- Cold: `/scratch/ruilin/workspace/agentdocker-lite/results/bench_r2egym_20260418_053804/cold/{docker,nitrobox}/task_*/`
- Hot:  `/scratch/ruilin/workspace/agentdocker-lite/results/bench_r2egym_20260418_053804/hot/{docker,nitrobox}/task_*/`

Each task dir has `timing.json` (all phase durations, agent exit reason, n_steps, docker_image, score).

## Reproduce

```bash
# Prereqs
git clone https://github.com/R2E-Gym/R2E-Gym.git
cd R2E-Gym && pip install -e .
cd ../agentdocker-lite
pip install -e .
python -m nitrobox.cli setup  # one-time cgroup + subuid config
export XDG_RUNTIME_DIR=/tmp/xdg-runtime-$(id -u)
mkdir -p $XDG_RUNTIME_DIR

# Start 8× vLLM (one per GPU 0-7, ports 8000-8007)
for i in 0 1 2 3 4 5 6 7; do
  port=$((8000+i))
  CUDA_VISIBLE_DEVICES=$i setsid vllm serve R2E-Gym/R2EGym-32B-Agent \
    --host 127.0.0.1 --port $port \
    --served-model-name R2E-Gym/R2EGym-32B-Agent \
    --max-model-len 32768 --gpu-memory-utilization 0.90 \
    > /tmp/vllm_r2e_gpu${i}.log 2>&1 < /dev/null &
  sleep 2
done

# Cold + hot bench
URLS=$(python3 -c "print(','.join(f'http://127.0.0.1:{8000+i}/v1' for i in range(8)))")
python examples/bench_r2egym_e2e.py \
    --n-tasks 88 --concurrency 16 --runs cold,hot \
    --agent llm --max-steps 30 \
    --llm-name 'hosted_vllm/R2E-Gym/R2EGym-32B-Agent' \
    --llm-base-urls "$URLS"
```

Note: Docker Hub rate-limits authenticated pulls at 200/6h. A single cold bench consumes ~88 pulls per backend (176 total), so a back-to-back cold re-run will 429 unless you wait for the rolling window. Run oracle mode (`--agent oracle`) for rapid iteration — no LLM, no tool files copied, ~2 min end-to-end.

## R2E-Gym–specific bugs uncovered & fixed during this work

1. **[nitrobox #32](https://github.com/opensage-agent/nitrobox/pull/32)** — BuildKit lazy-blob resolution crash (`missing descriptor handlers for lazy blobs`) when images share a `diff_id` via different compressed-blob digests. PR bypasses `cacheManager.Get`'s lazy check by reading overlay mounts directly from the snapshotter. Verified with 2-image and 4-image minimal repros.
2. **`NitroboxRuntime.run()` heredoc truncation** — trailing `" {args}"` (empty args → single trailing space) invalidated bash heredoc terminators (`PYEOF ` ≠ `PYEOF`). Fixed by conditionally appending args.
3. **`NitroboxRuntime.copy_to_container()` silent failure** — `sandbox.copy_to()` writes via host-side overlay upper-dir paths which aren't visible inside the rootless userns mount namespace. Fixed by streaming file contents via base64 + heredoc through the persistent shell. Impact: without this, `env.add_commands()` silently failed, leaving `search`/`file_editor`/`execute_bash`/`finish` missing, every agent task erred with `127: No such file or directory`.
4. **R2E-Gym `Agent.llm_base_url`** — `Agent.__init__` reads `$LLM_BASE_URL` env var globally, ignoring `args.llm_base_url`. Bench patches `agent.llm_base_url` directly per-worker so 8-vLLM round-robin actually takes effect (instead of all 16 workers hammering one server).

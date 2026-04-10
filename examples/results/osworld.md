# OSWorld — Docker vs nitrobox

**Dataset:** OSWorld (GUI agent benchmark, Ubuntu Desktop VM)
**Agent:** Claude Sonnet 4.6, Computer Use agent
**Tasks:** 120 (12 per domain, 116 evaluated after filtering)
**Max steps:** 30
**Concurrency:** 16
**Date:** 2026-04-09/10

## E2E Results (120 tasks, concurrency 16)

### Run 1

|   C |      Env |      Wall |       EnvSetup |          Agent |            LLM |         Verify |       Teardown | Overhead | Pass | Fail |  Err |
|-----|----------|-----------|----------------|----------------|----------------|----------------|----------------|----------|------|------|------|
|  16 |   docker |   2188.9s |     16.4s (7%) |   210.5s (84%) |    56.0s (22%) |     22.9s (9%) |      0.3s (0%) |      78% |   95 |   21 |    0 |
|  16 | nitrobox |   1932.7s |      5.7s (2%) |   206.7s (88%) |    57.1s (24%) |    22.9s (10%) |      0.3s (0%) |      76% |   93 |   23 |    0 |

Wall speedup: **1.13x**, env_setup: **2.9x**

### Run 2

|   C |      Env |      Wall |       EnvSetup |          Agent |            LLM |         Verify |       Teardown | Overhead | Pass | Fail |  Err |
|-----|----------|-----------|----------------|----------------|----------------|----------------|----------------|----------|------|------|------|
|  16 |   docker |   2049.7s |     16.6s (7%) |   200.8s (84%) |    56.3s (23%) |     22.6s (9%) |      0.3s (0%) |      77% |   93 |   23 |    0 |
|  16 | nitrobox |   1941.5s |      5.9s (3%) |   194.9s (87%) |    51.2s (23%) |    22.8s (10%) |      0.3s (0%) |      77% |   91 |   25 |    0 |

Wall speedup: **1.06x**, env_setup: **2.8x**

### Run 3

|   C |      Env |      Wall |       EnvSetup |          Agent |            LLM |         Verify |       Teardown | Overhead | Pass | Fail |  Err |
|-----|----------|-----------|----------------|----------------|----------------|----------------|----------------|----------|------|------|------|
|  16 |   docker |   1899.8s |     16.5s (7%) |   194.7s (83%) |    58.4s (25%) |    22.9s (10%) |      0.3s (0%) |      75% |   90 |   26 |    0 |
|  16 | nitrobox |   1972.6s |      5.6s (2%) |   207.1s (88%) |    58.7s (25%) |    22.9s (10%) |      0.3s (0%) |      75% |   87 |   29 |    0 |

Wall speedup: **0.96x**, env_setup: **2.9x**

### Summary (3 runs, 348 tasks per env)

| Metric | Docker | nitrobox |
|--------|--------|----------|
| Wall time (mean) | 2046.1s | **1948.9s** (1.05x) |
| env_setup (mean) | 16.5s | **5.7s** (2.9x) |
| Pass rate (mean) | 79.9% (278/348) | 77.9% (271/348) |
| Errors | 0 | 0 |

- **env_setup consistently 2.9x faster** (5.7s vs 16.5s) — this is loadvm vs Docker container restart
- Wall-clock is 0.96-1.13x (noisy, ~5% average speedup)
- Pass rate differs by ~2% due to LLM non-determinism (agent picks different strategies each run)
- **0 infrastructure errors** in either provider across all 3 runs

### Per-domain breakdown (3-run totals)

| Domain | Docker | nitrobox |
|--------|--------|----------|
| chrome | 33/36 | 34/36 |
| gimp | 28/36 | 27/36 |
| libreoffice_calc | 26/36 | 26/36 |
| libreoffice_impress | 30/36 | 25/36 |
| libreoffice_writer | 29/36 | 30/36 |
| multi_apps | 17/24 | 18/24 |
| os | 29/36 | 29/36 |
| thunderbird | 29/36 | 30/36 |
| vlc | 26/36 | 21/36 |
| vs_code | 31/36 | 31/36 |

Per-domain differences are within LLM agent non-determinism noise
(same prompt, same environment, different strategies chosen per run).

## Trajectories

- Run 1:
  - Docker: `/scratch/yuzhou/projects/OSWorld/results_bench_docker_120_run1/`
  - nitrobox: `/scratch/yuzhou/projects/OSWorld/results_bench_nitrobox_120_run1/`
- Run 2:
  - Docker: `/scratch/yuzhou/projects/OSWorld/results_bench_docker_120_run2/`
  - nitrobox: `/scratch/yuzhou/projects/OSWorld/results_bench_nitrobox_120_run2/`
- Run 3:
  - Docker: `/scratch/yuzhou/projects/OSWorld/results_bench_docker_120_run3/`
  - nitrobox: `/scratch/yuzhou/projects/OSWorld/results_bench_nitrobox_120_run3/`

## Reproduce

```bash
# 1. Clone our OSWorld fork (includes nitrobox provider + --api_provider fix)
git clone -b nitrobox-provider https://github.com/rucnyz/OSWorld.git
cd OSWorld && pip install -r requirements.txt

# 2. Verify KVM access
test -w /dev/kvm && echo "KVM OK"

# 3. Run e2e comparison (Claude Sonnet 4.6, 120 tasks, 16 concurrency)
ANTHROPIC_API_KEY=sk-ant-... python examples/bench_osworld_e2e.py \
    --osworld-dir /path/to/osworld \
    --n-tasks 120 --max-steps 30 \
    --envs docker,nitrobox \
    --concurrency 16
```

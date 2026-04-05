# Harbor Dataset Compatibility

Tracking nitrobox compatibility with all Harbor-supported datasets.
Each dataset is tested with the `oracle` agent against the Docker
environment as baseline.

## Status

| Dataset | Version | Tasks | Status | Notes |
|---------|---------|-------|--------|-------|
| [terminal-bench](tb2.md) | 2.0 | 89 | **82/89 match** | 4 both-fail (task bugs), 3 differ (2 flaky, 1 fixed) |
| swebench | — | — | Not tested | |
| swebenchpro | — | — | Not tested | |
| swesmith | — | — | Not tested | |
| swtbench | — | — | Not tested | |
| aider_polyglot | — | — | Not tested | |
| autocodebench | — | — | Not tested | |
| compilebench | — | — | Not tested | |
| livecodebench | — | — | Not tested | |
| humanevalfix | — | — | Not tested | |
| evoeval | — | — | Not tested | |
| deveval | — | — | Not tested | |
| mlgym-bench | — | — | Not tested | |
| replicationbench | — | — | Not tested | |
| codepde | — | — | Not tested | |
| aime | — | — | Not tested | |
| gpqa-diamond | — | — | Not tested | |
| usaco | — | — | Not tested | |
| mmau | — | — | Not tested | |
| sldbench | — | — | Not tested | |

## How to Run

```bash
# Full comparison (oracle agent, c4, all tasks)
python examples/bench_harbor_e2e.py \
    --harbor-dir /path/to/harbor \
    --dataset terminal-bench@2.0 \
    --agent oracle \
    --concurrency 4 \
    --envs docker,nitrobox

# Or manually:
cd harbor
uv run harbor run -d <dataset>@<version> -a oracle -e nitrobox -n 4 --job-name <name> -y
uv run harbor run -d <dataset>@<version> -a oracle -e docker -n 4 --job-name <name> -y
```

## Criteria

A dataset is "supported" when:
- Every task that passes with Docker also passes with nitrobox
- Any difference is documented with root cause
- Flaky tasks (pass sometimes, fail sometimes, on both environments) are acceptable

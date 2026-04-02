#!/usr/bin/env python3
"""Benchmark: Harbor e2e — Docker vs nitrobox at different concurrency levels.

Runs the same set of tasks with both Docker and nitrobox environments,
comparing wall-clock time, per-phase overhead, and result correctness.

Requirements:
    - Harbor installed: pip install harbor  (or from source)
    - nitrobox installed: pip install nitrobox
    - Docker daemon running
    - A model endpoint (vLLM / OpenAI-compatible API)

Usage:
    # Quick test (5 tasks, concurrency 1 and 4)
    python examples/bench_harbor_e2e.py \\
        --harbor-dir /path/to/harbor \\
        --dataset terminal-bench@2.0 \\
        --agent-import-path qwen_agent_harbor:QwenAgent \\
        --n-tasks 5 --concurrency 1,4

    # Full benchmark
    python examples/bench_harbor_e2e.py \\
        --harbor-dir /path/to/harbor \\
        --dataset swebench-verified \\
        --agent oracle \\
        --n-tasks 20 --concurrency 1,4,8,16,32

Environment variables:
    MODEL_NAME         Model name for the agent
    MODEL_ENDPOINT     Model API endpoint URL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def run_harbor(
    harbor_dir: str,
    dataset: str,
    agent: str | None,
    agent_import_path: str | None,
    env_type: str,
    n_tasks: int,
    n_concurrent: int,
    job_name: str,
) -> dict:
    """Run harbor and return timing results."""
    cmd = [
        "uv", "run", "harbor", "run",
        "-d", dataset,
        "-e", env_type,
        "--n-concurrent", str(n_concurrent),
        "--n-attempts", "1",
        "-l", str(n_tasks),
        "--job-name", job_name,
    ]
    if agent:
        cmd.extend(["-a", agent])
    if agent_import_path:
        cmd.extend(["--agent-import-path", agent_import_path])

    env = {**os.environ}

    start = time.monotonic()
    result = subprocess.run(
        cmd, cwd=harbor_dir, env=env,
        capture_output=True, text=True,
    )
    wall_time = time.monotonic() - start

    if result.returncode != 0:
        print(f"  [WARN] harbor exited with code {result.returncode}")
        print(f"  stderr: {result.stderr[-500:]}")

    # Parse results from job directory
    job_dir = Path(harbor_dir) / "jobs" / job_name
    return _parse_job_results(job_dir, wall_time)


def _parse_job_results(job_dir: Path, wall_time: float) -> dict:
    """Parse trial results from a harbor job directory."""
    results = {
        "wall_time_s": wall_time,
        "trials": 0,
        "rewards": {"1.0": 0, "0.0": 0},
        "phases": {
            "environment_setup": [],
            "agent_execution": [],
            "verifier": [],
        },
        "errors": 0,
    }

    if not job_dir.exists():
        return results

    for trial_dir in job_dir.iterdir():
        if not trial_dir.is_dir():
            continue
        result_file = trial_dir / "result.json"
        if not result_file.exists():
            continue

        with open(result_file) as f:
            data = json.load(f)

        results["trials"] += 1

        # Reward
        vr = data.get("verifier_result")
        if vr:
            reward = vr.get("rewards", {}).get("reward")
            if reward is not None:
                results["rewards"][str(float(reward))] = (
                    results["rewards"].get(str(float(reward)), 0) + 1
                )

        # Exception
        if data.get("exception_info"):
            results["errors"] += 1

        # Phase timings
        for phase in ["environment_setup", "agent_execution", "verifier"]:
            timing = data.get(phase)
            if timing and timing.get("started_at") and timing.get("finished_at"):
                start = datetime.fromisoformat(timing["started_at"].rstrip("Z"))
                end = datetime.fromisoformat(timing["finished_at"].rstrip("Z"))
                duration = (end - start).total_seconds()
                results["phases"][phase].append(duration)

    return results


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _format_results_table(
    all_results: dict[str, dict[str, dict]],
    concurrency_levels: list[int],
) -> str:
    """Format results as a markdown table."""
    lines = []

    # Header
    lines.append(
        f"| {'Concurrency':>11} | {'Env':>8} | {'Wall(s)':>8} | "
        f"{'Setup(s)':>8} | {'Agent(s)':>8} | {'Verify(s)':>8} | "
        f"{'Pass':>6} | {'Fail':>6} | {'Err':>4} |"
    )
    lines.append(
        f"|{'-'*13}|{'-'*10}|{'-'*10}|"
        f"{'-'*10}|{'-'*10}|{'-'*10}|"
        f"{'-'*8}|{'-'*8}|{'-'*6}|"
    )

    for c in concurrency_levels:
        for env_type in ["docker", "nitrobox"]:
            key = f"{env_type}_c{c}"
            r = all_results.get(key)
            if not r:
                continue

            wall = r["wall_time_s"]
            setup = _mean(r["phases"]["environment_setup"])
            agent = _mean(r["phases"]["agent_execution"])
            verify = _mean(r["phases"]["verifier"])
            pass_n = r["rewards"].get("1.0", 0)
            fail_n = r["rewards"].get("0.0", 0)
            err_n = r["errors"]

            lines.append(
                f"| {c:>11} | {env_type:>8} | {wall:>8.1f} | "
                f"{setup:>8.1f} | {agent:>8.1f} | {verify:>8.1f} | "
                f"{pass_n:>6} | {fail_n:>6} | {err_n:>4} |"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Harbor e2e benchmark: Docker vs nitrobox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--harbor-dir", required=True,
        help="Path to harbor repository root",
    )
    parser.add_argument(
        "--dataset", default="terminal-bench@2.0",
        help="Harbor dataset (default: terminal-bench@2.0)",
    )
    parser.add_argument(
        "--agent", default=None,
        help="Agent name (e.g., oracle, claude-code)",
    )
    parser.add_argument(
        "--agent-import-path", default=None,
        help="Custom agent import path (e.g., qwen_agent_harbor:QwenAgent)",
    )
    parser.add_argument(
        "--n-tasks", type=int, default=10,
        help="Number of tasks to run (default: 10)",
    )
    parser.add_argument(
        "--concurrency", default="1,4",
        help="Comma-separated concurrency levels (default: 1,4)",
    )
    parser.add_argument(
        "--envs", default="docker,nitrobox",
        help="Comma-separated environments to test (default: docker,nitrobox)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to save JSON results",
    )
    args = parser.parse_args()

    concurrency_levels = [int(c) for c in args.concurrency.split(",")]
    env_types = [e.strip() for e in args.envs.split(",")]

    if not args.agent and not args.agent_import_path:
        args.agent = "oracle"

    print(f"Harbor e2e benchmark")
    print(f"  Dataset:     {args.dataset}")
    print(f"  Agent:       {args.agent or args.agent_import_path}")
    print(f"  Tasks:       {args.n_tasks}")
    print(f"  Concurrency: {concurrency_levels}")
    print(f"  Envs:        {env_types}")
    print()

    all_results = {}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for c in concurrency_levels:
        for env_type in env_types:
            job_name = f"bench_{env_type}_c{c}_{timestamp}"
            key = f"{env_type}_c{c}"

            print(f"Running: {env_type} @ concurrency={c} ...")
            r = run_harbor(
                harbor_dir=args.harbor_dir,
                dataset=args.dataset,
                agent=args.agent,
                agent_import_path=args.agent_import_path,
                env_type=env_type,
                n_tasks=args.n_tasks,
                n_concurrent=c,
                job_name=job_name,
            )
            all_results[key] = r
            print(
                f"  Done: {r['wall_time_s']:.1f}s wall, "
                f"{r['trials']} trials, "
                f"{r['rewards'].get('1.0', 0)} pass"
            )

    # Print comparison table
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80 + "\n")
    print(_format_results_table(all_results, concurrency_levels))

    # Print speedup summary
    print("\nSpeedup (wall-clock time):")
    for c in concurrency_levels:
        dk = f"docker_c{c}"
        nk = f"nitrobox_c{c}"
        if dk in all_results and nk in all_results:
            d = all_results[dk]["wall_time_s"]
            n = all_results[nk]["wall_time_s"]
            print(f"  c={c}: Docker {d:.1f}s vs nitrobox {n:.1f}s — {d/n:.2f}x")

    # Correctness check
    print("\nCorrectness check (rewards match):")
    for c in concurrency_levels:
        dk = f"docker_c{c}"
        nk = f"nitrobox_c{c}"
        if dk in all_results and nk in all_results:
            d_pass = all_results[dk]["rewards"].get("1.0", 0)
            n_pass = all_results[nk]["rewards"].get("1.0", 0)
            match = "MATCH" if d_pass == n_pass else "MISMATCH"
            print(f"  c={c}: Docker {d_pass} pass, nitrobox {n_pass} pass — {match}")

    # Save JSON
    if args.output:
        with open(args.output, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()

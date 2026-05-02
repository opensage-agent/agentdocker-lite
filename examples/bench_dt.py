#!/usr/bin/env python3
"""Benchmark: DecodingTrust-Agent compose envs — Docker vs nitrobox.

Pure infrastructure-overhead benchmark across the docker-compose
environments shipped in [DecodingTrust-Agent](https://github.com/AI-secure/DecodingTrust-Agent)
(``dt_arena/envs/<env>/docker-compose.yml``). Measures the sandbox
lifecycle cost only — no LLM, no agent, no task verification:

    create  → health_wait  → reset × N  → shutdown

For DT-Agent's typical RL-style usage (one sandbox per task, thousands
of tasks per session), this lifecycle is wholly amortized to overhead;
the ratio of nitrobox vs docker per-phase time is what shows up in
real eval throughput.

The nitrobox side passes ``healthcheck_overrides={"start_interval":
0.5}`` to ``ComposeProject``: the docker-engine default of 5 s for
``start_interval`` matters a lot here because every sandbox start
waits up to one full interval to be detected healthy. Tightening to
0.5 s recovers ≈4 s per sandbox. (Docker side runs unmodified —
docker compose's health poll cadence is already controlled by its own
internal ticker, not the per-service ``start_interval`` field, so it
isn't the bottleneck on that path.)

Setup:
    git clone https://github.com/AI-secure/DecodingTrust-Agent.git
    cd DecodingTrust-Agent && pip install -r requirements.txt

Usage:
    # 3 small envs, 3 reset cycles each
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent

    # All single-service envs (faster sweep), 5 reset cycles
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --envs finance,legal,gmail,bigquery,paypal \\
        --reset-cycles 5

    # Compare against the docker-engine default start_interval for
    # nitrobox (5 s) to quantify the override gain
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --healthcheck-start-interval 5.0

    # Save raw timings as JSON for plotting / regression tracking
    python examples/bench_dt.py \\
        --dt-dir /path/to/DecodingTrust-Agent \\
        --output dt_bench_results.json

Environment variables:
    DT_AGENT_DIR  Path to DecodingTrust-Agent checkout (alternative to --dt-dir)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Pre-defined env profiles. Each entry covers everything bench_dt
# needs to know to start the env without parsing dt_arena/config/env.yaml:
# the compose path (relative to the DT-Agent checkout), the port env
# vars to inject for the compose ``${VAR}`` substitution, and a single
# HTTP URL we can poll to confirm "ready". Keep this list small,
# aligned with envs that are actually fast to bring up — bench_dt is
# infra-only, so the cheaper the env the more cycles we can afford.
ENV_PROFILES = {
    "finance": {
        "compose": "dt_arena/envs/finance/docker-compose.yml",
        "ports": {"FINANCE_WEB_PORT": 17100},
        "health_url": "http://127.0.0.1:17100/api/health",
    },
    "legal": {
        "compose": "dt_arena/envs/legal/docker-compose.yml",
        "ports": {"LEGAL_WEB_PORT": 17201},
        "health_url": "http://127.0.0.1:17201/api/health",
    },
    "gmail": {
        "compose": "dt_arena/envs/gmail/docker-compose.yml",
        "ports": {
            "GMAIL_SMTP_PORT": 17102,
            "GMAIL_AUTH_PORT": 17103,
            "GMAIL_PROXY_PORT": 17104,
            "GMAIL_UI_PORT": 17105,
            "GMAIL_FRONTEND_PORT": 17106,
        },
        "health_url": "http://127.0.0.1:17105/api/v1/messages",
    },
}


# ── Helpers ────────────────────────────────────────────────────────────


def _timed(fn):
    t0 = time.monotonic()
    result = fn()
    return time.monotonic() - t0, result


def _http_poll(url: str, timeout: int = 120) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            r = urllib.request.urlopen(url, timeout=3)
            if 200 <= r.status < 400:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"health timeout after {timeout}s: {url}")


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _find_dt_dir(hint: str | None) -> str | None:
    candidates = [
        hint,
        os.environ.get("DT_AGENT_DIR"),
        "../DecodingTrust-Agent",
        "../../DecodingTrust-Agent",
    ]
    for c in candidates:
        if c and Path(c).is_dir() and (Path(c) / "dt_arena").is_dir():
            return str(Path(c).resolve())
    return None


# ── Per-env benchmarks ─────────────────────────────────────────────────


def bench_docker(dt_dir: str, env_name: str, profile: dict, reset_cycles: int) -> dict:
    compose = str(Path(dt_dir) / profile["compose"])
    project = f"benchdt_docker_{env_name}"
    env_subst = {k: str(v) for k, v in profile["ports"].items()}
    env = {**os.environ, **env_subst}
    cmd = ["docker", "compose", "-f", compose, "-p", project]

    r = {"create": 0.0, "health": 0.0, "reset": [], "shutdown": 0.0}

    t, _ = _timed(lambda: subprocess.run(
        cmd + ["up", "-d"], capture_output=True, env=env, check=True))
    r["create"] = t

    t, _ = _timed(lambda: _http_poll(profile["health_url"]))
    r["health"] = t

    for _ in range(reset_cycles):
        # docker compose restart is the closest analogue to nitrobox's
        # filesystem reset + service restart loop. Health re-poll is
        # included to make the per-task overhead apples-to-apples.
        t1, _ = _timed(lambda: subprocess.run(
            cmd + ["restart"], capture_output=True, env=env, check=True))
        t2, _ = _timed(lambda: _http_poll(profile["health_url"]))
        r["reset"].append(t1 + t2)

    t, _ = _timed(lambda: subprocess.run(
        cmd + ["down", "-v", "--remove-orphans"], capture_output=True, env=env))
    r["shutdown"] = t
    return r


def bench_nitrobox(
    dt_dir: str,
    env_name: str,
    profile: dict,
    reset_cycles: int,
    start_interval: float,
) -> dict:
    from nitrobox import ComposeProject

    compose = Path(dt_dir) / profile["compose"]
    project = f"benchdt_nbx_{env_name}"
    env_subst = {k: str(v) for k, v in profile["ports"].items()}

    r = {"create": 0.0, "health": 0.0, "reset": [], "shutdown": 0.0}

    t0 = time.monotonic()
    proj = ComposeProject(
        compose,
        project_name=project,
        env=env_subst,
        # KEY KNOB: tighter start_interval lets nitrobox detect a
        # newly-started service "healthy" much sooner than the
        # docker-engine default of 5 s. Override travels into the
        # per-service _HealthMonitor without forking the upstream
        # compose file.
        healthcheck_overrides={"start_interval": start_interval},
    )
    proj.up(detach=True)
    r["create"] = time.monotonic() - t0

    t, _ = _timed(lambda: proj.wait_healthy(timeout=120))
    r["health"] = t

    for _ in range(reset_cycles):
        def _cycle():
            proj.reset()
            proj.wait_healthy(timeout=120)
        t, _ = _timed(_cycle)
        r["reset"].append(t)

    t, _ = _timed(lambda: proj.down())
    r["shutdown"] = t
    return r


# ── Reporting ──────────────────────────────────────────────────────────


def _print_report(all_results: dict, reset_cycles: int, typical_task: float) -> None:
    print(f"\n{'='*84}")
    print(f"  DT-Agent compose envs — Docker vs nitrobox  ({reset_cycles} reset cycles each)")
    print(f"{'='*84}\n")

    header = (
        f"{'Env':>10} {'Backend':>9} {'create':>8} {'health':>8} "
        f"{'reset':>8} {'shutdown':>9} {'total':>8} {'overhead':>9}"
    )
    print(header)
    print("-" * len(header))

    summary = {
        "docker":   {"create": [], "health": [], "reset": [], "shutdown": [], "total": []},
        "nitrobox": {"create": [], "health": [], "reset": [], "shutdown": [], "total": []},
    }

    for env_name, by_backend in all_results.items():
        for backend in ("docker", "nitrobox"):
            r = by_backend.get(backend)
            if not r:
                continue
            total = r["create"] + r["health"] + sum(r["reset"]) + r["shutdown"]
            per_task = r["create"] + r["health"] + _avg(r["reset"]) + r["shutdown"]
            overhead_pct = per_task / (per_task + typical_task) * 100
            print(
                f"{env_name:>10} {backend:>9} {r['create']:7.2f}s {r['health']:7.2f}s "
                f"{_avg(r['reset']):7.2f}s {r['shutdown']:8.2f}s {total:7.1f}s {overhead_pct:7.1f}%"
            )
            summary[backend]["create"].append(r["create"])
            summary[backend]["health"].append(r["health"])
            summary[backend]["reset"].extend(r["reset"])
            summary[backend]["shutdown"].append(r["shutdown"])
            summary[backend]["total"].append(total)

    print(f"\n{'='*84}")
    print(f"  Aggregate (averaged across {len(all_results)} env(s))")
    print(f"{'='*84}\n")

    print(f"{'Phase':>20} {'Docker':>10} {'NitroBox':>10} {'Diff':>12} {'Winner':>10}")
    print("-" * 64)
    for phase in ("create", "health", "reset", "shutdown"):
        d, n = _avg(summary["docker"][phase]), _avg(summary["nitrobox"][phase])
        winner = "NitroBox" if n < d else "Docker"
        print(f"{phase:>20} {d:9.2f}s {n:9.2f}s {d - n:+10.2f}s {winner:>10}")

    for backend in ("docker", "nitrobox"):
        s = summary[backend]
        per_task = (
            _avg(s["create"]) + _avg(s["health"])
            + _avg(s["reset"]) + _avg(s["shutdown"])
        )
        overhead_pct = per_task / (per_task + typical_task) * 100
        print(
            f"\n  {backend.upper():>9} per-task overhead: {per_task:6.2f}s  "
            f"({overhead_pct:.1f}% of {typical_task:.0f}s task)"
        )
    print()


# ── Main ───────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(
        description="Bench DT-Agent compose envs: Docker vs nitrobox",
    )
    p.add_argument("--dt-dir", help="Path to DecodingTrust-Agent checkout (or set DT_AGENT_DIR)")
    p.add_argument(
        "--envs", default="finance,legal,gmail",
        help="Comma-separated env names. Available: " + ", ".join(ENV_PROFILES),
    )
    p.add_argument("--reset-cycles", type=int, default=3,
                   help="Reset cycles per env (default: 3)")
    p.add_argument(
        "--healthcheck-start-interval", type=float, default=0.5,
        help="nitrobox healthcheck start_interval override in seconds. "
             "Pass 5.0 to compare against docker-engine default. (default: 0.5)",
    )
    p.add_argument("--typical-task", type=float, default=60.0,
                   help="Typical task wall time used for the overhead-% column (default: 60s)")
    p.add_argument("--envs-only", choices=["docker", "nitrobox", "both"],
                   default="both", help="Run only one backend (default: both)")
    p.add_argument("--output", help="Save raw results as JSON")
    args = p.parse_args()

    dt_dir = _find_dt_dir(args.dt_dir)
    if not dt_dir:
        print("ERROR: pass --dt-dir or set DT_AGENT_DIR", file=sys.stderr)
        return 1

    selected = [e.strip() for e in args.envs.split(",") if e.strip()]
    unknown = [e for e in selected if e not in ENV_PROFILES]
    if unknown:
        print(
            f"ERROR: unknown env(s): {unknown}. Available: {list(ENV_PROFILES)}",
            file=sys.stderr,
        )
        return 1

    print(f"DT-Agent: {dt_dir}")
    print(f"Envs: {', '.join(selected)}")
    print(f"Reset cycles: {args.reset_cycles}")
    if args.envs_only in ("nitrobox", "both"):
        print(f"nitrobox start_interval override: {args.healthcheck_start_interval}s")

    all_results: dict[str, dict] = {}
    for env_name in selected:
        profile = ENV_PROFILES[env_name]
        all_results[env_name] = {}

        if args.envs_only in ("docker", "both"):
            print(f"\n--- {env_name}: Docker ---")
            try:
                all_results[env_name]["docker"] = bench_docker(
                    dt_dir, env_name, profile, args.reset_cycles,
                )
                r = all_results[env_name]["docker"]
                print(f"  done (create={r['create']:.1f}s shutdown={r['shutdown']:.1f}s)")
            except Exception as e:
                print(f"  FAILED: {e}")

        if args.envs_only in ("nitrobox", "both"):
            print(f"--- {env_name}: nitrobox ---")
            try:
                all_results[env_name]["nitrobox"] = bench_nitrobox(
                    dt_dir, env_name, profile, args.reset_cycles,
                    args.healthcheck_start_interval,
                )
                r = all_results[env_name]["nitrobox"]
                print(f"  done (create={r['create']:.1f}s shutdown={r['shutdown']:.1f}s)")
            except Exception as e:
                print(f"  FAILED: {e}")

    _print_report(all_results, args.reset_cycles, args.typical_task)

    if args.output:
        Path(args.output).write_text(json.dumps({
            "config": {
                "dt_dir": dt_dir,
                "envs": selected,
                "reset_cycles": args.reset_cycles,
                "healthcheck_start_interval": args.healthcheck_start_interval,
            },
            "results": all_results,
        }, indent=2))
        print(f"Raw results → {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

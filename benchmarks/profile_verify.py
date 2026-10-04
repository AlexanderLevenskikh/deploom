#!/usr/bin/env python3
"""Profile the heaviest CLI step (verify-exact) with cProfile.

Builds a deterministic run-dir up to a verify-ready candidate (begin,
plan-next, materialize, precheck, apply-feedback with a correct scripted
repair), then runs `python -m cProfile iterative_migration.py verify-exact`
and prints the top cumulative time consumers.

Usage:
    python benchmarks/profile_verify.py [--out profile.prof] [--top 40]
"""
from __future__ import annotations

import argparse
import pstats
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from bench_iterative_migration import (  # noqa: E402
    Fixture,
    PYTHON,
    Loop,
    _rmtree,
    _run,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "work" / "verify-exact.prof"))
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    stage = ROOT / "benchmarks" / "work" / "profile-verify"
    _rmtree(stage)
    stage.mkdir(parents=True)
    fixture = Fixture(stage)

    if not (fixture.cache / "_cacache").exists():
        fixture.seed(
            [
                {"is-number": "5.0.0", "is-finite": "1.0.0"},
                {"is-number": "7.0.0", "is-finite": "1.0.0"},
            ]
        )
    project = fixture.clone_project("project")
    run_dir = stage / "runs" / "run"
    run_dir.mkdir(parents=True)
    targets = stage / "targets.json"
    targets.write_text(
        '{"is-number": "7.0.0", "is-string": "99.99.99"}', encoding="utf-8"
    )
    verify_config = stage / "verify-config.json"
    verify_config.write_text(
        '{"commands": ["node check.js"], "projectChecks": "adaptive", '
        '"timeoutSeconds": 120, "attemptTimeoutSeconds": 1200, '
        '"snapshotCopyTimeoutSeconds": 600}',
        encoding="utf-8",
    )

    loop = Loop(fixture, project, run_dir, targets, verify_config)
    loop.cli(
        "begin",
        "--project-dir", str(project),
        "--project-name", "profile-app",
        "--target-level", "yellow",
        "--targets-file", str(targets),
        "--verify-config", str(verify_config),
        "--cohort-max-packages", "1",
        "--run-budget-minutes", "30",
        "--phase-timeout-seconds", "1200",
    )
    loop.cli("plan-next")
    candidate = loop._read_candidate()
    assert "is-number" in candidate["delta"]["changed"], candidate
    loop.cli("materialize", "--timeout-seconds", "1200")
    assert loop.steps[-1]["lastEvent"]["event"] == "materialize.done", loop.steps[-1]
    loop.cli("precheck", "--timeout-seconds", "1200")
    loop._repair_for_assignment(candidate["fullAssignment"])
    feedback = loop._feedback("READY_FOR_VERIFY", ["src/config.js"], attempt_id=0, reason="adapted")
    loop.cli("apply-feedback", "--feedback-file", str(feedback))

    prof = ROOT / "benchmarks" / "work" / "verify-exact.prof"
    env = dict(fixture.cli_env())
    started = time.perf_counter()
    result = _run(
        [PYTHON, "-m", "cProfile", "-o", str(prof),
         str(ROOT / "iterative_migration.py"), "--run-dir", str(run_dir), "verify-exact"],
        ROOT,
        env=env,
        timeout=2400,
    )
    elapsed = time.perf_counter() - started
    print(f"verify-exact wall: {elapsed:.2f}s rc={result.returncode}")
    import pstats
    stats = pstats.Stats(str(prof))
    stats.sort_stats("cumulative").print_stats(args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

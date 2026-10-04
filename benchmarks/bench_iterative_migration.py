#!/usr/bin/env python3
"""Benchmark harness for the DepLoom iterative-migration vertical loop.

Runs the REAL iterative_migration.py CLI, one fresh Python process per step
(identical to how the Desktop control plane drives it), against a tiny real
npm/Git fixture. The npm cache is seeded once (real registry mirror); every
later install runs with npm_config_offline=true so runs are deterministic.

Scenarios:
  * cold            вЂ” full vertical loop from a fresh run-dir (no proof cache).
  * repeat-identical вЂ” same loop against a fresh run-dir + a fresh byte-identical
                       project clone (warm pyc / npm / OS file cache; fresh proof
                       cache). Measures how much an identical re-run repeats work.
  * leaf-change     вЂ” same loop but one leaf target is dropped from the targets
                      file (mirrors "user changed one dependency" replan).

Each CLI step is timed and, when DEPLOOM_METRICS=1 (set by this harness), the
CLI emits ITERATIVE_MIGRATION_METRICS_V1 envelopes with subprocess/git/fs
counters per step. Raw per-step results are written as JSON under the results
directory (git-ignored).

Output: median + p95 per scenario over --reps runs, plus raw rows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# CLI project root under test. DEPLOOM_BENCH_ROOT points the harness at a
# different checkout of the production CLI (used for before/after A/B runs)
# while the bench stage/npm-cache/results stay under THIS repo's benchmarks/work.
ROOT = Path(os.environ.get("DEPLOOM_BENCH_ROOT") or Path(__file__).resolve().parents[1])
WORK = Path(__file__).resolve().parents[1] / "benchmarks" / "work"
RESULTS = WORK / "results"

PYTHON = os.environ.get("DEPLOOM_BENCH_PYTHON") or str(
    (ROOT / ".venv" / "Scripts" / "python.exe")
    if (ROOT / ".venv" / "Scripts" / "python.exe").exists()
    else sys.executable
)


def _rmtree(path: Path, retries: int = 5) -> None:
    """Windows-safe rmtree: clears read-only attributes and retries stale handles."""
    if not path.exists():
        return
    last_error: BaseException | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as exc:  # noqa: PERF203 - bounded retries
            last_error = exc
            subprocess.run(
                ["attrib", "-R", str(path), "/S", "/D"],
                capture_output=True,
                check=False,
            )
            time.sleep(1.0)
    # Last resort: PowerShell Remove-Item handles read-only git objects the way
    # shutil.rmtree cannot on Windows (retries transient anti-virus handles).
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Remove-Item -LiteralPath {json.dumps(str(path))} -Recurse -Force; "
         f"if (Test-Path -LiteralPath {json.dumps(str(path))}) {{ exit 1 }}"],
        capture_output=True,
        check=False,
    )
    if path.exists():
        raise last_error or RuntimeError(f"cannot remove {path}")


def _command(name: str) -> str | None:
    for executable in (name, name + ".cmd", name + ".exe"):
        found = shutil.which(executable)
        if found:
            return found
    return None


def _run(argv, cwd: Path, env=None, timeout: int = 2400) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


_CHECK_SOURCE = """\
const fs = require('fs');
function installedVersion(name) {
  try {
    const pkg = JSON.parse(fs.readFileSync('node_modules/' + name + '/package.json', 'utf8'));
    return pkg.version;
  } catch (e) { return 'missing'; }
}
function loadConfig() {
  try { return require('./src/config.js'); } catch (e) { return {}; }
}
const errors = [];
const num = installedVersion('is-number');
const fin = installedVersion('is-finite');
const cfg = loadConfig();
if (num === '7.0.0' && cfg.api !== 'v2') errors.push('is-number 7.0.0 requires config.api=v2');
if (num === '5.0.0' && cfg.api !== 'v1') errors.push('is-number 5.0.0 requires config.api=v1');
if (fin === '1.1.0' && cfg.feature !== 'beta') errors.push('is-finite 1.1.0 requires config.feature=beta');
if (fin === '1.0.0' && cfg.feature !== 'stable') errors.push('is-finite 1.0.0 requires config.feature=stable');
if (errors.length) { console.error(errors.join('\\n')); process.exit(1); }
console.log('checks ok');
"""


class Fixture:
    """Seeds an npm cache and materializes byte-identical project clones."""

    def __init__(self, stage: Path) -> None:
        self.stage = stage
        self.cache = stage / "npm-cache"
        self.npm = _command("npm")
        if not self.npm:
            raise RuntimeError("npm required")
        self.git = _command("git")
        self.seed_env = dict(os.environ)
        self.seed_env["npm_config_cache"] = str(self.cache)
        self.seed_env["npm_config_audit"] = "false"
        self.seed_env["npm_config_fund"] = "false"
        self.seed_env["npm_config_prefer_offline"] = "true"

    def seed(self, versions: list[dict]) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        seed_dir = self.stage / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        for deps in versions:
            manifest = {"name": "seed", "version": "1.0.0", "dependencies": deps}
            (seed_dir / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = _run([self.npm, "install", "--no-audit", "--no-fund"], seed_dir, env=self.seed_env)
            if result.returncode != 0:
                raise RuntimeError(f"npm cache seed failed: {result.stderr[-800:]}")

    def clone_project(self, name: str) -> Path:
        project = self.stage / name
        if project.exists():
            _rmtree(project)
        (project / "src").mkdir(parents=True)
        (project / "src" / "config.js").write_text(
            "module.exports = { api: 'v1', feature: 'stable' };\n", encoding="utf-8"
        )
        (project / "check.js").write_text(_CHECK_SOURCE, encoding="utf-8")
        (project / "package.json").write_text(
            json.dumps(
                {
                    "name": "sample-app",
                    "version": "1.0.0",
                    "private": True,
                    "scripts": {"test": "node check.js"},
                    "dependencies": {"is-number": "5.0.0", "is-finite": "1.0.0"},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git_env = dict(os.environ)
        git_env.update(
            {
                "GIT_AUTHOR_NAME": "bench",
                "GIT_AUTHOR_EMAIL": "bench@example.test",
                "GIT_COMMITTER_NAME": "bench",
                "GIT_COMMITTER_EMAIL": "bench@example.test",
            }
        )
        lock = _run(
            [self.npm, "install", "--package-lock-only", "--no-audit", "--no-fund"],
            project,
            env=self.seed_env,
        )
        if lock.returncode != 0:
            raise RuntimeError(f"offline lockfile generation failed: {lock.stderr[-800:]}")
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "fixture"],
        ):
            result = _run(argv, project, env=git_env)
            if result.returncode != 0:
                raise RuntimeError(f"git fixture failed: {result.stderr[-500:]}")
        return project

    def cli_env(self) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "npm_config_cache": str(self.cache),
                "npm_config_offline": "true",
                "npm_config_audit": "false",
                "npm_config_fund": "false",
                "npm_config_prefer_offline": "true",
                "PYTHONIOENCODING": "utf-8",
                "DEPLOOM_METRICS": "1",
            }
        )
        return env


def parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ITERATIVE_MIGRATION_STATUS_V1 "):
            try:
                events.append(json.loads(line[len("ITERATIVE_MIGRATION_STATUS_V1 "):]))
            except (ValueError, TypeError):
                continue
    return events


def parse_metrics(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ITERATIVE_MIGRATION_METRICS_V1 "):
            try:
                return json.loads(line[len("ITERATIVE_MIGRATION_METRICS_V1 "):])
            except (ValueError, TypeError):
                return None
    return None


class Loop:
    """Drives the vertical loop CLI and records per-step timing/counters."""

    def __init__(self, fixture: Fixture, project: Path, run_dir: Path, targets_file: Path, verify_config: Path) -> None:
        self.fixture = fixture
        self.project = project
        self.run_dir = run_dir
        self.targets_file = targets_file
        self.verify_config = verify_config
        self.steps: list[dict] = []
        self.feedback_index = 0

    def cli(self, *args: str, timeout: int = 2400) -> dict:
        started = time.perf_counter()
        env = self.fixture.cli_env()
        result = _run(
            [PYTHON, str(ROOT / "iterative_migration.py"), "--run-dir", str(self.run_dir), *args],
            ROOT,
            env=env,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        events = parse_events(result.stdout)
        metrics = parse_metrics(result.stdout)
        record = {
            "args": list(args),
            "wallSeconds": round(elapsed, 4),
            "returncode": result.returncode,
            "lastEvent": events[-1] if events else None,
            "metrics": metrics,
        }
        self.steps.append(record)
        if result.returncode not in (0, 2, 4, 130):
            raise RuntimeError(
                f"cli {args} failed rc={result.returncode}\nstdout={result.stdout[-1200:]}\nstderr={result.stderr[-800:]}"
            )
        return record

    # -- helpers mirroring the physical acceptance driver -------------------

    def _trial_project(self) -> Path:
        candidate = json.loads((self.run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))
        refs = candidate["materializationRefs"]
        return Path(refs["workspaceRoot"]) / refs["projectRelative"]

    def _scripted_repair(self, api=None, feature=None) -> None:
        config_path = self._trial_project() / "src" / "config.js"
        text = config_path.read_text(encoding="utf-8")
        if api is not None:
            text = re.sub(
                r"(api:\s*['\"])[^'\"]*(['\"])", lambda m: m.group(1) + str(api) + m.group(2), text
            )
        if feature is not None:
            text = re.sub(
                r"(feature:\s*['\"])[^'\"]*(['\"])", lambda m: m.group(1) + str(feature) + m.group(2), text
            )
        config_path.write_text(text, encoding="utf-8")

    def _feedback(self, kind: str, changed_files=None, attempt_id=0, reason=""):
        candidate = json.loads((self.run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))
        feedback = {
            "schemaVersion": 1,
            "runId": candidate["runId"],
            "candidateId": candidate["candidateId"],
            "baseCheckpointId": candidate["baseCheckpointId"],
            "attemptId": attempt_id,
            "kind": kind,
            "reason": reason,
            "changedFiles": changed_files or [],
            "diagnosticsRefs": [],
            "proposedScope": {},
            "proposedConstraints": {},
            "createdAt": "2026-09-29T00:00:00Z",
        }
        path = self.run_dir.parent / f"feedback-{self.feedback_index}-{attempt_id}.json"
        self.feedback_index += 1
        path.write_text(json.dumps(feedback), encoding="utf-8")
        return path

    def _repair_for_assignment(self, assignment: dict) -> None:
        api = feature = None
        if assignment.get("is-number") == "7.0.0":
            api = "v2"
        elif assignment.get("is-number") == "5.0.0":
            api = "v1"
        if assignment.get("is-finite") == "1.1.0":
            feature = "beta"
        elif assignment.get("is-finite") == "1.0.0":
            feature = "stable"
        self._scripted_repair(api=api, feature=feature)

    def _read_candidate(self) -> dict:
        return json.loads((self.run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))

    def _cohort(self, wrong_then_right: bool = False) -> str:
        plan = self.cli("plan-next")
        assert plan["lastEvent"]["event"] == "plan-next.candidate", plan
        candidate = self._read_candidate()
        changed = candidate["delta"]["changed"]
        assert len(changed) == 1, f"cohort must be a single package, got {changed}"
        target = next(iter(changed.values()))
        assert target in ("7.0.0", "1.1.0"), f"unexpected target {target}"
        pkg = next(iter(changed))
        self.cli("materialize", "--timeout-seconds", "1200")
        mat_done = self.steps[-1]
        assert mat_done["lastEvent"]["event"] == "materialize.done", mat_done
        self.cli("precheck", "--timeout-seconds", "1200")
        pre_ok = self.steps[-1]
        assert pre_ok["lastEvent"]["event"] == "precheck.repair-required", pre_ok
        if wrong_then_right:
            self._scripted_repair(api="wrong-api", feature="wrong-feature")
            bad = self._feedback("READY_FOR_VERIFY", ["src/config.js"], attempt_id=0, reason="wrong")
            self.cli("apply-feedback", "--feedback-file", str(bad))
            verify_bad = self.cli("verify-exact")
            assert verify_bad["lastEvent"]["event"] == "verify-exact.result" and not verify_bad["lastEvent"]["ok"]
            assert self._read_candidate()["attemptId"] == 1
            attempt = 1
        else:
            attempt = 0
        self._repair_for_assignment(candidate["fullAssignment"])
        feedback = self._feedback("READY_FOR_VERIFY", ["src/config.js"], attempt_id=attempt, reason="adapted")
        self.cli("apply-feedback", "--feedback-file", str(feedback))
        verify = self.cli("verify-exact")
        assert verify["lastEvent"]["event"] == "checkpoint.accepted", verify
        return pkg

    def _drain(self) -> None:
        for _ in range(6):
            plan = self.cli("plan-next")
            if plan["lastEvent"]["event"] == "plan-next.no-candidate":
                return
            mat = self.cli("materialize", "--timeout-seconds", "1200")
            assert mat["lastEvent"]["event"] == "materialize.failed", mat
            feedback = self._feedback("INCONCLUSIVE", reason="impossible target")
            self.cli("apply-feedback", "--feedback-file", str(feedback))
        raise AssertionError("drain did not terminate")


def run_vertical_loop(
    fixture: Fixture,
    project: Path,
    run_dir: Path,
    targets_file: Path,
    verify_config: Path,
    expected_upgrades: set[str],
) -> Loop:
    loop = Loop(fixture, project, run_dir, targets_file, verify_config)
    begin = loop.cli(
        "begin",
        "--project-dir",
        str(project),
        "--project-name",
        "sample-app",
        "--target-level",
        "yellow",
        "--targets-file",
        str(targets_file),
        "--verify-config",
        str(verify_config),
        "--cohort-max-packages",
        "1",
        "--run-budget-minutes",
        "30",
        "--phase-timeout-seconds",
        "1200",
    )
    assert begin["lastEvent"]["event"] == "begin.done", begin
    upgraded: set[str] = set()
    for index in range(len(expected_upgrades)):
        upgraded.add(loop._cohort(wrong_then_right=(index == 1)))
    assert upgraded == expected_upgrades, f"expected {expected_upgrades}, got {upgraded}"
    loop._drain()               # impossible is-string -> NO_ACTIONABLE
    finish = loop.cli("finish")
    assert finish["lastEvent"]["event"] == "finish.done", finish
    return loop


def write_verify_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "commands": ["node check.js"],
                "projectChecks": "adaptive",
                "timeoutSeconds": 120,
                "attemptTimeoutSeconds": 1200,
                "snapshotCopyTimeoutSeconds": 600,
            }
        ),
        encoding="utf-8",
    )


def summarize(values: list[float]) -> dict:
    values = sorted(values)
    n = len(values)
    if n == 0:
        return {"n": 0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "all": []}
    if n % 2 == 0:
        median = 0.5 * (values[n // 2 - 1] + values[n // 2])
    else:
        median = values[n // 2]
    p95 = values[min(n - 1, int(0.95 * (n - 1)))]
    return {
        "n": n,
        "median": round(median, 4),
        "p95": round(p95, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "all": [round(v, 4) for v in values],
    }


def scenario(fixture: Fixture, name: str, run_id: int) -> dict:
    project = fixture.clone_project(f"{name}-project-{run_id}")
    run_dir = fixture.stage / "runs" / f"{name}-{run_id}"
    if run_dir.exists():
        _rmtree(run_dir)
    run_dir.mkdir(parents=True)
    targets = fixture.stage / f"{name}-targets-{run_id}.json"
    if name == "leaf-change":
        # Drop one leaf target: only is-finite + impossible remain.
        targets.write_text(json.dumps({"is-finite": "1.1.0", "is-string": "99.99.99"}), encoding="utf-8")
        expected_upgrades = {"is-finite"}
    else:
        targets.write_text(json.dumps({"is-number": "7.0.0", "is-finite": "1.1.0", "is-string": "99.99.99"}), encoding="utf-8")
        expected_upgrades = {"is-number", "is-finite"}
    verify_config = fixture.stage / "verify-config.json"
    write_verify_config(verify_config)
    started = time.perf_counter()
    loop = run_vertical_loop(fixture, project, run_dir, targets, verify_config, expected_upgrades)
    total = time.perf_counter() - started
    step_walls = [s["wallSeconds"] for s in loop.steps]
    proc_counts = {"git": 0, "npm": 0, "node": 0, "python": 0, "other": 0}
    for step in loop.steps:
        m = step["metrics"] or {}
        for kind in ("git", "npm", "node", "python"):
            proc_counts[kind] += m.get("subprocessByKind", {}).get(kind, 0)
        proc_counts["other"] += max(
            0,
            m.get("subprocessLaunches", 0) - sum(m.get("subprocessByKind", {}).get(k, 0) for k in ("git", "npm", "node", "python")),
        )
    return {
        "scenario": name,
        "runId": run_id,
        "totalWallSeconds": round(total, 4),
        "steps": len(loop.steps),
        "stepWalls": [round(v, 4) for v in step_walls],
        "gitLaunches": proc_counts["git"],
        "npmLaunches": proc_counts["npm"],
        "nodeLaunches": proc_counts["node"],
        "pythonLaunches": proc_counts["python"],
        "otherLaunches": proc_counts["other"],
        "stepsDetail": loop.steps,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=("cold", "repeat-identical", "leaf-change", "all"), default="all")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="keep existing npm cache seed / fixture stage")
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    stage = WORK / "iterative"
    stage.mkdir(parents=True, exist_ok=True)
    fixture = Fixture(stage)

    versions = [
        {"is-number": "5.0.0", "is-finite": "1.0.0"},
        {"is-number": "7.0.0", "is-finite": "1.0.0"},
        {"is-number": "7.0.0", "is-finite": "1.1.0"},
    ]
    if (fixture.cache / "_cacache").exists():
        print("reusing existing npm cache seed", flush=True)
    else:
        print("seeding npm cache (network, once)...", flush=True)
        fixture.seed(versions)

    scenarios = ["cold", "repeat-identical", "leaf-change"] if args.scenario == "all" else [args.scenario]
    results: dict[str, list[dict]] = {}
    for scenario_name in scenarios:
        print(f"running scenario {scenario_name} x{args.reps} ...", flush=True)
        rows = []
        for i in range(args.reps):
            rows.append(scenario(fixture, scenario_name, i))
            print(
                f"  {scenario_name} run {i}: {rows[-1]['totalWallSeconds']}s "
                f"git={rows[-1]['gitLaunches']} npm={rows[-1]['npmLaunches']} "
                f"node={rows[-1]['nodeLaunches']}",
                flush=True,
            )
        results[scenario_name] = rows

    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = RESULTS / f"iterative-{stamp}.json"
    raw.write_text(json.dumps({"created": stamp, "results": results}, indent=2), encoding="utf-8")

    print("\n=== summary (seconds, e2e total) ===")
    for scenario_name, rows in results.items():
        totals = [r["totalWallSeconds"] for r in rows]
        s = summarize(totals)
        git = [r["gitLaunches"] for r in rows]
        npm = [r["npmLaunches"] for r in rows]
        print(
            f"{scenario_name:16} median={s['median']:8.2f}  p95={s['p95']:8.2f}  "
            f"min={s['min']:8.2f} max={s['max']:8.2f}  "
            f"git={summarize(git)['median']:.0f} npm={summarize(npm)['median']:.0f}  n={s['n']}"
        )
    print(f"\nraw results: {raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

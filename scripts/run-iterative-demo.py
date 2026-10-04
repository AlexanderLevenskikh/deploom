"""Run retained, physical cumulative-migration demos through the real Node
coordinator drive (desktop/dist-electron/iterative-runner.js + iterative-stream.js
+ iterative-attempt.js) and the real Python CLI, on real Git/npm fixtures.

No fake verifier, no external agent session, and no changes to a user's
project. Scripted repairs are PROPOSALS: only verify-exact may accept a
checkpoint.

Profiles (--profile):
  happy-path-24        (default) retained 24-dependency happy-path demo.
  failure-matrix       bounded infra retry + INFRA_BLOCKED stop with evidence,
                       checkpoint preserved, operator heal (cache switch) +
                       plan-next --retry-infra, stop/restart of the drive with
                       the SAME candidate, wrong-repair rejection. The demo
                       driver NEVER sends feedback (no INCONCLUSIVE) and never
                       edits durable state for the application: infrastructure
                       stops surface through the coordinator, not the driver.
  cross-group-closure  a mutually-exclusive pair (is-string + is-symbol, "a+h")
                       inside a noisy 8-package batch: the combined assignment
                       is RED, gets REJECTED once, and the planner's
                       split-then-recombine finds a workable separation. Search
                       is never declared exhausted early; no package is skipped,
                       repeated or duplicated across accepted checkpoints.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "physical_driver", ROOT / "tests/acceptance/test_iterative_migration_physical.py"
)
physical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(physical)

OLD = {"is-number": "5.0.0", "is-finite": "1.0.0", "is-string": "1.0.7",
       "is-boolean": "0.0.1", "is-date-object": "1.0.5", "is-symbol": "1.0.4",
       "is-bigint": "1.0.4", "is-regex": "1.1.4", "is-negative-zero": "2.0.2",
       "is-map": "2.0.2", "is-set": "2.0.2", "object-is": "1.1.5"}
NEW = {"is-number": "7.0.0", "is-finite": "1.1.0", "is-string": "1.1.1",
       "is-boolean": "0.0.2", "is-date-object": "1.1.0", "is-symbol": "1.1.1",
       "is-bigint": "1.1.0", "is-regex": "1.2.1", "is-negative-zero": "2.0.3",
       "is-map": "2.0.3", "is-set": "2.0.3", "object-is": "1.1.6"}

EXTRA_OLD = {"is-array-buffer": "3.0.2", "is-typed-array": "1.1.9",
             "is-data-view": "1.0.0", "is-shared-array-buffer": "1.0.2",
             "is-weakmap": "2.0.1", "is-weakset": "2.0.2",
             "is-generator-function": "1.0.10", "is-async-function": "2.0.0",
             "is-callable": "1.2.4", "has-symbols": "1.0.3",
             "has-tostringtag": "1.0.0", "isobject": "3.0.1"}
EXTRA_NEW = {"is-array-buffer": "3.0.5", "is-typed-array": "1.1.15",
             "is-data-view": "1.0.2", "is-shared-array-buffer": "1.0.4",
             "is-weakmap": "2.0.2", "is-weakset": "2.0.4",
             "is-generator-function": "1.1.0", "is-async-function": "2.1.1",
             "is-callable": "1.2.7", "has-symbols": "1.1.0",
             "has-tostringtag": "1.0.2", "isobject": "4.0.0"}

NODE_DRIVE = ROOT / "scripts/iterative-node-drive.mjs"


# -- shared fixture / CLI / drive helpers ------------------------------------

def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def npm_env(cache: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "npm_config_cache": str(cache),
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_prefer_offline": "true",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def seed_cache(cache: Path, version_sets, timeout: int = 1800) -> None:
    """Warm the npm cache (real registry, once) with EXACT version sets so every
    later install in the demo runs with npm_config_offline=true."""
    cache.mkdir(parents=True, exist_ok=True)
    seed = cache.parent / f"seed-{cache.name}"
    seed.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "npm_config_cache": str(cache),
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_prefer_offline": "true",
        }
    )
    for deps in version_sets:
        write_json(seed / "package.json", {"name": "demo-seed", "version": "1.0.0", "dependencies": deps})
        result = physical._run([physical._command("npm"), "install", "--no-audit", "--no-fund"], seed, env=env, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"npm cache seed failed (registry unreachable?): {result.stderr[-800:]}")


def build_project(stage: Path, old: dict, config_defaults: dict, check_js: str, cache: Path | None = None) -> Path:
    """A git project pinned to OLD versions whose `node check.js` is version
    sensitive about src/config.js. The lockfile is generated offline from the
    seeded cache so the fixture stays hermetic after seeding."""
    cache = cache or (stage / "npm-cache")
    npm = physical._command("npm")
    git = physical._command("git")
    project = stage / "sample-app"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "config.js").write_text(
        "module.exports = " + json.dumps(config_defaults) + ";\n", encoding="utf-8"
    )
    (project / "check.js").write_text(check_js, encoding="utf-8")
    write_json(
        project / "package.json",
        {
            "name": "sample-app",
            "version": "1.0.0",
            "private": True,
            "scripts": {"test": "node check.js"},
            "dependencies": old,
        },
    )
    git_env = dict(os.environ)
    git_env.update(
        {
            "GIT_AUTHOR_NAME": "Migration Demo",
            "GIT_AUTHOR_EMAIL": "demo@example.test",
            "GIT_COMMITTER_NAME": "Migration Demo",
            "GIT_COMMITTER_EMAIL": "demo@example.test",
        }
    )
    lock = physical._run([npm, "install", "--package-lock-only", "--no-audit", "--no-fund"], project, env=npm_env(cache), timeout=1800)
    if lock.returncode:
        raise RuntimeError(f"offline lockfile generation failed: {lock.stderr[-800:]}")
    for argv in ([git, "init", "-q"], [git, "add", "-A"], [git, "commit", "-q", "-m", "fixture"]):
        result = physical._run(argv, project, env=git_env, timeout=300)
        if result.returncode:
            raise RuntimeError(f"git fixture failed: {result.stderr[-500:]}")
    return project


def run_cli(run_dir: Path, env: dict, *args: str, timeout: int = 2400) -> dict:
    command = [sys.executable, str(ROOT / "iterative_migration.py"), "--run-dir", str(run_dir), *args]
    result = physical._run(command, ROOT, env=env, timeout=timeout)
    events = physical.parse_events(result.stdout)
    failure = physical.parse_failure(result.stdout or result.stderr)
    payload = {
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "events": events,
        "failure": failure,
        "last": events[-1] if events else None,
    }
    if result.returncode not in (0, 2, 4, 130) and not failure:
        raise RuntimeError(
            f"iterative CLI {args} failed rc={result.returncode}\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )
    return payload


def read_candidate(run_dir: Path) -> dict:
    return read_json(run_dir / "trial" / "candidate.json")


def read_run(run_dir: Path) -> dict:
    return read_json(run_dir / "run.json")


def run_drive(run_dir: Path, env: dict, timeout: int = 3600) -> dict:
    """Run the REAL production coordinator drive headlessly until it stops at a
    gate / finish / error. The drive never acts for the application."""
    if not NODE_DRIVE.exists():
        raise RuntimeError(
            f"{NODE_DRIVE} missing; build Desktop first (cd desktop && npx tsc -p tsconfig.electron.json)"
        )
    script = ROOT / "iterative_migration.py"
    command = [physical._command("node"), str(NODE_DRIVE), "--run-dir", str(run_dir), "--python", sys.executable, "--script", str(script)]
    result = physical._run(command, ROOT, env=env, timeout=timeout)
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("NODE_DRIVE_RESULT "):
            return json.loads(line[len("NODE_DRIVE_RESULT "):])
    raise RuntimeError(
        f"node drive gave no result: rc={result.returncode}\nstdout={result.stdout[-2500:]}\nstderr={result.stderr[-2500:]}"
    )


def write_feedback(run_dir: Path, env: dict, *, kind: str, attempt_id: int, changed_files=None, reason=""):
    candidate = read_candidate(run_dir)
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
        "createdAt": "2026-10-04T00:00:00Z",
    }
    path = run_dir / "feedback-write.json"
    write_json(path, feedback)
    result = run_cli(run_dir, env, "apply-feedback", "--feedback-file", str(path))
    if result["last"] is None or result["last"]["event"] != "feedback.accepted":
        raise RuntimeError(f"apply-feedback {kind} attempt={attempt_id} not accepted: {result['stdout'][-1500:]}")
    return feedback


def repair_config(path: Path, changes: dict) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in changes.items():
        text = re.sub(
            rf"(\"{key}\"\s*:\s*\")[^\"]*(\")",
            lambda m: m.group(1) + str(value) + m.group(2),
            text,
        )
    path.write_text(text, encoding="utf-8")


PAUSE_MARKER = ".dependency-roadmap/demo-stop-ready"
_PAUSE_BLOCK = (
    "if (process.env.DEPLOOM_DEMO_PAUSE === '1') {"
    "require('fs').mkdirSync('.dependency-roadmap',{recursive:true});"
    "require('fs').writeFileSync('.dependency-roadmap/demo-stop-ready','ready');"
    "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,60000);}\n"
)


def check_source(contracts: str, *extra_tail: str) -> str:
    """Build a version-sensitive check.js: asserts every dependency is installed,
    applies the given per-version contract lines, then optionally pauses."""
    return (
        "const fs=require('fs');\n"
        "function v(name){try{return JSON.parse(fs.readFileSync('node_modules/'+name+'/package.json','utf8')).version}catch(e){return 'missing'}}\n"
        "function cfg(){try{return require('./src/config.js')}catch(e){return {}}}\n"
        "const errors=[];\n"
        "const c=cfg();\n"
        + contracts
        + ("if (errors.length){console.error(errors.join('\\n'));process.exit(1);}\n"
           "console.log('checks ok');\n")
        + "".join(extra_tail)
    )


def version_presence(name: str, allowed: list) -> str:
    allowed_literal = ", ".join(repr(x) for x in allowed)
    return f"if (![{allowed_literal}].includes(v('{name}'))) errors.push('{name} unusable: '+v('{name}'));\n"


def contract_if(name: str, version: str, field: str, value: str) -> str:
    return f"if (v('{name}')==='{version}' && c.{field}!=='{value}') errors.push('{name} {version} needs {field}={value}');\n"


def trial_project(run_dir: Path) -> Path:
    candidate = read_candidate(run_dir)
    refs = candidate["materializationRefs"]
    return Path(refs["workspaceRoot"]) / refs["projectRelative"]


def kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(pid, signal.SIGKILL)


def run_drive_with_restart(run_dir: Path, env: dict, *, restart_after_step: str, timeout: int = 3600) -> dict:
    """Run the real drive but kill it right after it successfully executed
    `restart_after_step`, then re-run the drive from the durable state. Proves
    the coordinator is restart-safe: the same candidate and the same checkpoint
    resume, and no attempt is re-spent."""
    proc = subprocess.Popen(
        [physical._command("node"), str(NODE_DRIVE), "--run-dir", str(run_dir),
         "--python", sys.executable, "--script", str(ROOT / "iterative_migration.py")],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        deadline = time.monotonic() + timeout
        restarted = False
        while proc.poll() is None and time.monotonic() < deadline:
            attempt_path = run_dir / "attempt.json"
            steps = []
            if attempt_path.exists():
                try:
                    steps = list(read_json(attempt_path).get("stepsDone") or [])
                except Exception:
                    steps = []
            if restart_after_step in steps and not restarted:
                restarted = True
                time.sleep(0.5)  # let the drive record the successful step and move on
                kill_pid_tree(proc.pid)
                break
            time.sleep(0.2)
        if proc.poll() is None:
            kill_pid_tree(proc.pid)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            kill_pid_tree(proc.pid)
            proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            kill_pid_tree(proc.pid)
            proc.communicate(timeout=30)
    if not restarted:
        raise RuntimeError(f"drive did not execute {restart_after_step} for a restart marker")
    return run_drive(run_dir, env, timeout=timeout)


def drive_until_finish(run_dir: Path, env: dict, *, repair_for, feedback_log: list, max_segments: int = 12, timeout: int = 3600) -> dict:
    """Drive until 'finished', servicing every agent gate with the SCRIPTED
    external agent (repair + READY_FOR_VERIFY feedback). Never INCONCLUSIVE."""
    for _ in range(max_segments):
        result = run_drive(run_dir, env, timeout=timeout)
        if result["stopped"] == "finished":
            return result
        if result["stopped"] == "agent-gate":
            candidate = read_candidate(run_dir)
            attempt = int(candidate.get("attemptId") or 0)
            repair_for(run_dir, candidate)
            feedback_log.append("READY_FOR_VERIFY")
            write_feedback(run_dir, env, kind="READY_FOR_VERIFY", attempt_id=attempt,
                           changed_files=["src/config.js"], reason="adapted config to the attempted assignment")
            continue
        if result["stopped"] == "infra-blocked":
            raise RuntimeError(f"unexpected infra-blocked in drive: {result['reason']}")
        raise RuntimeError(f"drive stopped unexpectedly: {json.dumps(result)}")
    raise RuntimeError("drive did not reach finish within segment budget")


# -- profiles ----------------------------------------------------------------

def _happy_path_body(d, old, new, blocked) -> None:
    stage, project = d.stage, d.project
    print("DEMO_WORKSPACE " + str(stage), flush=True)
    def write(path, value):
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    seed = stage / "seed"
    for deps in (old, new):
        write(seed / "package.json", {"name": "demo-seed", "version": "1.0.0", "dependencies": deps})
        result = physical._run([d.npm, "install", "--no-audit", "--no-fund"], seed, env=d.seed_env, timeout=1800)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
    manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"] = old
    manifest["scripts"]["test"] = "node check.js"
    write(project / "package.json", manifest)
    check = project / "check.js"
    checks = """
for (const [name, versions] of Object.entries(DEMO_ALLOWED)) {
  const version = installedVersion(name);
  if (!versions.includes(version)) throw Error(`unexpected ${name}@${version}`);
  if (typeof require(name) !== 'function') throw Error(`unusable export: ${name}`);
}
for (const [name, field] of [['is-array-buffer', 'buffer'], ['is-callable', 'callable']]) {
  if (DEMO_NEW[name] && installedVersion(name) === DEMO_NEW[name] && cfg[field] !== 'adapted') {
    throw Error(`${name} upgrade requires application adaptation: ${field}`);
  }
}
"""
    pause = _PAUSE_BLOCK
    origins = check.read_text(encoding="utf-8")
    check.write_text(pause + origins + "\nconst DEMO_ALLOWED = " +
                     json.dumps({name: [old[name], new[name]] for name in old}) +
                     ";\nconst DEMO_NEW = " + json.dumps(new) + ";\n" + checks, encoding="utf-8")
    (project / ".gitignore").write_text("node_modules/\n.dependency-roadmap/\n", encoding="utf-8")
    result = physical._run([d.npm, "install", "--package-lock-only", "--no-audit", "--no-fund"], project, env=d.seed_env)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "Migration Demo", "GIT_AUTHOR_EMAIL": "demo@example.test",
               "GIT_COMMITTER_NAME": "Migration Demo", "GIT_COMMITTER_EMAIL": "demo@example.test"}
    for argv in ([d.git, "add", "-A"], [d.git, "commit", "-qm", "dependency demo"]):
        result = physical._run(argv, project, env=git_env)
        if result.returncode: raise RuntimeError(result.stderr)
    targets = {**new, **{name: "99.99.99" for name in blocked}}
    verify = d._verify_config_file()
    verify_config = json.loads(verify.read_text(encoding="utf-8"))
    verify_config["commands"] = ["npm run test"]
    write(verify, verify_config)
    run = stage / "run"
    timeline = []
    def cli(*argv):
        result = d._run_cli(run, *argv)
        last = result["last"] or result["failure"] or {}
        timeline.append({"command": list(argv), "returncode": result["returncode"], "result": last})
        write(stage / "TIMELINE.json", timeline)
        print(json.dumps({"command": argv[0], "result": last}, ensure_ascii=True), flush=True)
        (stage / "cli.log").open("a", encoding="utf-8").write(result["stdout"] + result["stderr"])
        return result
    begin = cli("begin", "--project-dir", str(project), "--project-name", "Iterative.Demo",
                "--targets-file", str(d._targets_file(targets)), "--verify-config", str(verify),
                "--run-budget-minutes", "120", "--phase-timeout-seconds", "1200")
    d.assertEqual("begin.done", begin["last"]["event"])
    checkpoints, wrong_repair, deferred = [], False, 0
    stopped = None
    repair_cohorts = []
    deferred_bases = set()
    for _ in range(max(3 * len(old), 24)):
        planned = cli("plan-next")
        if planned["last"]["event"] == "plan-next.no-candidate": break
        candidate = json.loads((run / "trial" / "candidate.json").read_text(encoding="utf-8"))
        before = physical.read_runtime_state(run)
        result = cli("materialize", "--timeout-seconds", "1200")
        if result["last"]["event"] == "materialize.failed":
            d.assertEqual("resolver", result["last"]["kind"])
            broken_key = (candidate["baseCheckpointId"], tuple(sorted(candidate["delta"]["changed"])))
            d.assertNotIn(broken_key, deferred_bases, "Same broken assignment repeated on unchanged base")
            deferred_bases.add(broken_key)
            deferred += 1
            continue
        d.assertEqual("materialize.done", result["last"]["event"])
        if stopped is None and checkpoints:
            env = {**d.seed_env, "npm_config_offline": "true", "DEPLOOM_DEMO_PAUSE": "1"}
            marker = d._trial_project(run) / PAUSE_MARKER
            with (stage / "stopped-precheck.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, str(ROOT / "iterative_migration.py"),
                    "--run-dir", str(run), "precheck"], cwd=ROOT, env=env, stdout=log, stderr=log,
                    start_new_session=os.name != "nt")
                try:
                    deadline = time.monotonic() + 120
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                    d.assertTrue(marker.exists(), "Pause marker was not reached by real project command")
                    d.assertIsNone(process.poll(), "Command exited before stop")
                    kill_pid_tree(process.pid)
                    process.wait(timeout=30)
                finally:
                    if process.poll() is None:
                        kill_pid_tree(process.pid)
                        process.wait(timeout=30)
            after_stop = physical.read_runtime_state(run)
            d.assertEqual(before["activeCheckpointId"], after_stop["activeCheckpointId"])
            same_trial = json.loads((run / "trial" / "candidate.json").read_text(encoding="utf-8"))
            d.assertEqual(candidate["candidateId"], same_trial["candidateId"])
            stopped = {"runId": before["runId"], "candidateId": candidate["candidateId"],
                       "checkpointBefore": before["activeCheckpointId"], "checkpointAfterStop": after_stop["activeCheckpointId"]}
            write(stage / "STOP_RESUME.json", stopped)
        precheck = cli("precheck", "--timeout-seconds", "1200")
        attempt = 0
        if precheck["last"]["event"] == "precheck.repair-required":
            repair_cohorts.append(candidate["delta"]["changed"])
            if not wrong_repair:
                d._scripted_repair(run, api="wrong-api", feature="wrong-feature")
                feedback = d._write_feedback(run, "READY_FOR_VERIFY", changed_files=["src/config.js"], attempt_id=0)
                cli("apply-feedback", "--feedback-file", str(feedback))
                bad = cli("verify-exact")
                d.assertFalse(bad["last"]["ok"])
                d.assertEqual(before["activeCheckpointId"], physical.read_runtime_state(run)["activeCheckpointId"])
                wrong_repair, attempt = True, 1
            d._repair_for_assignment(run, candidate["fullAssignment"])
            config_path = d._trial_project(run) / "src/config.js"
            adaptations = {field: "adapted" for name, field in
                           [("is-array-buffer", "buffer"), ("is-callable", "callable")]
                           if candidate["fullAssignment"].get(name) == new.get(name) and name in new}
            if adaptations:
                with config_path.open("a", encoding="utf-8") as stream:
                    stream.write("\nObject.assign(module.exports, " + json.dumps(adaptations) + ");\n")
        feedback = d._write_feedback(run, "READY_FOR_VERIFY", changed_files=["src/config.js"] if precheck["last"]["event"] == "precheck.repair-required" else [], attempt_id=attempt)
        cli("apply-feedback", "--feedback-file", str(feedback))
        verified = cli("verify-exact")
        d.assertEqual("checkpoint.accepted", verified["last"]["event"])
        state = physical.read_runtime_state(run)
        d.assertEqual(before["activeCheckpointId"], state["activeCheckpoint"]["parentCheckpointId"])
        for name, version in before["activeCheckpoint"]["fullAssignment"].items():
            if name not in candidate["delta"]["changed"]:
                d.assertEqual(version, state["activeCheckpoint"]["fullAssignment"][name])
        checkpoints.append(state["activeCheckpointId"])
    else:
        raise AssertionError("Planner did not terminate within bounded cohort attempts")
    d.assertTrue(wrong_repair)
    d.assertGreaterEqual(deferred, 1)
    d.assertLessEqual(deferred, 3 * len(blocked))
    accepted_updates = sum(1 for name, version in physical.read_runtime_state(run)["activeCheckpoint"]["fullAssignment"].items() if version != old[name])
    d.assertEqual(len(old) - len(blocked), accepted_updates)
    d.assertLess(len(checkpoints), accepted_updates, "Greedy cohorts must reduce physical verification cycles")
    d.assertIsNotNone(stopped)
    finished = cli("finish")
    d.assertEqual("finish.done", finished["last"]["event"])
    state = physical.read_runtime_state(run)
    final = state["activeCheckpoint"]["fullAssignment"]
    for name, version in new.items():
        d.assertEqual(old[name] if name in blocked else version, final[name])
    for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md"):
        d.assertTrue((run / "reports" / name).is_file())
    write(stage / "ACCEPTANCE.json", {"directDependencies": len(old), "acceptedUpdates": accepted_updates, "acceptedCohorts": len(checkpoints),
          "deferredUpdates": len(blocked), "repairCohorts": repair_cohorts, "stopResume": stopped, "deferredAttempts": deferred, "wrongRepairRejected": wrong_repair, "checkpoints": checkpoints,
          "finalRun": state, "sourceGitStatus": physical._run([d.git, "status", "--porcelain"], project).stdout})
    print("DEMO_ACCEPTED " + str(stage / "ACCEPTANCE.json"), flush=True)


def _accepted_delta_chain(run_dir: Path, final_state: dict) -> list:
    """AcceptedDelta.changed for every verified checkpoint on the parent chain
    (C0 and empty deltas excluded): the cumulative set of accepted upgrades."""
    deltas: list = []
    current = final_state["activeCheckpoint"]
    seen_ids = {final_state["activeCheckpointId"]}
    while current:
        changed = (current.get("acceptedDelta") or {}).get("changed") or {}
        if changed:
            deltas.append(changed)
        parent = current.get("parentCheckpointId")
        if not parent or parent in seen_ids:
            break
        seen_ids.add(parent)
        cp_path = run_dir / "checkpoints" / f"{parent}.json"
        if not cp_path.exists():
            break
        current = read_json(cp_path)
    return deltas


def _targets_file(stage: Path, targets: dict) -> Path:
    path = stage / "targets.json"
    write_json(path, targets)
    return path


def run_failure_matrix(driver) -> None:
    d = driver
    stage = Path(tempfile.mkdtemp(prefix="demo-failure-matrix-"))
    print("DEMO_WORKSPACE " + str(stage), flush=True)
    try:
        old = {"is-number": "5.0.0", "is-finite": "1.0.0", "is-bigint": "1.0.4"}
        new = {"is-number": "7.0.0", "is-finite": "1.1.0", "is-bigint": "1.1.0"}
        cache_a = stage / "npm-cache-a"
        cache_b = stage / "npm-cache-b"
        # cache A has OLD everything but NONE of the NEW versions: every exact
        # candidate install is an offline infra cache-miss, whatever the planner
        # proposes first, so the bounded retry path is hit deterministically.
        seed_cache(cache_a, [old])
        # cache B heals the cause: it additionally has every NEW version.
        seed_cache(cache_b, [old, new])
        contracts = (
            contract_if("is-number", "7.0.0", "api", "v2")
            + contract_if("is-number", "5.0.0", "api", "v1")
            + contract_if("is-finite", "1.1.0", "feature", "beta")
            + contract_if("is-finite", "1.0.0", "feature", "stable")
            + version_presence("is-bigint", ["1.0.4", "1.1.0"])
        )
        project = build_project(stage, old,
                                {"api": "v1", "feature": "stable"},
                                check_source(contracts),
                                cache=cache_a)
        verify = stage / "verify-config.json"
        write_json(verify, {
            "commands": ["node check.js"], "projectChecks": "adaptive",
            "timeoutSeconds": 120, "attemptTimeoutSeconds": 1200,
            "snapshotCopyTimeoutSeconds": 600,
        })
        run = stage / "run"
        run.mkdir(parents=True, exist_ok=True)
        env_a = npm_env(cache_a)
        env_b = npm_env(cache_b)
        feedback_log: list = []

        targets = dict(new)
        begin = run_cli(run, env_a, "begin", "--project-dir", str(project), "--project-name", "FailureMatrix",
                        "--targets-file", str(_targets_file(stage, targets)), "--verify-config", str(verify),
                        "--run-budget-minutes", "60", "--phase-timeout-seconds", "1200")
        d.assertEqual("begin.done", begin["last"]["event"])
        c0 = physical.read_runtime_state(run)
        d.assertEqual("C0", c0["activeCheckpointId"])
        d.assertEqual("VERIFIED", c0["activeCheckpoint"]["status"])

        # 1. Drive with the degraded cache: unbounded repeats are impossible;
        #    exact maxInfraRetries install attempts then an INFRA_BLOCKED stop.
        drive_a = run_drive(run, env_a)
        d.assertEqual("infra-blocked", drive_a["stopped"],
                      f"expected infra-blocked stop, got {json.dumps(drive_a)}")
        materializes = [s for s in (drive_a.get("steps") or []) if s == "materialize"]
        d.assertEqual(3, len(materializes),
                      f"maxInfraRetries=2 must bound installs to 3, got {len(materializes)}")
        run_state = read_run(run)
        d.assertIsNotNone(run_state.get("infraBlocked"), "run.infraBlocked must be durable")
        d.assertEqual("CACHE_MISS", run_state["infraBlocked"].get("code"), "infra stop must carry the cache-miss evidence")
        d.assertEqual("infrastructure", run_state["infraBlocked"].get("classification"))
        d.assertTrue(run_state["infraBlocked"].get("assignmentFingerprint"), "infra stop must carry the blocked fingerprint")
        ledger = read_json(run / "ledger.json")
        d.assertEqual(3, ledger["counters"]["infraRetries"], "infra retry counter must record bounded attempts")
        after_stop = physical.read_runtime_state(run)
        d.assertEqual("C0", after_stop["activeCheckpointId"],
                      "verified checkpoint must be preserved across the infra stop")
        d.assertEqual(c0["activeCheckpoint"]["fullAssignment"], after_stop["activeCheckpoint"]["fullAssignment"])

        # 2. The healthy cache alone does NOT silently continue: the durable
        #    INFRA_BLOCKED marker still stops the coordinator until the operator
        #    explicitly retries.
        drive_b0 = run_drive(run, env_b)
        d.assertEqual("infra-blocked", drive_b0["stopped"],
                      "drive must not auto-continue past INFRA_BLOCKED without retry-infra")

        # 3. Operator continuation: plan-next --retry-infra after the cause changed.
        retried = run_cli(run, env_b, "plan-next", "--retry-infra")
        d.assertEqual("plan-next.candidate", retried["last"]["event"], retried["stdout"][-2000:])
        d.assertIsNone(read_run(run).get("infraBlocked"), "retry-infra must clear the marker")

        # 4. Restart the drive right after the healed materialize: the SAME
        #    candidate and SAME checkpoint must resume (restart-safe), then a
        #    wrong repair is rejected before the correct one lands.
        before_restart = read_candidate(run)
        before_restart_checkpoint = read_run(run)["activeCheckpointId"]
        restart_result = run_drive_with_restart(run, env_b, restart_after_step="materialize")
        d.assertEqual("agent-gate", restart_result["stopped"],
                      f"restart must re-enter the repair gate for the same candidate, got {json.dumps(restart_result)}")
        after_restart_candidate = read_candidate(run)
        d.assertEqual(before_restart["candidateId"], after_restart_candidate["candidateId"],
                      "restart must resume the SAME candidate (no attempt re-spent)")
        d.assertEqual(before_restart_checkpoint, read_run(run)["activeCheckpointId"],
                      "restart must preserve the active checkpoint")

        wrong_repair_done = {"done": False}

        def repair_for(run_dir, candidate):
            assignment = candidate["fullAssignment"]
            changes = {}
            changes["api"] = "v2" if assignment.get("is-number") == "7.0.0" else "v1"
            changes["feature"] = "beta" if assignment.get("is-finite") == "1.1.0" else "stable"
            if not wrong_repair_done["done"]:
                repair_config(trial_project(run_dir) / "src/config.js", {"api": "wrong-api", "feature": "wrong-feature"})
                wrong_repair_done["done"] = True
            else:
                repair_config(trial_project(run_dir) / "src/config.js", changes)

        finish = drive_until_finish(run, env_b, repair_for=repair_for, feedback_log=feedback_log)
        d.assertEqual("finished", finish["stopped"], f"expected finish, got {json.dumps(finish)}")

        # 5. Assert the driver never fabricated application feedback.
        d.assertTrue(feedback_log and all(k != "INCONCLUSIVE" for k in feedback_log),
                     f"driver must never send INCONCLUSIVE, wrote {feedback_log}")

        # 6. All three targets are upgraded, the wrong repair was exercised, and
        #    the source git stays clean. (A single verified cohort is fine — the
        #    bounded-retry and restart guarantees are the failure-matrix point.)
        final_state = physical.read_runtime_state(run)
        full = final_state["activeCheckpoint"]["fullAssignment"]
        for name, version in new.items():
            d.assertEqual(version, full[name], f"{name} must be upgraded to {version}")
        accepted_deltas = _accepted_delta_chain(run, final_state)
        discovered = set()
        for delta in accepted_deltas:
            discovered.update(delta)
        d.assertEqual(set(old), discovered, "every target must be upgraded across accepted cohorts")
        d.assertTrue(wrong_repair_done["done"], "the wrong-repair rejection must have been exercised")
        for doc in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md"):
            d.assertTrue((run / "reports" / doc).is_file(), f"{doc} must exist")
        d.assertEqual("", physical._run([d.git, "status", "--porcelain"], project).stdout,
                      "source repository must not be modified by the demo")
        summary = {
            "profile": "failure-matrix",
            "infraStop": {"retries": len(materializes), "maxInfraRetries": 2, "checkpointPreserved": "C0"},
            "autoContinueWithoutRetry": False,
            "healedViaRetryInfra": True,
            "stopped": finish["stopped"],
            "feedbackKinds": feedback_log,
        }
        write_json(stage / "DEMO_SUMMARY.json", summary)
        print("DEMO_ACCEPTED " + str(stage / "DEMO_SUMMARY.json"), flush=True)
    finally:
        if os.environ.get("DEPLOOM_DEMO_KEEP_OUTPUT") != "1":
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


def run_cross_group_closure(driver, *, companions_only: bool = False) -> None:
    d = driver
    stage = Path(tempfile.mkdtemp(prefix="demo-closure-"))
    print("DEMO_WORKSPACE " + str(stage), flush=True)
    try:
        old = {"is-string": "1.0.7", "is-symbol": "1.0.4", "is-number": "5.0.0",
               "is-finite": "1.0.0", "is-date-object": "1.0.5", "is-regex": "1.1.4",
               "is-bigint": "1.0.4", "is-map": "2.0.2"}
        new = {"is-string": "1.1.1", "is-symbol": "1.1.1", "is-number": "7.0.0",
               "is-finite": "1.1.0", "is-date-object": "1.1.0", "is-regex": "1.2.1",
               "is-bigint": "1.1.0", "is-map": "2.0.3"}
        if companions_only:
            # Sorted positions 1 and 3 are in DIFFERENT halves; their pair
            # cannot be reached by recursive halving alone. Two noisy targets
            # keep the physical failure matrix small and reproducible.
            fixture_names = {"is-date-object", "is-finite", "is-regex", "is-symbol"}
            old = {n: v for n, v in old.items() if n in fixture_names}
            new = {n: v for n, v in new.items() if n in fixture_names}
        cache = stage / "npm-cache"
        seed_cache(cache, [old, new])
        # a == is-string, h == is-symbol: mutually exclusive (cannot both be at
        # their new versions), each green separately; noisy batch around them.
        contracts = (
            "if (v('is-string')==='1.1.1' && v('is-symbol')==='1.1.1') errors.push('a+h pair must not coexist at new versions');\n"
            + contract_if("is-string", "1.1.1", "is_str", "s2")
            + contract_if("is-string", "1.0.7", "is_str", "s1")
            + contract_if("is-symbol", "1.1.1", "is_sym", "y2")
            + contract_if("is-symbol", "1.0.4", "is_sym", "y1")
            + contract_if("is-number", "7.0.0", "api", "v2")
            + contract_if("is-number", "5.0.0", "api", "v1")
            + contract_if("is-finite", "1.1.0", "feature", "beta")
            + contract_if("is-finite", "1.0.0", "feature", "stable")
            + "".join(version_presence(n, [old[n], new[n]]) for n in old if n not in ("is-string", "is-symbol", "is-number", "is-finite"))
        )
        if companions_only:
            # A genuine joint-update trap: neither companion works alone; all
            # noisy targets are impossible. Only the cross-half PAIR is green.
            contracts = (
                "if ((v('is-finite')==='1.1.0') !== (v('is-symbol')==='1.1.1')) errors.push('companions must upgrade together');\n"
                + "".join(f"if (v({json.dumps(n)})==={json.dumps(new[n])}) errors.push('noisy target blocked: {n}');\n"
                          for n in old if n not in ('is-finite', 'is-symbol'))
                + contract_if("is-finite", "1.1.0", "feature", "beta")
                + contract_if("is-symbol", "1.1.1", "is_sym", "y2")
            )
        project = build_project(
            stage, old,
            {"api": "v1", "feature": "stable", "is_str": "s1", "is_sym": "y1"},
            check_source(contracts),
        )
        verify = stage / "verify-config.json"
        write_json(verify, {
            "commands": ["node check.js"], "projectChecks": "adaptive",
            "timeoutSeconds": 120, "attemptTimeoutSeconds": 1200,
            "snapshotCopyTimeoutSeconds": 600,
        })
        run = stage / "run"
        run.mkdir(parents=True, exist_ok=True)
        env = npm_env(cache)
        feedback_log: list = []
        pair_candidate_ids: set = set()

        begin = run_cli(run, env, "begin", "--project-dir", str(project), "--project-name", "Closure",
                        "--targets-file", str(_targets_file(stage, new)), "--verify-config", str(verify),
                        "--run-budget-minutes", "120", "--phase-timeout-seconds", "1200")
        d.assertEqual("begin.done", begin["last"]["event"])

        def repair_for(run_dir, candidate):
            assignment = candidate["fullAssignment"]
            changes = {}
            changes["api"] = "v2" if assignment.get("is-number") == "7.0.0" else "v1"
            changes["feature"] = "beta" if assignment.get("is-finite") == "1.1.0" else "stable"
            changes["is_str"] = "s1" if assignment.get("is-string") != "1.1.1" else "s2"
            changes["is_sym"] = "y1" if assignment.get("is-symbol") != "1.1.1" else "y2"
            repair_config(trial_project(run_dir) / "src/config.js", changes)

        # Drive until finished, servicing every agent gate with the SCRIPTED
        # external agent. When the planner proposes the combined a+h candidate
        # (which is intrinsically RED: no config can pass the pair rule), the
        # naive agent repairs anyway, attempts exhaust, and the assignment is
        # REJECTED once; the planner then splits to a workable separation.
        for _ in range(100 if companions_only else 30):
            result = run_drive(run, env, timeout=3600)
            if result["stopped"] == "finished":
                break
            if result["stopped"] == "agent-gate":
                candidate = read_candidate(run)
                changed = candidate["delta"]["changed"]
                if {"is-string", "is-symbol"} <= set(changed):
                    pair_candidate_ids.add(candidate["candidateId"])
                attempt = int(candidate.get("attemptId") or 0)
                repair_for(run, candidate)
                feedback_log.append("READY_FOR_VERIFY")
                write_feedback(run, env, kind="READY_FOR_VERIFY", attempt_id=attempt,
                               changed_files=["src/config.js"], reason="config adapted to attempted assignment")
                continue
            if result["stopped"] == "infra-blocked":
                raise RuntimeError(f"unexpected infra stop in closure: {result['reason']}")
            raise RuntimeError(f"drive stopped unexpectedly in closure: {json.dumps(result)}")
        else:
            raise AssertionError("closure drive did not finish within cohort budget")

        final_state = physical.read_runtime_state(run)
        # The durable terminal verdict lives on run.json (terminalOutcome.outcome);
        # the live status may still carry a transient coordination name.
        terminal = str((read_run(run).get("terminalOutcome") or {}).get("outcome") or read_run(run).get("terminal") or "")
        full = final_state["activeCheckpoint"]["fullAssignment"]
        pair = ["is-finite", "is-symbol"] if companions_only else ["is-string", "is-symbol"]
        if companions_only:
            accepted = _accepted_delta_chain(run, final_state)
            d.assertTrue(any(set(delta) == set(pair) for delta in accepted), "joint companion pair was never accepted")
            for name in old:
                d.assertEqual(new[name] if name in pair else old[name], full[name])
            d.assertEqual("PARTIAL_VERIFIED", terminal)
            d.assertTrue(all(k == "READY_FOR_VERIFY" for k in feedback_log))
            summary = {"profile": "cross-group-companions", "terminal": terminal,
                       "acceptedPair": pair, "acceptedCohorts": len(accepted), "feedbackKinds": feedback_log}
            write_json(stage / "DEMO_SUMMARY.json", summary)
            print("DEMO_ACCEPTED " + str(stage / "DEMO_SUMMARY.json"), flush=True)
            return
        for name, version in new.items():
            if name in pair:
                continue
            d.assertEqual(version, full[name], f"{name} must be upgraded to {version}")

        # The a+h rule is an exclusive OR over the whole tree: at most one of the
        # pair may sit at its new version. The planner chooses one (here is-string,
        # accepted first), the other is demonstrably blocked, and the run ends with
        # an HONEST PARTIAL_VERIFIED terminal — never a fabricated COMPLETE.
        upgraded = [n for n in pair if full[n] == new[n]]
        blocked = [n for n in pair if full[n] != new[n]]
        d.assertEqual(1, len(upgraded), f"exactly one of {pair} must be upgraded, got {upgraded}")
        d.assertTrue(blocked, "the other member of the exclusive pair must stay on its old verified version")
        d.assertEqual("PARTIAL_VERIFIED", terminal,
                      f"blocked cross-group member must yield an honest PARTIAL_VERIFIED terminal, got {terminal!r}")

        # The search was never declared exhausted early, and the pair was never
        # accepted together.
        accepted_deltas = _accepted_delta_chain(run, final_state)
        for delta in accepted_deltas:
            d.assertFalse({"is-string", "is-symbol"} <= set(delta),
                          "a+h must never be accepted together")
        discovered = set()
        for delta in accepted_deltas:
            discovered.update(delta)
        d.assertEqual(set(new) - {blocked[0]}, discovered,
                      "every non-blocked package must be upgraded exactly once across accepted checkpoints")
        names_seen: list = []
        for delta in accepted_deltas:
            names_seen.extend(sorted(delta))
        d.assertEqual(len(names_seen), len(set(names_seen)),
                      "no package may be duplicated across accepted checkpoints")

        # The combined pair really was proposed by the planner at least once and
        # then split — that is the closure that must not declare None/skip.
        d.assertGreaterEqual(len(pair_candidate_ids), 1,
                             "the a+h pair must be proposed before it is split (closure exercised)")
        d.assertTrue(feedback_log and all(k == "READY_FOR_VERIFY" for k in feedback_log),
                     f"driver must only send READY_FOR_VERIFY at agent gates, wrote {feedback_log}")

        summary = {
            "profile": "cross-group-closure",
            "pair": pair,
            "pairProposed": len(pair_candidate_ids),
            "acceptedCohorts": len(accepted_deltas),
            "terminal": terminal,
            "upgradedOfPair": upgraded,
            "blockedOfPair": blocked,
            "allTargetsAtNew": {n: full.get(n) for n in new},
            "feedbackKinds": feedback_log,
        }
        write_json(stage / "DEMO_SUMMARY.json", summary)
        print("DEMO_ACCEPTED " + str(stage / "DEMO_SUMMARY.json"), flush=True)
    finally:
        if os.environ.get("DEPLOOM_DEMO_KEEP_OUTPUT") != "1":
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("happy-path-24", "failure-matrix", "cross-group-closure", "cross-group-companions"), default="happy-path-24")
    parser.add_argument("--output-root", default=".dependency-roadmap/iterative-demo")
    parser.add_argument("--packages", type=int, choices=(12, 24), default=24)
    parser.add_argument("--seed-cache", help="Reuse an existing demo npm cache inside this repository")
    parser.add_argument("--keep-output", action="store_true", help="Keep the stage directory for inspection")
    args = parser.parse_args()
    if args.keep_output:
        os.environ["DEPLOOM_DEMO_KEEP_OUTPUT"] = "1"

    cls = physical.IterativeMigrationPhysicalAcceptance
    if args.profile == "happy-path-24":
        cls.setUpClass()
        driver = cls("test_vertical_loop_first_upgrade_repair_and_recovery")
        if args.seed_cache:
            cached = (ROOT / args.seed_cache).resolve()
            if not cached.is_relative_to(ROOT) or not cached.is_dir():
                raise SystemExit("seed-cache must be an existing directory inside this repository")
            cls.cache = cached
            cls.seed_env["npm_config_cache"] = str(cached)
        _happy_path_body(driver, {**OLD, **(EXTRA_OLD if args.packages == 24 else {})},
                         {**NEW, **(EXTRA_NEW if args.packages == 24 else {})},
                         {"is-string", *({"is-weakset"} if args.packages == 24 else set())})
    elif args.profile == "failure-matrix":
        cls.setUpClass()
        run_failure_matrix(cls("test_vertical_loop_first_upgrade_repair_and_recovery"))
    elif args.profile in {"cross-group-closure", "cross-group-companions"}:
        cls.setUpClass()
        run_cross_group_closure(cls("test_vertical_loop_first_upgrade_repair_and_recovery"),
                               companions_only=args.profile == "cross-group-companions")


if __name__ == "__main__":
    main()

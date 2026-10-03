"""Physical acceptance: the Iterative Migration vertical loop with REAL
Git / npm install / check commands through the production adapters.

The npm cache is seeded once in setUpClass with the exact package versions the
fixture needs (real registry, real tarballs).  Every later install in the test
runs with npm_config_offline=true, so the loop is deterministic after the seed;
the seed itself is skipped when the registry is unreachable.

Fixture semantics (matrix items 1, 2, 6, 8, 12-14 of the plan):
  * project.git with dependencies is-number@5.0.0 and is-finite@1.0.0 and a
    real check command `node check.js` that is version-sensitive about
    src/config.js.
  * C0 control verification passes (config api=v1 / feature=stable matches 5.0.0).
  * Candidate A upgrades is-number to 7.0.0: PRECHECK is RED (config api=v1),
    the scripted agent adapts src/config.js to api=v2, verify-exact accepts C1.
  * Candidate B upgrades is-finite to 1.1.0: the scripted agent first produces
    a WRONG fix (feature still stable) -> verify-exact stays RED and opens
    repair attempt 2; the agent then produces the right fix -> C2 accepted.
    C2 contains A and B (the second repair accumulates on the first).
  * Candidate C (is-string@99.99.99, an impossible target) fails to materialize;
    an INCONCLUSIVE feedback frees it; plan-next finds nothing left and the run
    terminates with C2 intact.  A fresh process (restart) still reads C2.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _command(name: str) -> str | None:
    for executable in (name, name + ".cmd", name + ".exe"):
        found = shutil.which(executable)
        if found:
            return found
    return None


def _run(argv, cwd: Path, env=None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
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


@unittest.skipUnless(
    os.name == "nt" or shutil.which("git"),
    "physical iterative migration acceptance needs git",
)
class IterativeMigrationPhysicalAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        npm = _command("npm")
        node = _command("node")
        git = _command("git")
        if not (npm and node and git):
            raise unittest.SkipTest("npm/node/git required for physical acceptance")
        cls.npm = npm
        cls.node = node
        cls.git = git

        stage = Path(tempfile.mkdtemp(prefix="iter-migration-acceptance-"))
        cls.stage = stage
        cls.cache = stage / "npm-cache"
        cls.cache.mkdir(parents=True, exist_ok=True)

        cls.seed_env = dict(os.environ)
        cls.seed_env["npm_config_cache"] = str(cls.cache)
        cls.seed_env["npm_config_audit"] = "false"
        cls.seed_env["npm_config_fund"] = "false"
        cls.seed_env["npm_config_prefer_offline"] = "true"

        # Seed the cache with the EXACT versions the loop needs (registry reach
        # required once).  A cache miss later turns any install flaky, so the
        # seed is the single network dependency of the whole test.
        seed_project = stage / "seed"
        seed_project.mkdir(parents=True, exist_ok=True)
        versions = [
            {"is-number": "5.0.0", "is-finite": "1.0.0"},
            {"is-number": "7.0.0", "is-finite": "1.0.0"},
            {"is-number": "7.0.0", "is-finite": "1.1.0"},
        ]
        for deps in versions:
            manifest = {"name": "seed", "version": "1.0.0", "dependencies": deps}
            (seed_project / "package.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            result = _run(
                [npm, "install", "--no-audit", "--no-fund"],
                seed_project,
                env=cls.seed_env,
                timeout=1800,
            )
            if result.returncode != 0:
                raise unittest.SkipTest(
                    f"npm cache seed failed (registry unreachable?): {result.stderr[-800:]}"
                )

        cls.project = stage / "sample-app"
        cls.project.mkdir(parents=True)
        (cls.project / "src").mkdir(parents=True)
        (cls.project / "src" / "config.js").write_text(
            "module.exports = { api: 'v1', feature: 'stable' };\n",
            encoding="utf-8",
        )
        (cls.project / "check.js").write_text(_CHECK_SOURCE, encoding="utf-8")
        (cls.project / "package.json").write_text(
            json.dumps(
                {
                    "name": "sample-app",
                    "version": "1.0.0",
                    "private": True,
                    "scripts": {"test": "node check.js"},
                    "dependencies": {
                        "is-number": "5.0.0",
                        "is-finite": "1.0.0",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        git_env = dict(os.environ)
        git_env.update(
            {
                "GIT_AUTHOR_NAME": "iter-migration",
                "GIT_AUTHOR_EMAIL": "iter@example.test",
                "GIT_COMMITTER_NAME": "iter-migration",
                "GIT_COMMITTER_EMAIL": "iter@example.test",
            }
        )
        # The verifier requires a canonical lockfile in the source project (like
        # every real repo).  Generate it OFFLINE from the seeded cache so the
        # fixture stays hermetic after the seed.
        lock_result = _run(
            [npm, "install", "--package-lock-only", "--no-audit", "--no-fund"],
            cls.project,
            env=cls.seed_env,
            timeout=1800,
        )
        if lock_result.returncode != 0:
            raise unittest.SkipTest(
                f"offline lockfile generation failed: {lock_result.stderr[-800:]}"
            )
        for argv in (
            [git, "init", "-q"],
            [git, "add", "-A"],
            [git, "commit", "-q", "-m", "fixture"],
        ):
            result = _run(argv, cls.project, env=git_env, timeout=300)
            if result.returncode != 0:
                raise unittest.SkipTest(f"git fixture failed: {result.stderr[-500:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "stage"):
            shutil.rmtree(cls.stage, ignore_errors=True)

    # -- CLI driver ---------------------------------------------------------

    def _run_cli(self, run_dir: Path, *args: str, timeout: int = 2400) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "npm_config_cache": str(self.cache),
                "npm_config_offline": "true",
                "npm_config_audit": "false",
                "npm_config_fund": "false",
                "npm_config_prefer_offline": "true",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        command = [sys.executable, str(ROOT / "iterative_migration.py"), "--run-dir", str(run_dir), *args]
        result = _run(command, ROOT, env=env, timeout=timeout)
        events = parse_events(result.stdout)
        failure = parse_failure(result.stdout or result.stderr)
        payload = {
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "events": events,
            "failure": failure,
            "last": events[-1] if events else None,
        }
        if result.returncode not in (0, 2, 4, 130) and not failure:
            self.fail(
                f"iterative CLI {args} failed rc={result.returncode}\n"
                f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
            )
        return payload

    def _targets_file(self, targets: dict) -> Path:
        path = self.stage / f"targets-{len(os.listdir(self.stage))}.json"
        path.write_text(json.dumps(targets), encoding="utf-8")
        return path

    def _verify_config_file(self) -> Path:
        path = self.stage / "verify-config.json"
        if not path.exists():
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
        return path

    def _trial_project(self, run_dir: Path) -> Path:
        candidate = json.loads((run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))
        refs = candidate["materializationRefs"]
        return Path(refs["workspaceRoot"]) / refs["projectRelative"]

    def _repair_requests(self, run_dir: Path) -> list[dict]:
        path = run_dir / "repair-requests.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["requests"]

    def _write_feedback(
        self, run_dir: Path, kind: str, *, changed_files=None, attempt_id=0, reason=""
    ) -> Path:
        candidate = json.loads((run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))
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
        path = self.stage / f"feedback-{len(os.listdir(self.stage))}-{attempt_id}.json"
        path.write_text(json.dumps(feedback), encoding="utf-8")
        return path

    def _scripted_repair(self, run_dir: Path, *, api=None, feature=None) -> None:
        config_path = self._trial_project(run_dir) / "src" / "config.js"
        text = config_path.read_text(encoding="utf-8")
        if api is not None:
            text = re.sub(
                r"(api:\s*['\"])[^'\"]*(['\"])",
                lambda m: m.group(1) + str(api) + m.group(2),
                text,
            )
        if feature is not None:
            text = re.sub(
                r"(feature:\s*['\"])[^'\"]*(['\"])",
                lambda m: m.group(1) + str(feature) + m.group(2),
                text,
            )
        config_path.write_text(text, encoding="utf-8")

    def _repair_for_assignment(self, run_dir: Path, assignment: dict) -> None:
        """Adapt src/config.js to the check contract for THIS exact assignment."""
        api = feature = None
        if assignment.get("is-number") == "7.0.0":
            api = "v2"
        elif assignment.get("is-number") == "5.0.0":
            api = "v1"
        if assignment.get("is-finite") == "1.1.0":
            feature = "beta"
        elif assignment.get("is-finite") == "1.0.0":
            feature = "stable"
        self._scripted_repair(run_dir, api=api, feature=feature)

    # -- tests --------------------------------------------------------------

    def test_vertical_loop_first_upgrade_repair_and_recovery(self) -> None:
        run_dir = self.stage / "run1"
        run_dir.mkdir(parents=True)

        # begin: C0 «Исходный замер» control verification must PASS.
        begin = self._run_cli(
            run_dir,
            "begin",
            "--project-dir",
            str(self.project),
            "--project-name",
            "sample-app",
            "--target-level",
            "yellow",
            "--targets-file",
            str(self._targets_file({"is-number": "7.0.0", "is-finite": "1.1.0", "is-string": "99.99.99"})),
            "--verify-config",
            str(self._verify_config_file()),
            "--cohort-max-packages",
            "1",  # This scenario explicitly tests two separate cumulative repairs.
            "--run-budget-minutes",
            "30",
            "--phase-timeout-seconds",
            "1200",
        )

        # ------------------------------------------------------------------
        # The relaxed planner picks the next best cohort itself, so the loop
        # is driven by whatever candidate each plan-next returns.  Every CLI
        # step is a FRESH process (restart boundary).
        # ------------------------------------------------------------------

        def read_candidate() -> dict:
            return json.loads((run_dir / "trial" / "candidate.json").read_text(encoding="utf-8"))

        def run_vertical(prefix: str, wrong_then_right: bool):
            """One full vertical loop for the ACTIVE planned candidate.
            Returns (accepted checkpoint id, candidate)."""
            plan = self._run_cli(run_dir, "plan-next")
            self.assertEqual("plan-next.candidate", plan["last"]["event"], plan["stdout"][-3000:])
            candidate = read_candidate()
            changed = candidate["delta"]["changed"]
            self.assertEqual(1, len(changed))
            pkg = next(iter(changed))
            target = changed[pkg]
            self.assertIn(target, ("7.0.0", "1.1.0"), f"{prefix}: unseeded target {pkg}@{target}")

            materialize = self._run_cli(run_dir, "materialize", "--timeout-seconds", "1200")
            self.assertEqual("materialize.done", materialize["last"]["event"], materialize["stdout"][-3000:])
            trial = self._trial_project(run_dir)
            installed = json.loads(
                (trial / "node_modules" / pkg / "package.json").read_text(encoding="utf-8")
            )
            self.assertEqual(target, installed["version"], f"{prefix}: trial did not hold the exact version")

            precheck = self._run_cli(run_dir, "precheck", "--timeout-seconds", "1200")
            self.assertEqual("precheck.repair-required", precheck["last"]["event"], precheck["stdout"][-3000:])
            requests = self._repair_requests(run_dir)
            self.assertEqual(
                1,
                len([r for r in requests if r["candidateId"] == candidate["candidateId"] and r["attemptId"] == 0]),
                f"{prefix}: expected a repair request for attempt 0",
            )
            self.assertEqual("node check.js", requests[-1]["failingCommands"][0]["command"])

            if wrong_then_right:
                # Scripted agent returns a WRONG fix first: verify-exact stays
                # RED and opens repair attempt 2.
                self._scripted_repair(run_dir, api="wrong-api", feature="wrong-feature")
                bad_feedback = self._write_feedback(
                    run_dir, "READY_FOR_VERIFY", changed_files=["src/config.js"], attempt_id=0,
                    reason="wrong adaptation",
                )
                self._run_cli(run_dir, "apply-feedback", "--feedback-file", str(bad_feedback))
                verify_bad = self._run_cli(run_dir, "verify-exact")
                self.assertEqual("verify-exact.result", verify_bad["last"]["event"], verify_bad["stdout"][-3000:])
                self.assertFalse(verify_bad["last"]["ok"])
                self.assertEqual("project", verify_bad["last"]["kind"])
                candidate_after = read_candidate()
                self.assertEqual(1, candidate_after["attemptId"], f"{prefix}: attempt should advance")
                self.assertEqual(
                    1,
                    len([r for r in self._repair_requests(run_dir)
                         if r["candidateId"] == candidate["candidateId"] and r["attemptId"] == 1]),
                    f"{prefix}: expected a repair request for attempt 1; "
                    f"requests={json.dumps(self._repair_requests(run_dir))}",
                )
                attempt = 1
            else:
                attempt = 0

            # Correct repair for THIS exact assignment, then verify-exact.
            self._repair_for_assignment(run_dir, candidate["fullAssignment"])
            feedback = self._write_feedback(
                run_dir, "READY_FOR_VERIFY", changed_files=["src/config.js"],
                attempt_id=attempt,
                reason="adapted config to the actual upgrade",
            )
            applied = self._run_cli(run_dir, "apply-feedback", "--feedback-file", str(feedback))
            self.assertEqual("feedback.accepted", applied["last"]["event"], applied["stdout"][-2000:])
            verify = self._run_cli(run_dir, "verify-exact")
            self.assertEqual("checkpoint.accepted", verify["last"]["event"], verify["stdout"][-3000:])
            accepted_id = verify["last"]["checkpointId"]
            state = read_runtime_state(run_dir)
            self.assertEqual(accepted_id, state["activeCheckpointId"])
            self.assertEqual(
                candidate["delta"]["changed"],
                state["activeCheckpoint"]["acceptedDelta"]["changed"],
                f"{prefix}: accepted delta must equal the candidate delta",
            )
            return accepted_id, candidate

        # C0 («Исходный замер») — control verification of the untouched repo.
        self.assertEqual("begin.done", begin["last"]["event"], begin["stdout"][-3000:])
        self.assertEqual("C0", begin["last"]["checkpointId"])
        active_c0 = read_runtime_state(run_dir)
        self.assertEqual("VERIFIED", active_c0["activeCheckpoint"]["status"])
        self.assertEqual("5.0.0", active_c0["activeCheckpoint"]["fullAssignment"]["is-number"])
        self.assertEqual("1.0.0", active_c0["activeCheckpoint"]["fullAssignment"]["is-finite"])

        # First cohort: full vertical loop, repair on attempt 0 -> C1.
        c1, candidate1 = run_vertical("first-cohort", wrong_then_right=False)
        self.assertEqual("C1", c1)
        status_after_c1 = read_runtime_state(run_dir)
        # The repair bytes are part of C1's immutable source snapshot.
        self.assertNotEqual(
            status_after_c1["activeCheckpoint"]["sourceSnapshotKey"],
            active_c0["activeCheckpoint"]["sourceSnapshotKey"],
        )
        accepted1 = status_after_c1["activeCheckpoint"]["acceptedDelta"]["changed"]
        self.assertEqual(1, len(accepted1))
        # No dangled repair request for the accepted candidate.
        self.assertEqual([], self._repair_requests(run_dir))
        self.assertEqual("C0", status_after_c1["activeCheckpoint"]["parentCheckpointId"])

        # Second cohort: built ON TOP of C1 — a wrong fix first, then the right
        # one (repair attempts advance); C2 accumulates BOTH upgrades.
        c2, candidate2 = run_vertical("second-cohort", wrong_then_right=True)
        self.assertEqual("C2", c2)
        self.assertEqual("C1", candidate2["baseCheckpointId"])
        status_after_c2 = read_runtime_state(run_dir)
        full = status_after_c2["activeCheckpoint"]["fullAssignment"]
        self.assertEqual("7.0.0", full["is-number"])
        self.assertEqual("1.1.0", full["is-finite"])

        # Drain any remaining candidates (the impossible is-string target):
        # materialization fails deterministically, INCONCLUSIVE frees it, and
        # plan-next eventually reports nothing actionable.  C2 stays intact.
        drained_impossible = 0
        plan_end = None
        for _ in range(6):
            plan_drain = self._run_cli(run_dir, "plan-next")
            if plan_drain["last"]["event"] == "plan-next.no-candidate":
                plan_end = plan_drain["last"]
                break
            candidate_drain = read_candidate()
            changed_drain = candidate_drain["delta"]["changed"]
            impossible = [n for n, v in changed_drain.items() if v == "99.99.99"]
            self.assertEqual(len(changed_drain), len(impossible),
                             "drain phase must only schedule the impossible target")
            self.assertLess(drained_impossible, 1, "impossible target must not be re-proposed inside one drain")
            materialize_drain = self._run_cli(run_dir, "materialize", "--timeout-seconds", "1200")
            self.assertEqual("materialize.failed", materialize_drain["last"]["event"], materialize_drain["stdout"][-3000:])
            self.assertEqual("resolver", materialize_drain["last"]["kind"])
            feedback_drain = self._write_feedback(
                run_dir, "INCONCLUSIVE", reason="target version does not exist"
            )
            feedback_result = self._run_cli(run_dir, "apply-feedback", "--feedback-file", str(feedback_drain))
            self.assertEqual("feedback.accepted", feedback_result["last"]["event"], feedback_result["stdout"][-2000:])
            drained_impossible += 1
        else:
            self.fail("plan-next never reached no-candidate during drain")
        self.assertIsNotNone(plan_end, "drain must end with plan-next.no-candidate")
        # The impossible target is NOT a satisfied policy goal: terminal is
        # NO_ACTIONABLE (deferred stays in the remainder), never COMPLETE.
        self.assertEqual("NO_ACTIONABLE", plan_end["reason"])

        # A fresh process (restart) reads the SAME durable state: C2 intact.
        status_restart = read_runtime_state(run_dir)
        self.assertEqual("C2", status_restart["activeCheckpointId"])
        self.assertEqual("7.0.0", status_restart["activeCheckpoint"]["fullAssignment"]["is-number"])
        self.assertEqual("1.1.0", status_restart["activeCheckpoint"]["fullAssignment"]["is-finite"])

        # finish emits the two developer documents.
        finished = self._run_cli(run_dir, "finish")
        self.assertIsNotNone(
            finished["last"],
            f"finish gave no status event: rc={finished['returncode']} "
            f"stdout={finished['stdout'][-2500:]}\nstderr={finished['stderr'][-1500:]}",
        )
        self.assertEqual("finish.done", finished["last"]["event"])
        reports = run_dir / "reports"
        self.assertTrue((reports / "MIGRATION_REPORT.md").exists())
        self.assertTrue((reports / "DEVELOPER_UPGRADE_GUIDE.md").exists())
        guide = (reports / "DEVELOPER_UPGRADE_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("is-number", guide)
        self.assertIn("is-finite", guide)


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


def parse_failure(stdout: str) -> dict | None:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("ITERATIVE_MIGRATION_FAILURE_V1 "):
            try:
                return json.loads(line[len("ITERATIVE_MIGRATION_FAILURE_V1 "):])
            except (ValueError, TypeError):
                return {"code": "PARSE", "summary": line}
    return None


def read_runtime_state(run_dir: Path) -> dict:
    """Current durable state without spawning the CLI: run.json + active
    checkpoint (status.json is only produced by the `status` subcommand)."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    checkpoint_path = run_dir / "checkpoints" / f"{run['activeCheckpointId']}.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists()
        else None
    )
    state = dict(run)
    state["activeCheckpoint"] = checkpoint
    return state


if __name__ == "__main__":
    unittest.main()

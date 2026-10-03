"""Explicit migration validation profiles and fail-closed Vitest evidence.

A successful comparison proves the configured predicate, not an entirely green
project. Baselines are sealed by the initial isolated control and hash-bound in
commands, so project proof identities cannot reuse a different failure set.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid


class EvidenceError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def normalize_vitest(raw, project):
    if not isinstance(raw, dict) or not isinstance(raw.get("testResults"), list):
        raise EvidenceError("VITEST_REPORT_INVALID")
    if raw.get("numRuntimeErrorTestSuites", 0) or raw.get("unhandledErrors"):
        raise EvidenceError("VITEST_RUNTIME_ERRORS")
    cases = {}
    failures = 0
    for suite in raw["testResults"]:
        assertions = suite.get("assertionResults")
        if not isinstance(assertions, list) or not assertions:
            raise EvidenceError("VITEST_COLLECTION_FAILED")
        name = str(suite.get("name", ""))
        try:
            name = Path(name).resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            raise EvidenceError("VITEST_SUITE_OUTSIDE_PROJECT")
        suite_failures = 0
        for item in assertions:
            # Older Vitest prefixes root-level fullName with a space. It is
            # not part of the case identity and must survive runner upgrades.
            title = str(item.get("fullName") or " ".join([*item.get("ancestorTitles", []), item.get("title", "")])).strip()
            key = name + "::" + str(title)
            status = item.get("status")
            if not title or key in cases or status not in {"passed", "failed", "pending", "skipped", "todo", "disabled"}:
                raise EvidenceError("VITEST_CASE_ID_OR_STATUS_INVALID")
            messages = item.get("failureMessages", [])
            if status == "failed" and (not isinstance(messages, list) or not messages):
                raise EvidenceError("VITEST_FAILURE_EVIDENCE_MISSING")
            normalized = [re.sub(r"\x1b\[[0-9;]*m", "", str(m)).replace(str(project.resolve()), "<project>").replace(project.resolve().as_posix(), "<project>") for m in messages]
            cases[key] = {"status": status, "failureHash": digest(normalized) if status == "failed" else "", "failureMessages": normalized if status == "failed" else []}
            suite_failures += status == "failed"
        if suite.get("status") == "failed" and not suite_failures:
            raise EvidenceError("VITEST_COLLECTION_FAILED")
        failures += suite_failures
    if not cases or len(cases) != raw.get("numTotalTests") or failures != raw.get("numFailedTests"):
        raise EvidenceError("VITEST_COVERAGE_OR_COUNTERS_INVALID")
    if not any(c["status"] in {"passed", "failed"} for c in cases.values()):
        raise EvidenceError("VITEST_NO_EXECUTED_TESTS")
    return {"schemaVersion": 1, "runner": "vitest", "cases": cases}


def compare(before, after):
    old, new = before["cases"], after["cases"]
    missing = sorted(set(old) - set(new))
    if missing:
        raise EvidenceError("TEST_COVERAGE_LOST: " + ", ".join(missing[:5]))
    for key, case in new.items():
        previous = old.get(key)
        if case["status"] == "failed" and (not previous or previous != case):
            raise EvidenceError("TEST_REGRESSION: " + key)
        if previous and previous["status"] in {"passed", "failed"} and case["status"] not in {"passed", "failed"}:
            raise EvidenceError("TEST_BECAME_SKIPPED: " + key)
    return {"total": len(new), "existingFailures": sum(c["status"] == "failed" for c in new.values()), "passed": sum(c["status"] == "passed" for c in new.values()), "skipped": sum(c["status"] not in {"passed", "failed"} for c in new.values())}


def command_line(args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def evidence_command(command, *, observation=None, baseline=None, baseline_hash=None):
    args = [sys.executable, str(Path(__file__).resolve()), "run", "--command", command]
    if observation:
        args += ["--observation", str(observation)]
    if baseline:
        args += ["--baseline", str(baseline), "--baseline-hash", baseline_hash]
    # An internal protocol is decoded to a direct argv by the supervisor.
    # Do not put nested quoted commands through cmd.exe on Windows.
    payload = base64.urlsafe_b64encode(json.dumps(args[2:]).encode()).decode()
    return "deploom-vitest-v1:" + payload


def internal_command_argv(command):
    prefix = "deploom-vitest-v1:"
    if not command.startswith(prefix):
        return None
    try:
        args = json.loads(base64.urlsafe_b64decode(command[len(prefix):]).decode())
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not args or args[0] != "run":
            raise EvidenceError("VALIDATION_COMMAND_INVALID")
        return [sys.executable, str(Path(__file__).resolve()), *args]
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("VALIDATION_COMMAND_INVALID") from exc


def display_validation_command(command):
    argv = internal_command_argv(command)
    if argv is None:
        return command
    return argv[argv.index("--command") + 1] + " [Vitest comparison]"


def validate_profile(raw):
    if not isinstance(raw, dict):
        raise ValueError("VALIDATION_PROFILE_INVALID")
    commands = raw.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 20 or any(not isinstance(c, str) or not c.strip() or "\n" in c or "\r" in c for c in commands):
        raise ValueError("VALIDATION_COMMANDS_REQUIRED")
    commands = [c.strip() for c in commands]
    unit = str(raw.get("unitCommand") or "").strip()
    allow = raw.get("compareExistingFailures") is True
    if allow and (unit not in commands or any(token in unit for token in ["&&", "||", ";", "|", "\n", "--reporter", "--outputFile"])):
        raise ValueError("VITEST_COMMAND_MUST_BE_A_SINGLE_SELECTED_COMMAND")
    deferred = raw.get("deferredChecks", "")
    if not isinstance(deferred, str) or len(deferred) > 4000:
        raise ValueError("DEFERRED_CHECKS_INVALID")
    return {"commands": [c.strip() for c in commands], "unitCommand": unit, "compareExistingFailures": allow, "deferredChecks": deferred.strip()}


def validate_baseline_reference(mapping):
    reference = mapping.get("testBaseline")
    if reference:
        try:
            baseline = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
            if digest(baseline) != reference["hash"]:
                raise EvidenceError("TEST_BASELINE_HASH_MISMATCH")
        except (OSError, KeyError, ValueError) as exc:
            raise EvidenceError(f"TEST_BASELINE_UNAVAILABLE: {exc}") from exc


def verify_initial_control(project, assignment, *, run_config, run_dir, verify_config, verifier, **kwargs):
    """Capture never grants authority. Re-run against a sealed comparison before
    accepting C0; any other failed command/unknown evidence keeps the red gate.
    """
    import dataclasses
    profile = run_config.get("validationProfile")
    if run_config.get("repairOnly") or not profile or not profile.get("compareExistingFailures"):
        return verifier(project, assignment, config=verify_config, **kwargs)
    unit = profile["unitCommand"]
    evidence_dir = run_dir / "validation-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    observation = evidence_dir / (uuid.uuid4().hex + ".json")
    capture_command = evidence_command(unit, observation=observation)
    capture_config = dataclasses.replace(verify_config, commands=tuple(capture_command if c == unit else c for c in verify_config.commands))
    result = verifier(project, assignment, config=capture_config, **kwargs)
    if not result.ok and (result.kind != "project" or not result.project_failures or any(f.command != capture_command for f in result.project_failures)):
        return result
    try:
        baseline = json.loads(observation.read_text(encoding="utf-8"))
        counts = compare(baseline, baseline)
    except (OSError, ValueError, KeyError) as exc:
        # Missing/invalid observation cannot turn a collection or launch error
        # into an accepted failure baseline.
        return dataclasses.replace(result, ok=False, kind="infrastructure", project_failures=(), summary=f"TEST_BASELINE_UNAVAILABLE: {exc}")
    baseline["initialResolvedStateKey"] = result.resolved_state_key
    baseline["initialPreparationProofKey"] = result.preparation_proof_key
    baseline["profileHash"] = digest(profile)
    baseline_hash = digest(baseline)
    baseline_path = evidence_dir / (baseline_hash + ".json")
    if not baseline_path.exists():
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    compare_command = evidence_command(unit, baseline=baseline_path, baseline_hash=baseline_hash)
    commands = [compare_command if c == unit else c for c in verify_config.commands]
    run_config["verifyConfig"]["commands"] = commands
    run_config["verifyConfig"]["testBaseline"] = {"path": str(baseline_path), "hash": baseline_hash}
    run_config["validationScope"] = {"mode": "test-nonregression", "baselinePath": str(baseline_path), "baselineHash": baseline_hash, **counts, "deferredChecks": profile.get("deferredChecks", ""), "commands": profile["commands"]}
    # The hash is in the command and therefore in the project proof identity.
    return verifier(project, assignment, config=dataclasses.replace(verify_config, commands=tuple(commands)), **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run"])
    parser.add_argument("--command", required=True)
    parser.add_argument("--observation")
    parser.add_argument("--baseline")
    parser.add_argument("--baseline-hash")
    args = parser.parse_args()
    project = Path.cwd()
    report = project / (".deploom-vitest-" + uuid.uuid4().hex + ".json")
    try:
        if args.baseline:
            before = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            if digest(before) != args.baseline_hash:
                raise EvidenceError("TEST_BASELINE_HASH_MISMATCH")
        from baseline_constraint_verifier import project_check_command_argv
        command = args.command + " --reporter=json --outputFile=" + command_line([report.name])
        # The outer authoritative supervisor owns this child and descendants.
        result = subprocess.run(project_check_command_argv(command), env={**os.environ, "CI": "1"}, check=False)
        raw = json.loads(report.read_text(encoding="utf-8"))
        after = normalize_vitest(raw, project)
        failed = any(c["status"] == "failed" for c in after["cases"].values())
        if result.returncode not in (0, 1) or bool(result.returncode) != failed:
            raise EvidenceError("VITEST_EXIT_REPORT_MISMATCH")
        if args.observation:
            Path(args.observation).write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
            return result.returncode
        counts = compare(before, after)
        evidence = {"baselineHash": args.baseline_hash, **counts, "mode": "test-nonregression"}
        destination = Path(args.baseline).parent / ("comparison-" + uuid.uuid4().hex + ".json")
        destination.write_text(json.dumps({"summary": evidence, "observed": after}, ensure_ascii=False), encoding="utf-8")
        print("MIGRATION_TEST_EVIDENCE_V1 " + json.dumps({**evidence, "evidence": str(destination)}))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print("VALIDATION_EVIDENCE_UNAVAILABLE: " + str(exc), file=sys.stderr)
        return 1 if str(exc).startswith(("TEST_REGRESSION:", "TEST_COVERAGE_LOST:", "TEST_BECAME_SKIPPED:")) else 2
    finally:
        report.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

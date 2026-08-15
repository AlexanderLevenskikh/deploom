#!/usr/bin/env python3
"""Validate durable, isolated dependency-regression test evidence.

The dependency update prompt is not the gate. This script is. It rejects runs
that merely report a green generic test command without package-level coverage,
a baseline/post-update result, and an isolated regression command.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from validate_dependency_update import read_json, read_scope_manifest, section_for_kind
from cli_io import configure_utf8_stdio

BUILD_TOOL_TOKENS = (
    "typescript", "vite", "webpack", "rollup", "esbuild", "swc", "babel",
    "jest", "vitest", "playwright", "cypress", "storybook", "eslint",
    "stylelint", "postcss", "sass", "less", "ts-jest", "lint-staged", "husky",
)
PASS_VALUES = {"passed", "pass", "ok", "success", "green"}
CHANGE_POLICY = "minimal-compatibility-only"
TEST_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
TEST_DECLARATION_RE = re.compile(r"\b(?:test|it)\s*\(")
ASSERTION_RE = re.compile(r"\b(?:expect|assert(?:Equal|True|False|Raises)?|should)\s*[.(]")
SKIP_OR_ONLY_RE = re.compile(r"\b(?:test|it|describe)\s*\.\s*(?:skip|only)\b")
TAUTOLOGY_PATTERNS = (
    re.compile(r"expect\s*\(\s*true\s*\)\s*\.\s*toBe\s*\(\s*true\s*\)"),
    re.compile(r"expect\s*\(\s*false\s*\)\s*\.\s*toBe\s*\(\s*false\s*\)"),
    re.compile(r"expect\s*\(\s*([0-9]+|['\"][^'\"]*['\"])\s*\)\s*\.\s*toBe\s*\(\s*\1\s*\)"),
)


def infer_test_policy(row: Dict[str, Any]) -> str:
    explicit = str(row.get("testPolicy") or row.get("test_policy") or "").strip().lower()
    if explicit:
        return explicit
    name = str(row.get("package") or row.get("name") or "").lower()
    kind = str(row.get("kind") or "")
    # Display groups are presentation only; regression policy must be derived
    # from the package/section itself so changing a dashboard group cannot alter
    # verification requirements.
    required = kind in {"runtime", "optional", "peer"} or any(t in name for t in BUILD_TOOL_TOKENS)
    return "required" if required else "not-required-allowed"


def script_name_from_command(command: str) -> Optional[str]:
    value = str(command or "").strip()
    patterns = (
        r"^(?:yarn|pnpm)\s+([A-Za-z0-9:_-]+)(?:\s|$)",
        r"^npm\s+run\s+([A-Za-z0-9:_-]+)(?:\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None


def inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def normalized_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def read_text_safely(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def package_test_slug(package: str) -> str:
    value = str(package or "").strip().lower().lstrip("@").replace("/", "-")
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value or "package"


def is_umbrella_generated_test_path(relative: str) -> bool:
    name = Path(str(relative)).name.lower()
    return bool(
        re.search(r"(?:^|[-_.])group[-_.]?\d+(?:[-_.]|$)", name)
        or "runtime-libraries" in name
        or "dependency-group" in name
        or "all-dependencies" in name
    )


def finding(code: str, package: str, detail: str, severity: str = "error") -> Dict[str, str]:
    return {"severity": severity, "code": code, "package": package, "detail": detail}


def validate(project_dir: Path, scope_path: Path, evidence_path: Path, project: Optional[str]) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    rows, scope_meta = read_scope_manifest(scope_path, project)
    evidence = read_json(evidence_path)
    findings: List[Dict[str, str]] = []

    actions = [row for row in rows if bool(row.get("shouldUpdate"))]
    required_rows = [row for row in actions if infer_test_policy(row) == "required"]

    regression_dir_text = str(evidence.get("regressionDirectory") or "").strip()
    regression_command = str(evidence.get("regressionTestCommand") or "").strip()
    default_command = str(evidence.get("defaultTestCommand") or "").strip()
    regression_dir = (project_dir / regression_dir_text).resolve() if regression_dir_text else None

    if not regression_dir_text:
        findings.append(finding("regression-directory-missing", "<suite>", "evidence.regressionDirectory is required"))
    elif not regression_dir or not regression_dir.exists() or not regression_dir.is_dir():
        findings.append(finding("regression-directory-not-found", "<suite>", f"directory does not exist: {regression_dir_text}"))

    if not regression_command:
        findings.append(finding("regression-command-missing", "<suite>", "evidence.regressionTestCommand is required"))
    if not default_command:
        findings.append(finding("default-command-missing", "<suite>", "evidence.defaultTestCommand is required"))
    if regression_command and default_command and regression_command == default_command:
        findings.append(finding("regression-command-not-isolated", "<suite>", "default and regression commands are identical"))
    if evidence.get("includedInDefaultTest") is not False:
        findings.append(finding("default-test-inclusion-not-proven", "<suite>", "includedInDefaultTest must be exactly false"))
    if str(evidence.get("changePolicy") or "").strip() != CHANGE_POLICY:
        findings.append(finding("change-policy-missing", "<suite>", f"changePolicy must be exactly {CHANGE_POLICY!r}"))
    if evidence.get("refactoringPerformed") is not False:
        findings.append(finding("refactoring-not-disproved", "<suite>", "refactoringPerformed must be exactly false; broader refactoring requires a separate task"))

    default_collected = evidence.get("defaultCollectedRegressionFiles")
    if not isinstance(default_collected, list):
        findings.append(finding("default-collection-proof-missing", "<suite>", "defaultCollectedRegressionFiles[] is required"))
    elif default_collected:
        findings.append(finding("regression-files-in-default-test", "<suite>", f"ordinary test collected regression files: {default_collected}"))

    regression_collected = evidence.get("regressionCollectedFiles")
    collected_paths: set[str] = set()
    if not isinstance(regression_collected, list) or not regression_collected:
        findings.append(finding("regression-collection-empty", "<suite>", "regressionCollectedFiles[] must contain at least one executed regression file"))
    else:
        for relative in regression_collected:
            candidate = (project_dir / str(relative)).resolve()
            collected_paths.add(normalized_relative(candidate, project_dir))
            if not candidate.exists() or not candidate.is_file():
                findings.append(finding("collected-test-file-not-found", "<suite>", f"executed regression file does not exist: {relative}"))
            elif regression_dir and not inside(candidate, regression_dir):
                findings.append(finding("collected-test-not-isolated", "<suite>", f"executed regression file is outside regressionDirectory: {relative}"))

    package_json_path = project_dir / "package.json"
    scripts: Dict[str, Any] = {}
    if package_json_path.exists():
        package_json = read_json(package_json_path)
        scripts = package_json.get("scripts") or {}
    else:
        findings.append(finding("package-json-missing", "<suite>", f"package.json not found: {package_json_path}"))

    regression_script = script_name_from_command(regression_command)
    default_script = script_name_from_command(default_command)
    if not regression_script:
        findings.append(finding("regression-script-unparseable", "<suite>", "use a dedicated `yarn <script>`, `pnpm <script>`, or `npm run <script>` command"))
    elif regression_script not in scripts:
        findings.append(finding("regression-script-missing", "<suite>", f"package.json scripts has no {regression_script!r}"))
    if default_script and regression_script and default_script == regression_script:
        findings.append(finding("regression-script-not-distinct", "<suite>", "ordinary and regression commands resolve to the same package script"))

    raw_packages = evidence.get("packages")
    if not isinstance(raw_packages, list):
        raw_packages = []
        findings.append(finding("package-evidence-missing", "<suite>", "evidence.packages[] is required"))

    evidence_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    generated_file_owners: Dict[str, set[str]] = {}
    for item in raw_packages:
        if not isinstance(item, dict):
            continue
        name = str(item.get("package") or item.get("name") or "")
        section = str(item.get("section") or "")
        if name:
            evidence_by_key[(section, name)] = item
            if not section:
                evidence_by_key[("", name)] = item

    for row in required_rows:
        name = str(row.get("package") or row.get("name") or "")
        section = str(row.get("section") or section_for_kind(str(row.get("kind") or "")))
        item = evidence_by_key.get((section, name)) or evidence_by_key.get(("", name))
        if not item:
            findings.append(finding("required-package-uncovered", name, f"no evidence entry for {section}:{name}"))
            continue
        if item.get("testRequired") is not True:
            findings.append(finding("required-policy-downgraded", name, "testRequired must be exactly true for a required manifest row"))
        invariant = str(item.get("invariant") or item.get("coverage") or "").strip()
        if len(invariant) < 12:
            findings.append(finding("invariant-missing", name, "describe the concrete behavior/config invariant this gate protects"))

        origin = str(item.get("testOrigin") or "").strip().lower()
        if origin not in {"existing", "generated"}:
            findings.append(finding("test-origin-invalid", name, "testOrigin must be `existing` or `generated`"))
        gate_type = str(item.get("gateType") or "").strip().lower()
        if gate_type not in {"test", "command"}:
            findings.append(finding("gate-type-invalid", name, "gateType must be `test` or `command`"))
        relevance = str(item.get("relevanceProof") or "").strip()
        if len(relevance) < 20:
            findings.append(finding("relevance-proof-missing", name, "explain which real package usage is exercised and how the gate fails on regression"))

        usage_files = item.get("usageFiles")
        usage_mentions_package = False
        if not isinstance(usage_files, list) or not usage_files:
            findings.append(finding("usage-files-missing", name, "usageFiles[] must point to real package usage/config in the project"))
        else:
            for relative in usage_files:
                candidate = (project_dir / str(relative)).resolve()
                if not candidate.exists() or not candidate.is_file():
                    findings.append(finding("usage-file-not-found", name, f"missing usage file: {relative}"))
                    continue
                if name.lower() in read_text_safely(candidate).lower():
                    usage_mentions_package = True
            action = str(row.get("action") or "update").strip().lower()
            if action != "remove" and not usage_mentions_package:
                findings.append(finding("package-usage-not-proven", name, "none of usageFiles contains the direct package name/import/config reference"))

        test_files = item.get("testFiles")
        test_file_texts: List[Tuple[str, str]] = []
        if not isinstance(test_files, list) or not test_files:
            findings.append(finding("test-files-missing", name, "testFiles[] must list durable regression files"))
        else:
            for relative in test_files:
                candidate = (project_dir / str(relative)).resolve()
                normalized = normalized_relative(candidate, project_dir)
                if not candidate.exists() or not candidate.is_file():
                    findings.append(finding("test-file-not-found", name, f"missing test file: {relative}"))
                    continue
                if regression_dir and not inside(candidate, regression_dir):
                    findings.append(finding("test-file-not-isolated", name, f"test file is outside regressionDirectory: {relative}"))
                if collected_paths and normalized not in collected_paths:
                    findings.append(finding("test-file-not-collected", name, f"regression command did not report this test file: {relative}"))
                text = read_text_safely(candidate)
                test_file_texts.append((str(relative), text))
                if origin == "generated":
                    slug = package_test_slug(name)
                    relative_text = Path(str(relative)).as_posix().lower()
                    if slug not in relative_text:
                        findings.append(finding(
                            "generated-test-not-package-specific",
                            name,
                            f"generated test/gate path must contain package slug {slug!r}: {relative}",
                        ))
                    if is_umbrella_generated_test_path(str(relative)):
                        findings.append(finding(
                            "generated-test-umbrella-name",
                            name,
                            f"generated tests must be split by package, not roadmap group/umbrella: {relative}",
                        ))
                    generated_file_owners.setdefault(normalized, set()).add(name)
                if candidate.suffix.lower() in TEST_SUFFIXES:
                    if SKIP_OR_ONLY_RE.search(text):
                        findings.append(finding("focused-or-skipped-test", name, f"skip/only is forbidden in durable regression file: {relative}"))
                    if any(pattern.search(text) for pattern in TAUTOLOGY_PATTERNS):
                        findings.append(finding("tautological-assertion", name, f"trivial self-fulfilling assertion found: {relative}"))
                    if gate_type == "test" and not TEST_DECLARATION_RE.search(text):
                        findings.append(finding("test-declaration-missing", name, f"no executable test()/it() declaration found: {relative}"))
                    if gate_type == "test" and not ASSERTION_RE.search(text):
                        findings.append(finding("assertion-missing", name, f"no assertion found in regression test: {relative}"))

        test_cases = item.get("testCases")
        cases = [str(case).strip() for case in test_cases] if isinstance(test_cases, list) else []
        cases = [case for case in cases if case]
        if not cases:
            findings.append(finding("test-cases-missing", name, "testCases[] must name concrete executed cases"))
        elif gate_type == "test" and test_file_texts:
            all_test_text = "\n".join(text for _, text in test_file_texts)
            for case in cases:
                if case not in all_test_text:
                    findings.append(finding("test-case-not-found", name, f"testCases entry is not present as a test title in testFiles: {case}"))

        if origin == "generated":
            probe = item.get("failureProbe")
            if not isinstance(probe, dict):
                findings.append(finding("failure-probe-missing", name, "generated regression gates require a temporary negative/mutation proof"))
            else:
                if str(probe.get("status") or "").lower() not in PASS_VALUES:
                    findings.append(finding("failure-probe-not-passed", name, "failureProbe.status must confirm that the gate failed for the intentional regression"))
                if probe.get("restored") is not True:
                    findings.append(finding("failure-probe-not-restored", name, "failureProbe.restored must be exactly true"))
                if len(str(probe.get("description") or "").strip()) < 20:
                    findings.append(finding("failure-probe-description-missing", name, "describe the temporary mutation and observed failure"))

        for phase_key in ("baseline", "postUpdate"):
            phase = item.get(phase_key)
            if not isinstance(phase, dict):
                findings.append(finding(f"{phase_key}-missing", name, f"{phase_key} evidence is required"))
                continue
            status = str(phase.get("status") or "").lower()
            if status not in PASS_VALUES:
                findings.append(finding(f"{phase_key}-not-passed", name, f"{phase_key}.status must be passed, actual={status or 'missing'}"))
            command = str(phase.get("command") or "").strip()
            if not command:
                findings.append(finding(f"{phase_key}-command-missing", name, f"{phase_key}.command is required"))
            elif regression_command and command != regression_command:
                findings.append(finding(f"{phase_key}-wrong-command", name, "baseline and post-update must run the same dedicated regression command"))

    for relative, owners in sorted(generated_file_owners.items()):
        if len(owners) > 1:
            owner_list = ", ".join(sorted(owners))
            for owner in sorted(owners):
                findings.append(finding(
                    "generated-test-file-shared",
                    owner,
                    f"one generated test/gate file cannot cover several packages ({owner_list}): {relative}",
                ))

    summary = {
        "scope": scope_meta,
        "actionRows": len(actions),
        "requiredRows": len(required_rows),
        "coveredRequiredRows": len(required_rows) - sum(1 for f in findings if f["code"] == "required-package-uncovered"),
        "regressionScript": regression_script,
        "regressionDirectory": regression_dir_text,
    }
    return findings, summary


def execute_regression_command(project_dir: Path, command: str) -> Tuple[int, str]:
    """Execute a simple package-manager script from trusted run evidence."""
    match = re.fullmatch(r"(yarn|pnpm)\s+([A-Za-z0-9:_-]+)", command)
    if match:
        argv = [match.group(1), match.group(2)]
    else:
        match = re.fullmatch(r"npm\s+run\s+([A-Za-z0-9:_-]+)", command)
        if not match:
            raise ValueError("regressionTestCommand must be a simple yarn/pnpm/npm script command")
        argv = ["npm", "run", match.group(1)]
    executable = shutil.which(argv[0])
    if not executable:
        raise ValueError(f"package manager executable not found: {argv[0]}")
    completed = subprocess.run(
        [executable, *argv[1:]], cwd=project_dir, text=True,
        capture_output=True, check=False, timeout=900,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, output[-8000:]


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Validate isolated dependency-regression test evidence.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--scope-manifest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--project")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--execute-regression", action="store_true", help="Run evidence.regressionTestCommand and fail on a non-zero exit code")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).expanduser().resolve()
    scope_path = Path(args.scope_manifest).expanduser().resolve()
    evidence_path = Path(args.evidence).expanduser().resolve()
    try:
        findings, summary = validate(project_dir, scope_path, evidence_path, args.project)
        if args.execute_regression:
            command = str(read_json(evidence_path).get("regressionTestCommand") or "").strip()
            exit_code, output_tail = execute_regression_command(project_dir, command)
            summary["regressionExecutionExitCode"] = exit_code
            if exit_code != 0:
                findings.append(finding(
                    "regression-command-failed", "<suite>",
                    f"{command!r} exited with {exit_code}; output tail:\n{output_tail}",
                ))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        findings = [finding("invalid-input", "<suite>", str(exc))]
        summary = {}

    errors = sum(1 for item in findings if item["severity"] == "error")
    payload = {"result": "FAIL" if errors else "PASS", "errors": errors, "summary": summary, "findings": findings}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"# Dependency regression validation: {payload['result']}")
        print()
        print(f"Required rows: {summary.get('requiredRows', 0)}; covered: {summary.get('coveredRequiredRows', 0)}")
        print(f"Regression script: `{summary.get('regressionScript') or 'missing'}`")
        print(f"Regression directory: `{summary.get('regressionDirectory') or 'missing'}`")
        if findings:
            print("\n| Severity | Code | Package | Detail |")
            print("|---|---|---|---|")
            for item in findings:
                detail = str(item["detail"]).replace("|", "\\|")
                print(f"| {item['severity']} | `{item['code']}` | `{item['package']}` | {detail} |")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Structured domain-failure boundary for user-facing runs.

A Baseline that stops for an expected product reason must not present a raw
Python traceback as its primary result. It must say what happened, in which
stage, what it did about it, and what the user can do next. The traceback is
diagnostic detail, not the answer.

This is deliberately NOT error hiding: every failure still stops the run with a
non-zero exit, and the full traceback is preserved in a diagnostic artifact.
Only `TOOL_INTERNAL_ERROR` is treated as an unexpected defect.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import traceback
from pathlib import Path
from typing import Mapping, Optional

FAILURE_SCHEMA = "DEPLOOM_FAILURE_V2"

# Categories are ordered most-specific first: the first pattern that matches
# anywhere in the message wins, so a generic wrapper such as
# BASELINE_VERIFY_UNKNOWN_ERROR never masks the precise cause it carries.
_CATEGORY_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        r"OBSERVED_RESOLVED_ASSIGNMENT_(ESCAPE|DRIFT|MISSING|UNDECLARED|INVALID)"
        r"|SUBSTRATE_ASSIGNMENT_DRIFT|RESOLVED_STATE_PROJECT_DRIFT",
        "ASSIGNMENT_DRIFT",
        "retry-after-rebuild",
        "The prepared dependency tree did not match the proven assignment. "
        "The suspect materialization is discarded and rebuilt automatically.",
    ),
    (
        r"SOURCE_SNAPSHOT_[A-Z_]+|SOURCE_CAPTURE_[A-Z_]+|SOURCE_MATERIALIZED_[A-Z_]+"
        r"|SOURCE_SYMLINK_ESCAPE|SOURCE_REPARSE_LAYOUT_UNSUPPORTED",
        "SOURCE_INTEGRITY",
        "retry",
        "The captured source bytes changed or could not be proven. "
        "Ensure nothing is writing to the project during verification.",
    ),
    (
        r"PREPARED_ARTIFACT_[A-Z_]+|ARTIFACT_INTEGRITY|PREPARED_SNAPSHOT_[A-Z_]+",
        "ARTIFACT_INTEGRITY",
        "retry-after-rebuild",
        "A cached prepared dependency artifact failed its integrity seal and "
        "was discarded. The next run rebuilds it.",
    ),
    (
        r"SOURCE_CHECKOUT_[A-Z_]+|SOURCE_BRANCH_[A-Z_]+|SOURCE_REMOTE_NOT_FOUND"
        r"|SOURCE_PROJECT_NOT_FOUND|SOURCE_NOT_GIT_REPOSITORY",
        "ENVIRONMENT",
        "user-action-required",
        "The project checkout could not be prepared. Commit or stash local "
        "changes and check the configured branch/remote.",
    ),
    (
        r"WATCHER|ReadDirectoryChangesW|GetOverlappedResult",
        "WATCHER_FAILURE",
        "retry",
        "Filesystem change notification became unusable; verification fell "
        "back to authoritative validation.",
    ),
    (
        r"SUBSTRATE_[A-Z_]+|VERIFICATION_SUBSTRATE",
        "SUBSTRATE_INVARIANT",
        "not-retryable",
        "A verification substrate invariant failed even after a clean rebuild. "
        "This is not a problem with the project's dependencies.",
    ),
    (
        r"TIMEOUT|TimeoutExpired|timed out",
        "PROCESS_TIMEOUT",
        "retry",
        "A project command exceeded its time budget. Increase the timeout or "
        "investigate the command.",
    ),
    (
        r"ENOSPC|No space left|DISK_FULL|not enough space",
        "DISK",
        "user-action-required",
        "The machine ran out of disk space.",
    ),
    (
        r"E401|EAUTHUNKNOWN|401 Unauthorized|403 Forbidden|authentication",
        "AUTH",
        "user-action-required",
        "The package registry rejected the credentials in use.",
    ),
    (
        r"ENOTFOUND|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|socket hang up|network",
        "NETWORK",
        "retry",
        "The registry could not be reached.",
    ),
    (
        r"E404|ETARGET|No matching version|registry",
        "REGISTRY",
        "not-retryable",
        "The registry does not offer a version combination the plan requires.",
    ),
    (
        r"RESOLVER_[A-Z_]+|ERESOLVE|peer dep|EPEERINVALID",
        "RESOLVER_INCOMPATIBLE",
        "not-retryable",
        "The package manager cannot resolve this dependency combination.",
    ),
    (
        r"BASELINE_RECOVERY_CONTINUE_UNAVAILABLE",
        "RECOVERY_STATE",
        "user-action-required",
        "The saved Baseline checkpoint is missing or incompatible with the "
        "current source or settings. It cannot be resumed; use Start over "
        "to begin a new search. Previously verified artifacts remain intact.",
    ),
    (
        r"BASELINE_BUDGET_EXHAUSTED|budget-exhausted-after-one-candidate"
        r"|wall-clock budget|time budget consumed",
        "BUDGET_EXHAUSTED",
        "user-action-required",
        "The wall-clock / attempt budget was consumed before a compatible "
        "assignment was verified. This proves only that the attempted candidate "
        "failed within the budget, not that the project is unresolvable; the "
        "previous baseline is preserved. Deepening the search depth (EXHAUSTIVE) "
        "does not add time -- resume with a larger explicit budget or new "
        "evidence.",
    ),
    (
        r"PROJECT_[A-Z_]+|project check|lint|tsc|type error",
        "PROJECT_INCOMPATIBLE",
        "not-retryable",
        "The project's own checks failed on this dependency combination.",
    ),

(
    r"BASELINE_VERIFICATION_(HARD_SAFETY_LIMIT|PLATEAU)"
    r"|HARD_SAFETY_LIMIT|absolute-hard-safety-ceiling|STAGNATION",
    "SEARCH_LIMIT",
    "user-action-required",
    "The verified search reached its bounded safety/plateau limit without "
    "a proven incumbent. Do not retry unchanged; use the suggested "
    "predicate/cohort action, explicitly choose exhaustive search, or "
    "change scope before continuing.",
),
    (
        r"BASELINE_VERIFY_UNKNOWN_ERROR|UNKNOWN",
        "UNKNOWN",
        "retry",
        "The run stopped for a reason that could not be classified.",
    ),
)


@dataclasses.dataclass(frozen=True)
class DeploomFailure:
    code: str
    category: str
    severity: str
    retryability: str
    summary: str
    recovery_action: str
    project: str = ""
    mode: str = ""
    iteration: str = ""
    assignment: str = ""
    phase: str = ""
    command: str = ""
    root_cause: str = ""
    proof_impact: str = ""
    diagnostic_artifact: str = ""
    # BLOCK_PSI_VERIFIED_BASELINE_FAST_BUDGET_V1
    # The last failed check evidence (candidate project check / confirmation)
    # so the user-facing envelope and the diagnostic artifact say WHICH command
    # failed, with which exit code, on which predicate, and how long it ran.
    check_phase: str = ""
    check_command: str = ""
    check_exit_code: str = ""
    check_predicate: str = ""
    check_elapsed_seconds: str = ""
    check_log: str = ""

    def to_envelope(self) -> dict[str, object]:
        return {
            "schemaVersion": FAILURE_SCHEMA,
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "retryability": self.retryability,
            "project": self.project,
            "mode": self.mode,
            "iteration": self.iteration,
            "assignment": self.assignment,
            "phase": self.phase,
            "command": self.command,
            "summary": self.summary,
            "rootCause": self.root_cause,
            "proofImpact": self.proof_impact,
            "recoveryAction": self.recovery_action,
            "checkPhase": self.check_phase,
            "checkCommand": self.check_command,
            "checkExitCode": self.check_exit_code,
            "checkPredicate": self.check_predicate,
            "checkElapsedSeconds": self.check_elapsed_seconds,
            "diagnosticArtifact": self.diagnostic_artifact,
            "occurredAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def human_summary(self) -> str:
        lines = [f"Baseline stopped safely: {self.category}", ""]
        for label, value in (
            ("Project", self.project),
            ("Mode", self.mode),
            ("Iteration", self.iteration),
            ("Assignment", self.assignment),
            ("Stage", self.phase),
            ("Command", self.command),
            ("Code", self.code),
        ):
            if value:
                lines.append(f"{label}: {value}")
        lines.append("")
        if self.root_cause:
            lines.append(f"Cause: {self.root_cause}")
        if self.proof_impact:
            lines.append(f"Proof impact: {self.proof_impact}")
        if self.check_command:
            lines.append(
                "Candidate check failed: "
                f"{self.check_command} (exit {self.check_exit_code or 'n/a'}, "
                f"phase={self.check_phase or 'n/a'}, "
                f"predicate={self.check_predicate or 'n/a'}, "
                f"elapsed={self.check_elapsed_seconds or 'n/a'}s)"
            )
        lines.append(f"Recovery: {self.recovery_action}")
        if self.diagnostic_artifact:
            lines.append("")
            lines.append(f"Detailed diagnostics: {self.diagnostic_artifact}")
        return "\n".join(lines)


_GENERIC_CODES = frozenset({
    "BASELINE_VERIFY_UNKNOWN_ERROR",
    "BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR",
})


def _extract_code(message: str) -> str:
    """Prefer the most specific code the message carries.

    A generic wrapper such as BASELINE_VERIFY_UNKNOWN_ERROR normally prefixes
    the precise cause. Reporting the wrapper as "the code" is what made the
    original incident look unclassifiable when it was not.
    """
    codes = re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,})\b", message or "")
    for code in codes:
        if code not in _GENERIC_CODES:
            return code
    return codes[0] if codes else ""


def classify_failure(
    exc: BaseException,
    *,
    expected: bool = True,
) -> tuple[str, str, str]:
    """Return (category, retryability, recovery_action).

    `expected=False` marks a genuine tool defect: only that becomes
    TOOL_INTERNAL_ERROR, and even then the run still stops cleanly.
    """
    message = str(exc) or type(exc).__name__
    if not expected:
        return (
            "TOOL_INTERNAL_ERROR",
            "report-defect",
            "This is a defect in DepLoom itself. The run stopped without "
            "publishing any result; please report the diagnostic artifact.",
        )
    for pattern, category, retryability, recovery in _CATEGORY_RULES:
        if re.search(pattern, message, re.IGNORECASE if pattern.islower() else 0):
            return category, retryability, recovery
    return (
        "UNKNOWN",
        "retry",
        "The run stopped for a reason that could not be classified.",
    )


def build_failure(
    exc: BaseException,
    *,
    expected: bool = True,
    context: Optional[Mapping[str, str]] = None,
    diagnostic_artifact: str = "",
    check_evidence: Optional[Mapping[str, str]] = None,
) -> DeploomFailure:
    context = dict(context or {})
    evidence = dict(check_evidence or {})
    category, retryability, recovery = classify_failure(exc, expected=expected)
    message = str(exc) or type(exc).__name__
    proof_impact = (
        "No proof was published for this assignment."
        if category
        in {
            "ASSIGNMENT_DRIFT",
            "SOURCE_INTEGRITY",
            "ARTIFACT_INTEGRITY",
            "SUBSTRATE_INVARIANT",
            "WATCHER_FAILURE",
        }
        else ""
    )
    # BLOCK_PSI_VERIFIED_BASELINE_FAST_BUDGET_V1
    # The recommendation must match the actual stop reason and the already
    # granted authorization: EXHAUSTIVE permits deeper search, it never grants
    # a new wall-clock budget. Disambiguate the "more iterations" permission
    # from the "deeper localization mode" and never promise a continuation
    # without new budget.
    if category in {"BUDGET_EXHAUSTED", "SEARCH_LIMIT"}:
        continuation = context.get("continuationReason", "")
        exhaustive_authorized = str(context.get("exhaustiveAuthorized", "")).lower() in {
            "1", "true", "yes"
        }
        if continuation == "AUTOMATIC_BUDGET_EXHAUSTED" and exhaustive_authorized:
            recovery += (
                " Exhaustive authorization is already granted and permits "
                "deeper search, but it does not extend the wall-clock budget: "
                "a new or larger explicit budget is required before continuing."
            )
        elif continuation == "AUTOMATIC_BUDGET_EXHAUSTED":
            recovery += (
                " Choosing EXHAUSTIVE changes the localization depth, not the "
                "time budget: resume with a larger explicit budget or new "
                "evidence."
            )
    return DeploomFailure(
        code=_extract_code(message) or type(exc).__name__,
        category=category,
        severity="error",
        retryability=retryability,
        summary=message.splitlines()[0][:500] if message else "",
        recovery_action=recovery,
        project=context.get("project", ""),
        mode=context.get("mode", ""),
        iteration=context.get("iteration", ""),
        assignment=context.get("assignment", ""),
        phase=context.get("phase", ""),
        command=context.get("command", ""),
        root_cause=message[:2000],
        proof_impact=proof_impact,
        diagnostic_artifact=diagnostic_artifact,
        check_phase=evidence.get("phase", ""),
        check_command=evidence.get("command", ""),
        check_exit_code=evidence.get("exitCode", ""),
        check_predicate=evidence.get("predicate", ""),
        check_elapsed_seconds=evidence.get("elapsedSeconds", ""),
        check_log=_bounded_check_log(evidence),
    )


def write_diagnostic_artifact(
    exc: BaseException,
    failure: DeploomFailure,
    *,
    directory: Optional[Path] = None,
) -> str:
    """Persist the envelope and the full traceback next to each other."""
    try:
        target = Path(
            directory
            or os.environ.get("DEPLOOM_DIAGNOSTICS_DIR")
            or (Path.home() / ".deploom" / "diagnostics")
        )
        target.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = target / f"deploom-failure-{stamp}-{os.getpid()}.json"
        payload = dict(failure.to_envelope())
        payload["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        if failure.check_log:
            payload["checkLog"] = failure.check_log
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return str(path)
    except OSError:
        # Failing to write diagnostics must never replace the real failure.
        return ""


_SECRET_HEAD_PATTERN = re.compile(
    r"(?i)(?P<head>"
    r"(?:token|password|passwd|secret|authorization|auth|_authToken|"
    r"npmRegistryServer|NPM_TOKEN|GH_TOKEN|GITHUB_TOKEN|Bearer)"
    r"\s*[:=]\s*)(?P<value>[^\s,;\"']+)"
)
_CHECK_LOG_TAIL_CHARS = 6000
_CHECK_LOG_LINES = 120


def _scrub_secrets(text: str) -> str:
    return _SECRET_HEAD_PATTERN.sub(lambda m: m.group("head") + "***", str(text or ""))


def _bounded_check_log(evidence: Mapping[str, str]) -> str:
    """Build a bounded, secret-scrubbed check log from failure evidence."""
    tail = _scrub_secrets(str(evidence.get("outputTail") or ""))
    lines = tail.splitlines()
    if len(lines) > _CHECK_LOG_LINES:
        tail = "\n".join(lines[-_CHECK_LOG_LINES:])
    tail = tail[-_CHECK_LOG_TAIL_CHARS:]
    command = _scrub_secrets(evidence.get("command") or "")
    if not command and not tail:
        return ""
    return "\n".join(
        line
        for line in (
            f"phase: {evidence.get('phase') or 'n/a'}",
            f"command: {command or 'n/a'}",
            f"exitCode: {evidence.get('exitCode') or 'n/a'}",
            f"predicate: {evidence.get('predicate') or 'n/a'}",
            f"elapsedSeconds: {evidence.get('elapsedSeconds') or 'n/a'}",
            f"outputTail (sanitized, bounded):{' ' + tail if tail else ' <empty>'}",
        )
        if line
    )

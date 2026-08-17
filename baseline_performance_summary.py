from __future__ import annotations

import dataclasses
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_DURATION_EVENTS = {
    "verify.workspace.finish": "workspaceMs",
    "verify.resolver.finish": "resolverMs",
    "verify.preparation.finish": "preparationMs",
    "verify.preparation.snapshot-publish": "snapshotPublishMs",
    "verify.project-check.clone.finish": "projectCloneMs",
    "verify.project-check.finish": "projectChecksMs",
}


@dataclasses.dataclass(frozen=True)
class TelemetryLoadResult:
    events: tuple[Mapping[str, Any], ...]
    malformed_lines: int = 0


def load_verification_telemetry(path: Path) -> TelemetryLoadResult:
    """Load best-effort observability data without turning it into proof authority."""
    events: list[Mapping[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return TelemetryLoadResult((), 0)
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            malformed += 1
            continue
        events.append(payload)
    return TelemetryLoadResult(tuple(events), malformed)


def _int_field(event: Mapping[str, Any], name: str) -> int:
    value = event.get(name)
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _verification_purpose(label: object) -> str:
    text = str(label or "").lower()
    if "nogood minimization" in text:
        return "minimization"
    if "graph certification" in text:
        return "graph-certification"
    if "exact confirmation" in text:
        return "exact-confirmation"
    if "baseline control" in text:
        return "control"
    if "reproduction" in text:
        return "reproduction"
    if "localization" in text:
        return "localization"
    if "iteration" in text and "assignment" in text:
        return "assignment"
    return "other"


def _new_bucket() -> dict[str, Any]:
    return {
        "events": 0,
        "attempts": 0,
        "completedAttempts": 0,
        "uniqueAssignments": set(),
        "durations": Counter(),
        "counts": Counter(),
        "proofHits": Counter(),
        "proofPublishes": Counter(),
        "outcomes": Counter(),
        "projectCommands": Counter(),
        "resolverPurposeMs": Counter(),
        "resolverPurposeExecutions": Counter(),
    }


def _consume(bucket: dict[str, Any], event: Mapping[str, Any]) -> None:
    name = str(event.get("event") or "")
    bucket["events"] += 1
    assignment = str(event.get("assignmentKey") or event.get("assignment") or "").strip()
    if assignment:
        bucket["uniqueAssignments"].add(assignment)

    duration_key = _DURATION_EVENTS.get(name)
    if duration_key:
        bucket["durations"][duration_key] += _int_field(event, "durationMs")
    if name == "verify.resolver.finish":
        purpose = _verification_purpose(event.get("label"))
        bucket["resolverPurposeMs"][purpose] += _int_field(event, "durationMs")
        bucket["resolverPurposeExecutions"][purpose] += 1

    if name == "verify.attempt.start":
        bucket["attempts"] += 1
    elif name == "verify.attempt.finish":
        bucket["completedAttempts"] += 1
    elif name == "verify.preparation.snapshot-hit":
        bucket["counts"]["preparationSnapshotHits"] += 1
    elif name == "verify.preparation.snapshot-publish":
        bucket["counts"]["preparationSnapshotPublishes"] += 1
    elif name == "verify.preparation.snapshot-rejected":
        bucket["counts"]["preparationSnapshotRejected"] += 1
    elif name == "verify.project-check.clone.finish":
        bucket["counts"]["projectFreshClones"] += 1
    elif name == "verify.project-check.finish":
        bucket["counts"]["projectChecks"] += 1
        command = str(event.get("command") or "").strip()
        if command:
            bucket["projectCommands"][command] += 1
    elif name == "proof.cache.hit":
        bucket["proofHits"][str(event.get("proofType") or "unknown")] += 1
    elif name == "proof.cache.publish":
        bucket["proofPublishes"][str(event.get("proofType") or "unknown")] += 1

    outcome = str(event.get("outcome") or "").strip()
    if outcome:
        bucket["outcomes"][outcome] += 1


def _freeze_bucket(bucket: Mapping[str, Any]) -> dict[str, Any]:
    durations = dict(sorted(bucket["durations"].items()))
    counts = dict(sorted(bucket["counts"].items()))
    proof_hits = dict(sorted(bucket["proofHits"].items()))
    proof_publishes = dict(sorted(bucket["proofPublishes"].items()))
    outcomes = dict(sorted(bucket["outcomes"].items()))
    commands = dict(sorted(bucket["projectCommands"].items()))
    resolver_by_purpose = {
        purpose: {
            "executions": int(bucket["resolverPurposeExecutions"].get(purpose, 0)),
            "durationMs": int(duration_ms),
        }
        for purpose, duration_ms in sorted(bucket["resolverPurposeMs"].items())
    }
    return {
        "events": int(bucket["events"]),
        "attempts": int(bucket["attempts"]),
        "completedAttempts": int(bucket["completedAttempts"]),
        "uniqueAssignments": len(bucket["uniqueAssignments"]),
        "durationsMs": durations,
        "counts": counts,
        "proofCacheHits": proof_hits,
        "proofPublishes": proof_publishes,
        "outcomes": outcomes,
        "projectCommands": commands,
        "resolverByPurpose": resolver_by_purpose,
        # Counts only: no invented wall-clock saving estimate.
        "avoidedWork": {
            "resolverExecutionsByProofHit": int(proof_hits.get("resolver", 0)),
            "fullProjectExecutionsByProofHit": int(proof_hits.get("project", 0)),
            "lifecyclePreparationsBySnapshotHit": int(counts.get("preparationSnapshotHits", 0)),
        },
    }


def summarize_verification_events(
    events: Sequence[Mapping[str, Any]],
    *,
    malformed_lines: int = 0,
) -> dict[str, Any]:
    overall = _new_bucket()
    by_project: dict[str, dict[str, Any]] = defaultdict(_new_bucket)
    for event in events:
        _consume(overall, event)
        project = str(event.get("projectPath") or "<unknown>")
        _consume(by_project[project], event)

    return {
        "schemaVersion": 1,
        "type": "deploom-baseline-performance-summary",
        "malformedTelemetryLines": max(0, int(malformed_lines)),
        "overall": _freeze_bucket(overall),
        "projects": {
            project: _freeze_bucket(by_project[project])
            for project in sorted(by_project)
        },
    }


def summarize_verification_telemetry(path: Path) -> dict[str, Any]:
    loaded = load_verification_telemetry(path)
    return summarize_verification_events(
        loaded.events,
        malformed_lines=loaded.malformed_lines,
    )


def _seconds(value_ms: object) -> str:
    try:
        value = int(value_ms)
    except (TypeError, ValueError):
        value = 0
    return f"{value / 1000.0:.1f}s"


def render_performance_markdown(summary: Mapping[str, Any]) -> str:
    overall = dict(summary.get("overall") or {})
    durations = dict(overall.get("durationsMs") or {})
    counts = dict(overall.get("counts") or {})
    hits = dict(overall.get("proofCacheHits") or {})
    avoided = dict(overall.get("avoidedWork") or {})
    rows = [
        "# Baseline Performance Summary",
        "",
        f"- Attempts: {overall.get('attempts', 0)} started / {overall.get('completedAttempts', 0)} completed",
        f"- Unique assignments observed: {overall.get('uniqueAssignments', 0)}",
        f"- Resolver time: {_seconds(durations.get('resolverMs', 0))}",
        f"- Lifecycle preparation time: {_seconds(durations.get('preparationMs', 0))}",
        f"- Prepared snapshot publish time: {_seconds(durations.get('snapshotPublishMs', 0))}",
        f"- Fresh project-clone time: {_seconds(durations.get('projectCloneMs', 0))}",
        f"- Project checks time: {_seconds(durations.get('projectChecksMs', 0))}",
        f"- Resolver proof hits: {hits.get('resolver', 0)}",
        f"- Project proof hits: {hits.get('project', 0)}",
        f"- Preparation snapshot hits: {counts.get('preparationSnapshotHits', 0)}",
        f"- Fresh project-check clones: {counts.get('projectFreshClones', 0)}",
        f"- Avoided resolver executions (count): {avoided.get('resolverExecutionsByProofHit', 0)}",
        f"- Avoided lifecycle preparations (count): {avoided.get('lifecyclePreparationsBySnapshotHit', 0)}",
        f"- Malformed telemetry lines ignored: {summary.get('malformedTelemetryLines', 0)}",
        "",
        "## Resolver cost by purpose",
        "",
    ]
    resolver_by_purpose = dict(overall.get("resolverByPurpose") or {})
    if resolver_by_purpose:
        for purpose in sorted(resolver_by_purpose):
            item = dict(resolver_by_purpose[purpose] or {})
            rows.append(
                f"- {purpose}: executions={item.get('executions', 0)}, "
                f"time={_seconds(item.get('durationMs', 0))}"
            )
    else:
        rows.append("- no resolver-finish telemetry")
    rows.extend([
        "",
        "> Telemetry is observability only. These counters never create or strengthen dependency proof.",
        "",
    ])
    projects = dict(summary.get("projects") or {})
    if projects:
        rows.extend(["## Per project", ""])
        for project in sorted(projects):
            item = dict(projects[project] or {})
            item_durations = dict(item.get("durationsMs") or {})
            rows.append(
                f"- `{project}` — attempts={item.get('attempts', 0)}, "
                f"assignments={item.get('uniqueAssignments', 0)}, "
                f"resolver={_seconds(item_durations.get('resolverMs', 0))}, "
                f"preparation={_seconds(item_durations.get('preparationMs', 0))}, "
                f"checks={_seconds(item_durations.get('projectChecksMs', 0))}"
            )
        rows.append("")
    return "\n".join(rows)

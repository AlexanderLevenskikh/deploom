#!/usr/bin/env python3
"""Analyze DepLoom Baseline verification JSONL telemetry.

This tool is intentionally read-only and non-authoritative. It reconstructs
performance categories, correlation-contract violations, cache efficiency and
slowest operations without changing solver/proof state.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from verification_observability import performance_category

# BLOCK_Y_FULL_OBSERVABILITY_V1


def _parse_timestamp(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSONL: {exc}") from exc
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _duration(event: Mapping[str, object]) -> int:
    try:
        return max(0, int(event.get("durationMs") or 0))
    except (TypeError, ValueError):
        return 0


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> int:
    ranges = sorted((max(0, a), max(0, b)) for a, b in intervals if b > a)
    if not ranges:
        return 0
    total = 0
    left, right = ranges[0]
    for start, end in ranges[1:]:
        if start <= right:
            right = max(right, end)
        else:
            total += right - left
            left, right = start, end
    return total + right - left


def _correlation_violations(events: Sequence[Mapping[str, object]]) -> list[str]:
    violations: list[str] = []

    def paired(kind: str, id_field: str) -> None:
        starts: collections.Counter[str] = collections.Counter()
        finishes: collections.Counter[str] = collections.Counter()
        for event in events:
            name = str(event.get("event") or "")
            identifier = str(event.get(id_field) or "")
            if not identifier:
                continue
            if name == f"verify.{kind}.start":
                starts[identifier] += 1
            elif name == f"verify.{kind}.finish":
                finishes[identifier] += 1
        for identifier in sorted(set(starts) | set(finishes)):
            if starts[identifier] != 1 or finishes[identifier] != 1:
                violations.append(
                    f"{kind} {identifier}: starts={starts[identifier]} finishes={finishes[identifier]}"
                )

    paired("request", "requestId")
    paired("attempt", "attemptId")

    stage_starts: collections.Counter[str] = collections.Counter()
    stage_finishes: collections.Counter[str] = collections.Counter()
    for event in events:
        stage_id = str(event.get("stageId") or "")
        if not stage_id:
            continue
        name = str(event.get("event") or "")
        if name.endswith(".start"):
            stage_starts[stage_id] += 1
        elif name.endswith(".finish"):
            stage_finishes[stage_id] += 1
    for stage_id in sorted(set(stage_starts) | set(stage_finishes)):
        if stage_starts[stage_id] != stage_finishes[stage_id]:
            violations.append(
                f"stage {stage_id}: starts={stage_starts[stage_id]} finishes={stage_finishes[stage_id]}"
            )

    cache_lookup: collections.Counter[str] = collections.Counter()
    cache_terminal: collections.Counter[str] = collections.Counter()
    for event in events:
        operation = str(event.get("cacheOperationId") or "")
        if not operation:
            continue
        name = str(event.get("event") or "")
        if name == "proof.cache.lookup":
            cache_lookup[operation] += 1
        elif name in {"proof.cache.hit", "proof.cache.miss", "proof.cache.rejected"}:
            cache_terminal[operation] += 1
    for operation in sorted(cache_lookup):
        if cache_lookup[operation] != 1 or cache_terminal[operation] != 1:
            violations.append(
                f"cache {operation}: lookups={cache_lookup[operation]} terminals={cache_terminal[operation]}"
            )

    sequences = [
        int(event["sequence"])
        for event in events
        if isinstance(event.get("sequence"), int)
    ]
    if sequences and sequences != sorted(sequences):
        violations.append("event sequence is not monotonic")
    if len(sequences) != len(set(sequences)):
        violations.append("event sequence contains duplicates")
    return violations


def build_report(
    events: Sequence[Mapping[str, object]],
    *,
    run_id: str = "",
    session_id: str = "",
) -> dict[str, object]:
    if not events:
        return {
            "runId": "",
            "events": 0,
            "performanceBreakdown": [],
            "violations": ["telemetry contains no events"],
        }

    effective_run = run_id.strip()
    if not effective_run:
        for event in reversed(events):
            candidate = str(event.get("runId") or "")
            if candidate:
                effective_run = candidate
                break

    effective_session = session_id.strip()
    if not effective_session:
        for event in reversed(events):
            if effective_run and str(event.get("runId") or "") != effective_run:
                continue
            candidate = str(event.get("sessionId") or "")
            if candidate:
                effective_session = candidate
                break

    filtered = [
        event
        for event in events
        if (not effective_run or str(event.get("runId") or "") == effective_run)
        and (not effective_session or str(event.get("sessionId") or "") == effective_session)
    ]
    if not filtered:
        return {
            "runId": effective_run,
            "events": 0,
            "performanceBreakdown": [],
            "violations": [f"runId {effective_run!r} not found"],
        }

    durations: collections.Counter[str] = collections.Counter()
    counts: collections.Counter[str] = collections.Counter()
    bytes_by_category: collections.Counter[str] = collections.Counter()
    files_by_category: collections.Counter[str] = collections.Counter()
    intervals: list[tuple[int, int]] = []
    slow: list[dict[str, object]] = []

    for event in filtered:
        name = str(event.get("event") or "")
        category = performance_category(name)
        if not category:
            continue
        duration = _duration(event)
        durations[category] += duration
        counts[category] += 1
        for key in ("byteCount", "bytesCopied", "bytesHashed", "bytesRead", "sourceBytes"):
            try:
                value = int(event.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                bytes_by_category[category] += value
                break
        for key in ("fileCount", "files", "sourceFiles", "candidateFiles"):
            try:
                value = int(event.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                files_by_category[category] += value
                break

        try:
            finish = int(event.get("runOffsetMs") or 0)
        except (TypeError, ValueError):
            finish = 0
        if duration and finish:
            intervals.append((max(0, finish - duration), finish))
        if duration:
            slow.append(
                {
                    "event": name,
                    "category": category,
                    "durationMs": duration,
                    "label": str(event.get("label") or event.get("command") or "")[:300],
                    "operationId": str(
                        event.get("operationId")
                        or event.get("stageId")
                        or event.get("attemptId")
                        or ""
                    ),
                }
            )

    known_work = sum(durations.values())
    breakdown: list[dict[str, object]] = []
    for category, duration in sorted(
        durations.items(), key=lambda item: (-item[1], item[0])
    ):
        byte_count = int(bytes_by_category[category])
        breakdown.append(
            {
                "category": category,
                "count": int(counts[category]),
                "workMs": int(duration),
                "workPct": round(duration * 100.0 / known_work, 2)
                if known_work
                else 0.0,
                "bytes": byte_count,
                "files": int(files_by_category[category]),
                "throughputMiBPerSec": round(
                    (byte_count / (1024 * 1024)) / (duration / 1000), 2
                )
                if byte_count and duration
                else 0.0,
            }
        )

    offsets: list[int] = []
    for event in filtered:
        try:
            offsets.append(int(event.get("runOffsetMs") or 0))
        except (TypeError, ValueError):
            pass
    elapsed = max(offsets, default=0)
    covered = min(elapsed, _merge_intervals(intervals))

    latest_summary = next(
        (
            dict(event)
            for event in reversed(filtered)
            if event.get("event") == "verification.run.summary"
        ),
        {},
    )

    cache = {
        "lookups": sum(1 for e in filtered if e.get("event") == "proof.cache.lookup"),
        "hits": sum(1 for e in filtered if e.get("event") == "proof.cache.hit"),
        "misses": sum(1 for e in filtered if e.get("event") == "proof.cache.miss"),
        "rejected": sum(1 for e in filtered if e.get("event") == "proof.cache.rejected"),
    }
    cache["hitRatePct"] = round(
        cache["hits"] * 100.0 / cache["lookups"], 2
    ) if cache["lookups"] else 0.0

    solver_statuses: collections.Counter[str] = collections.Counter(
        str(e.get("status") or "unknown")
        for e in filtered
        if e.get("event") == "solver.z3.finish"
    )

    return {
        "runId": effective_run,
        "sessionId": effective_session,
        "events": len(filtered),
        "sessionContext": latest_summary.get("sessionContext") or {},
        "sessionElapsedMs": int(
            latest_summary.get("sessionElapsedMs") or elapsed
        ),
        "performanceBreakdown": breakdown,
        "topSlowOperations": sorted(
            slow, key=lambda item: int(item["durationMs"]), reverse=True
        )[:15],
        "accounting": latest_summary.get("accounting")
        or {
            "model": "interval-union-v1-reconstructed",
            "knownOperationWorkMs": known_work,
            "coveredWallMs": covered,
            "unattributedWallMs": max(0, elapsed - covered),
            "coveragePct": round(covered * 100.0 / elapsed, 2) if elapsed else 0.0,
            "parallelWorkFactor": round(known_work / covered, 3) if covered else 0.0,
        },
        "cache": cache,
        "solverStatuses": dict(sorted(solver_statuses.items())),
        "localization": {
            "checks": sum(
                1 for e in filtered if e.get("event") == "localization.ddmin.check-finish"
            ),
            "confirmations": sum(
                1
                for e in filtered
                if e.get("event") == "localization.ddmin.confirmation-finish"
            ),
        },
        "violations": _correlation_violations(filtered),
    }


def _human_duration(ms: int) -> str:
    seconds = max(0, int(ms)) / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:04.1f}s"
    hours, minute = divmod(minutes, 60)
    return f"{int(hours)}h {int(minute)}m {sec:04.1f}s"


def print_report(report: Mapping[str, object]) -> None:
    print(f"DepLoom verification performance — run {report.get('runId') or '<unknown>'} / {report.get('sessionId') or '<session>'}")
    print(f"Events: {report.get('events', 0)}")
    print(f"Session wall: {_human_duration(int(report.get('sessionElapsedMs') or 0))}")
    print()
    print("Category                         Count       Work       Share     MiB/s")
    print("-" * 72)
    for item in report.get("performanceBreakdown") or []:
        if not isinstance(item, dict):
            continue
        print(
            f"{str(item.get('category') or ''):<31}"
            f"{int(item.get('count') or 0):>6}  "
            f"{_human_duration(int(item.get('workMs') or 0)):>10}  "
            f"{float(item.get('workPct') or 0):>7.2f}%  "
            f"{float(item.get('throughputMiBPerSec') or 0):>8.2f}"
        )

    accounting = report.get("accounting") or {}
    if isinstance(accounting, dict):
        print()
        print(
            "Accounting: "
            f"covered={_human_duration(int(accounting.get('coveredWallMs') or 0))}, "
            f"unattributed={_human_duration(int(accounting.get('unattributedWallMs') or 0))}, "
            f"coverage={float(accounting.get('coveragePct') or 0):.2f}%, "
            f"parallelWorkFactor={float(accounting.get('parallelWorkFactor') or 0):.3f}"
        )

    cache = report.get("cache") or {}
    if isinstance(cache, dict):
        print(
            "Cache: "
            f"lookups={cache.get('lookups', 0)}, hits={cache.get('hits', 0)}, "
            f"misses={cache.get('misses', 0)}, rejected={cache.get('rejected', 0)}, "
            f"hitRate={float(cache.get('hitRatePct') or 0):.2f}%"
        )

    slow = report.get("topSlowOperations") or []
    if slow:
        print()
        print("Slowest operations:")
        for item in slow[:10]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "")
            suffix = f" — {label}" if label else ""
            print(
                f"  {_human_duration(int(item.get('durationMs') or 0)):>10}  "
                f"{item.get('category')}{suffix}"
            )

    violations = report.get("violations") or []
    print()
    if violations:
        print("Contract violations:")
        for violation in violations:
            print(f"  - {violation}")
    else:
        print("Correlation contract: PASS")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("telemetry", type=Path)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    events = load_events(args.telemetry)
    report = build_report(events, run_id=args.run_id, session_id=args.session_id)
    print_report(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return 2 if args.strict and report.get("violations") else 0


if __name__ == "__main__":
    raise SystemExit(main())

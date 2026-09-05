#!/usr/bin/env python3
"""Summarize empirical iterative-cohort evidence from DepLoom observability JSONL."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


def read_events(paths: Iterable[Path]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and str(value.get("event") or "").startswith("baseline.cohort."):
                events.append(value)
    return events


def summarize(events: Iterable[dict[str, object]]) -> dict[str, object]:
    values = list(events)
    suggestions = [event for event in values if event.get("event") == "baseline.cohort.suggested"]
    actions = [event for event in values if event.get("event") == "baseline.cohort.user-action"]
    deferrals = [event for event in actions if str(event.get("action") or "").upper() == "DEFER"]
    reactivations = [event for event in actions if str(event.get("action") or "").upper() == "REACTIVATE"]
    incumbents = [event for event in values if event.get("event") == "baseline.cohort.incumbent"]
    expansions = [event for event in values if event.get("event") == "baseline.cohort.expanded"]

    first_incumbent_by_run: dict[str, dict[str, object]] = {}
    for event in sorted(incumbents, key=lambda item: int(item.get("runOffsetMs") or 0)):
        first_incumbent_by_run.setdefault(str(event.get("runId") or ""), event)

    ttfvur_ms = sorted(
        int(event.get("timeToFirstVerifiedUsableResultMs") or 0)
        for event in first_incumbent_by_run.values()
        if int(event.get("timeToFirstVerifiedUsableResultMs") or 0) > 0
    )
    defer_to_incumbent_ms: list[int] = []
    converted = 0
    for action in deferrals:
        run_id = str(action.get("runId") or "")
        incumbent = first_incumbent_by_run.get(run_id)
        if incumbent is None:
            continue
        action_offset = int(action.get("runOffsetMs") or 0)
        incumbent_offset = int(incumbent.get("runOffsetMs") or 0)
        if incumbent_offset >= action_offset:
            defer_to_incumbent_ms.append(incumbent_offset - action_offset)
            converted += 1

    by_cohort: dict[str, dict[str, int]] = defaultdict(lambda: {"suggested": 0, "deferred": 0, "reactivated": 0})
    for event in suggestions:
        by_cohort[str(event.get("cohortId") or "unknown")]["suggested"] += 1
    for event in deferrals:
        by_cohort[str(event.get("cohortId") or "unknown")]["deferred"] += 1
    for event in reactivations:
        by_cohort[str(event.get("cohortId") or "unknown")]["reactivated"] += 1

    return {
        "suggestions": len(suggestions),
        "deferrals": len(deferrals),
        "reactivations": len(reactivations),
        "expansions": len(expansions),
        "verifiedIncumbents": len(incumbents),
        "runsWithVerifiedUsableResult": len(first_incumbent_by_run),
        "medianTTFVURSeconds": round(statistics.median(ttfvur_ms) / 1000, 3) if ttfvur_ms else None,
        "p90TTFVURSeconds": round(ttfvur_ms[max(0, int(len(ttfvur_ms) * .9) - 1)] / 1000, 3) if ttfvur_ms else None,
        "suggestionAcceptanceRate": round(len(deferrals) / len(suggestions), 4) if suggestions else None,
        "deferralToIncumbentRate": round(converted / len(deferrals), 4) if deferrals else None,
        "medianDeferralToIncumbentSeconds": round(statistics.median(defer_to_incumbent_ms) / 1000, 3) if defer_to_incumbent_ms else None,
        "p90DeferralToIncumbentSeconds": round(sorted(defer_to_incumbent_ms)[max(0, int(len(defer_to_incumbent_ms) * .9) - 1)] / 1000, 3) if defer_to_incumbent_ms else None,
        "cohorts": dict(sorted(by_cohort.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = summarize(read_events(args.paths))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Iterative cohort evidence")
        for key, value in report.items():
            if key == "cohorts":
                continue
            print(f"  {key}: {value}")
        print("  cohorts:")
        for name, counts in report["cohorts"].items():
            print(f"    {name}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

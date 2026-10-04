#!/usr/bin/env python3
"""Print a readable per-step breakdown from the latest iterative benchmark JSON."""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "work" / "results"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="raw", default=None, help="specific results JSON file")
    args = ap.parse_args()
    if args.raw:
        path = Path(args.raw)
    else:
        files = sorted(glob.glob(str(RESULTS / "iterative-*.json")))
        if not files:
            print("no results found", file=sys.stderr)
            return 2
        path = Path(files[-1])
    print(f"file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for scenario, rows in data["results"].items():
        for r in rows:
            print(
                f"=== {scenario} run {r['runId']}: total={r['totalWallSeconds']}s "
                f"git={r['gitLaunches']} npm={r['npmLaunches']} node={r['nodeLaunches']} "
                f"other={r['otherLaunches']} steps={r['steps']}"
            )
            agg_setup = sum((s.get("metrics") or {}).get("subprocessSetupWallSeconds", 0) for s in r["stepsDetail"])
            agg_stat = sum((s.get("metrics") or {}).get("statCalls", 0) for s in r["stepsDetail"])
            agg_walk = sum((s.get("metrics") or {}).get("directoryWalks", 0) for s in r["stepsDetail"])
            print(
                f"    aggregate: popenSetup={agg_setup:.3f}s statCalls={agg_stat} "
                f"directoryWalks={agg_walk}"
            )
            for s in r["stepsDetail"]:
                m = s.get("metrics") or {}
                kinds = " ".join(f"{k}={v}" for k, v in m.get("subprocessByKind", {}).items())
                event = s["lastEvent"]["event"] if s["lastEvent"] else None
                gitdetail = m.get("gitByCommand") or {}
                gitfmt = ", ".join(f"{k}:{v}" for k, v in sorted(gitdetail.items()))
                print(
                    f"  {s['wallSeconds']:7.2f}s rc={s['returncode']:<2} "
                    f"{kinds or '-':38} {s['args'][0]:14} -> {event}"
                )
                if gitfmt:
                    print(f"            git: {gitfmt}")
                samples = m.get("argvSamples") or []
                for sample in samples:
                    print(f"            sample: {sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

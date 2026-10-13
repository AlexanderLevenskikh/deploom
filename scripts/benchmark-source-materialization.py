#!/usr/bin/env python3
"""Offline, disposable A/B substrate benchmark with a filtered source fixture.

Run on two revisions on the same volume with the same arguments. No registry,
user checkout, package install or privileged filesystem operation is required.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from verification_workspace_backend import materialize_private_tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--kb", type=int, default=64)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if not 1 <= args.files <= 100_000 or not 1 <= args.kb <= 4096 or not 1 <= args.reps <= 20:
        parser.error("provide files 1..100000, kb 1..4096, reps 1..20")
    with tempfile.TemporaryDirectory(prefix="deploom-materialize-bench-") as raw:
        source = Path(raw) / "source"
        source.mkdir()
        (source / "package.json").write_text('{"name":"bench"}', encoding="utf8")
        deps = source / "node_modules"
        deps.mkdir()
        (deps / "excluded.bin").write_bytes(b'dependency data')
        asset_root = source / "src"
        asset_root.mkdir()
        sample = bytes([i % 251 for i in range(1024)]) * args.kb
        for index in range(args.files):
            (asset_root / f"file-{index:06d}.dat").write_bytes(sample)
        samples = []
        for index in range(args.reps):
            target = Path(raw) / f"trial-{index}"
            started = time.perf_counter()
            method = materialize_private_tree(source, target, timeout_seconds=0,
                exclude_dir_names=("node_modules",))
            elapsed = time.perf_counter() - started
            if (target / 'node_modules').exists() or (target / 'src' / 'file-000000.dat').read_bytes() != sample:
                raise RuntimeError("BENCHMARK_COPY_MISMATCH")
            # Writable-private test: the producer must remain intact.
            (target / 'src' / 'file-000000.dat').write_bytes(b'modified')
            if (source / 'src' / 'file-000000.dat').read_bytes() != sample:
                raise RuntimeError("BENCHMARK_SOURCE_MUTATION")
            samples.append({"seconds": round(elapsed, 5), "method": method})
    report = {"schema": "deploom-source-materialization-bench-v1", "platform": sys.platform,
        "files": args.files, "fileKB": args.kb, "reps": args.reps,
        "samples": samples, "medianSeconds": statistics.median(row['seconds'] for row in samples),
        "warning": "Synthetic, warm-host benchmark, logical bytes only; not a product speedup claim"}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

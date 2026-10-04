#!/usr/bin/env python3
"""Physical verification benchmark with retained caches and real mutations.

Measures verification only. Network audit and Desktop rendering are outside
this harness and must not be inferred from these numbers.
"""
from __future__ import annotations
import argparse
import dataclasses
import hashlib
import platform
import json
import os
from pathlib import Path
import sys
import time

from bench_iterative_migration import ROOT, WORK, RESULTS, Fixture, _run, summarize
sys.path.insert(0, str(ROOT))
from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
from verification_observability import process_resource_snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--reps', type=int, default=3)
    args = parser.parse_args()
    if args.reps < 1:
        parser.error('--reps must be positive')
    stage = WORK / 'verification-reuse'
    stage.mkdir(parents=True, exist_ok=True)
    fixture = Fixture(stage)
    if not (fixture.cache / '_cacache').exists():
        fixture.seed([{'is-number': '5.0.0', 'is-finite': '1.0.0'}, {'is-number': '7.0.0', 'is-finite': '1.0.0'}])
    stamp = time.strftime('%Y%m%d-%H%M%S')
    results = {name: [] for name in ('cold-verification', 'warm-identical', 'leaf-source-change', 'root-config-change', 'dependency-change')}
    for rep in range(args.reps):
        project = fixture.clone_project(f'{stamp}-project-{rep}')
        cache = stage / f'{stamp}-proofs-{rep}'
        config = BaselineVerifyConfig(commands=('node check.js',), project_checks='strict', timeout_seconds=120, proof_cache_dir=str(cache), telemetry_path=str(stage / f'{stamp}-events-{rep}.jsonl'))
        assignment = {'is-number': '5.0.0', 'is-finite': '1.0.0'}
        os.environ['npm_config_cache'] = str(fixture.cache)
        os.environ['DEPLOOM_VERIFICATION_ROOT'] = str(stage / 'substrate')
        env = fixture.cli_env()
        def commit():
            for command in (['git','add','-A'], ['git','-c','user.name=Benchmark','-c','user.email=benchmark@example.invalid','commit','-qm','fixture mutation']):
                completed = _run(command, project)
                if completed.returncode:
                    raise RuntimeError(completed.stderr)
        for name in results:
            if name == 'leaf-source-change':
                (project / 'src' / 'leaf.js').write_text('module.exports = 1;\n', encoding='utf-8')
                commit()
            elif name == 'root-config-change':
                manifest = json.loads((project / 'package.json').read_text(encoding='utf-8'))
                manifest['scripts'] = {'check': 'node check.js'}
                (project / 'package.json').write_text(json.dumps(manifest), encoding='utf-8')
                commit()
                config = dataclasses.replace(config, commands=('node check.js && node check.js',))
            elif name == 'dependency-change':
                assignment = {**assignment, 'is-number': '7.0.0'}
                (project / 'src' / 'config.js').write_text("module.exports = {api: 'v2', feature: 'stable'};\n", encoding='utf-8')
                commit()
            telemetry = Path(config.telemetry_path)
            event_offset = telemetry.stat().st_size if telemetry.exists() else 0
            before = process_resource_snapshot()
            start = time.perf_counter()
            result = verify_assignment(project, assignment, config=config, run_project_checks=True, runtime_env=env)
            elapsed = time.perf_counter() - start
            after = process_resource_snapshot()
            if not result.ok or result.kind != 'passed':
                raise RuntimeError(f'{name}: verification did not pass: {result.summary}\n{result.output[-1600:]}')
            with telemetry.open('rb') as stream:
                stream.seek(event_offset)
                events = [json.loads(line) for line in stream if line.strip()]
            counts = {event: sum(item.get('event') == event for item in events) for event in ('proof.cache.hit', 'proof.cache.miss', 'verify.resolver.start', 'verify.project-check.start', 'verify.project-check.finish')}
            if counts['verify.project-check.finish'] < 1:
                raise RuntimeError(f'{name}: mandatory shell check evidence is missing')
            row = {'wallSeconds': elapsed, 'status': result.kind, 'assignment': dict(assignment), 'commands': list(config.commands), 'preparationProofKey': result.preparation_proof_key, 'resolvedStateKey': result.resolved_state_key, 'eventCounts': counts, 'resourcesBefore': dataclasses.asdict(before), 'resourcesAfter': dataclasses.asdict(after)}
            results[name].append(row)
            print(f'{name} #{rep}: {elapsed:.3f}s ({result.kind})', flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / f'verification-reuse-{stamp}.json'
    digest = hashlib.sha256()
    for path in sorted(ROOT.glob('*.py')):
        digest.update(path.name.encode('utf-8'))
        digest.update(path.read_bytes())
    metadata = {'sourceDigest': digest.hexdigest(), 'revisionLabel': os.environ.get('DEPLOOM_BENCH_LABEL', 'working-tree'), 'python': sys.version, 'platform': platform.platform(), 'scope': 'verification only; no online audit, AI latency or Desktop', 'cacheContract': 'retained project and proof caches within each five-scenario sequence; seeded offline npm cache'}
    output.write_text(json.dumps({'metadata': metadata, 'results': results}, indent=2), encoding='utf-8')
    for name, rows in results.items():
        print(name, summarize([row['wallSeconds'] for row in rows]))
    print(f'raw results: {output}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

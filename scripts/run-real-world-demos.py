"""Run retained, registry-independent workspace and manifest demos."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from examples.real_world_workspace_demos import demos, run_suite


def main():
    from cli_io import configure_utf8_stdio
    configure_utf8_stdio()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'.dependency-roadmap/demos/real-world')
    parser.add_argument('--core-only',action='store_true',help='Omit real npm installs; retain filesystem contracts')
    parser.add_argument('--case',action='append',dest='cases',help='Run one or more listed case IDs')
    parser.add_argument('--list',action='store_true')
    args=parser.parse_args()
    if args.list:
        for item in demos(): print(f'{item.name}: {item.title} [{item.group}]')
        return 0
    known={item.name for item in demos()}
    if args.cases and not set(args.cases)<=known:parser.error('Unknown --case; use --list')
    directory=args.output_dir.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    report=run_suite(directory,core_only=args.core_only,names=set(args.cases or []))
    for row in report['cases']:print(row['status'],row['id'],row.get('error',''))
    print('REPORT:',directory/'REPORT.md')
    print('RESULT:',report['passed'],'PASS;',report['failed'],'FAIL')
    return 1 if report['failed'] else 0


if __name__=='__main__':raise SystemExit(main())

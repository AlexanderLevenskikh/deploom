"""Empirical scheduler tuning with synthetic dependency projects.

Production planner and manifest writer, physical Node checks or virtual time.
This measures scheduling only: synthetic compatibility rules, no npm install,
external agent, vulnerability audit, or production verification authority.
"""
from __future__ import annotations
import argparse
import itertools
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from iterative_cohort_planner import plan_adaptive_cohort
from cohort_action_cost import STRATEGIES
from cohort_size_policy import next_size
from cohort_conflict_regions import update_regions
from iterative_scope_expansion import prepare_expansions
from baseline_constraint_verifier import _apply_assignment, assignment_fingerprint

CHECK = """const fs = require('fs');
const {dependencies:a} = JSON.parse(fs.readFileSync('package.json','utf8'));
const fixture = JSON.parse(fs.readFileSync('fixture.json','utf8'));
const conflicts = fixture.pairs.filter(pair => pair.every(n => a[n] === '2.0.0'));
const blocked = fixture.blocked.filter(n => a[n] === '2.0.0');
const missing = (fixture.requiredPairs || []).filter(([origin,peer]) => a[origin] === '2.0.0' && a[peer] !== '2.0.0');
setTimeout(() => { console.log(JSON.stringify(fixture.opaque ? {conflicts:[],blocked:[],missing:[]} : {conflicts,blocked,missing})); process.exit(conflicts.length || blocked.length || missing.length ? 1 : 0); }, fixture.delayMs + (fixture.slow || []).filter(n => a[n] === '2.0.0').length * fixture.extraDelayMs);
"""

def write(path,value): path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')

def experiment(root,profile,ratio,steps,seed,delay,strategy="size-aware",
               packages=26,virtual=False,initial=24,recovery_seconds=0):
    project=root/f'{profile}-{strategy}-initial{initial}-n{packages}-ratio{ratio}-steps{steps}-seed{seed}'
    project.mkdir(parents=True,exist_ok=False)
    names=[f'demo-p{i:02}' for i in range(packages)]
    rng=random.Random(seed);rng.shuffle(names)
    # Different orders expose sensitivity to the position of a conflict.
    blocked=[names[5]] if profile in ('large-remainder','variable-cost') else []
    groups=[]
    if profile=='fragmented-families':
        remaining=sorted(names)
        while remaining:
            width=23 if len(groups)%2==0 else 2
            groups.append(remaining[:width]);remaining=remaining[width:]
        blocked=[rng.choice(groups[0])]
    pair_count=6 if packages==26 else packages//4
    pair_offset=11 if packages==26 else packages//2
    pairs=[[names[i],names[i+pair_offset]] for i in range(pair_count)] if profile in ('cross-conflicts','opaque-failure') else []
    dense_size=8 if packages==26 else min(16,packages//3)
    if profile=='multi-failed-regions':
        pairs=[[f'demo-p{i:02}',f'demo-p{i+1:02}'] for i in range(0,packages-1,24)]
        pair_count=len(pairs)
    if profile=='dense-conflicts': pairs=[list(pair) for pair in itertools.combinations(names[:dense_size],2)]
    write(project/'fixture.json',{'opaque':profile in ('opaque-failure','multi-failed-regions'),'blocked':blocked,'pairs':pairs,'delayMs':int(delay*1000),
        'slow':names[:4] if profile=='variable-cost' else [],'extraDelayMs':int(delay*250),
        'requiredPairs':[['demo-p00',f'demo-p{packages-1:02}']] if profile=='mandatory-companion' else []})
    (project/'check.cjs').write_text(CHECK,encoding='utf-8')
    (project/'index.html').write_text('<!doctype html><title>Greedy demo</title><p>Synthetic dependency scheduling fixture</p>',encoding='utf-8')
    old=dict.fromkeys(sorted(names),'1.0.0');desired=dict.fromkeys(sorted(names),'2.0.0')
    write(project/'package.json',{'name':'deploom-greedy-demo','private':True,'scripts':{'test':'node check.cjs'},'dependencies':old})
    incumbent=dict(old);blocks=set();nogoods=[];checkpoints=[];history=[];observations=[];expansions=[]
    started=time.perf_counter();planning_seconds=0;first_accepted_seconds=None;virtual_seconds=0;recovery_total=0
    stop_reason='scope-exhausted';size_state={};regions=[]
    expected=packages-(1 if blocked else pair_count if profile in ('cross-conflicts','opaque-failure','multi-failed-regions') else dense_size-1 if profile=='dense-conflicts' else 0)
    fixed=strategy in ('fixed-8','fixed-24')
    effective_initial=int(strategy.split('-')[1]) if fixed else initial
    planner_strategy='whole-first' if fixed else strategy
    config={'localConflictRegions':False,'cohortGroups':groups,'cohortInitialPackages':effective_initial,'cohortMaxPackages':24,
            'cohortExplorationRatio':ratio,'cohortSearchSteps':steps,'cohortSchedulingStrategy':planner_strategy,
            'cohortSizePolicy':'fixed' if fixed else 'adaptive'}
    fixture=json.loads((project/'fixture.json').read_text(encoding='utf-8'))
    for attempt in range(packages*8):
        before=time.perf_counter()
        planning_desired,planning_ledger,issues=prepare_expansions(incumbent,desired,
            {'schedulerObservations':observations,'scopeExpansions':expansions,'cohortSizeState':size_state,'conflictRegions':regions},f'C{len(checkpoints)}',checkpoints)
        if issues: raise RuntimeError('Fixture companion proposal is unresolved')
        plan,detail=plan_adaptive_cohort(incumbent=incumbent,desired=planning_desired,
            config=config,
            ledger=planning_ledger,checkpoints=checkpoints,base_checkpoint_id=f'C{len(checkpoints)}',
            blocked_fingerprints=blocks,learned_nogoods=nogoods,fingerprint_fn=assignment_fingerprint)
        planning_seconds+=time.perf_counter()-before
        if plan is None:
            if detail.get('searchBudgetExhausted'):stop_reason='search-budget-exhausted'
            break
        _apply_assignment(project,plan.assignment_dict)
        check_start=time.perf_counter()
        if virtual:
            a=plan.assignment_dict
            evidence={'conflicts':[pair for pair in fixture['pairs'] if all(a[n]=='2.0.0' for n in pair)],
                      'blocked':[n for n in fixture['blocked'] if a[n]=='2.0.0'],
                      'missing':[[origin,peer] for origin,peer in fixture['requiredPairs'] if a[origin]=='2.0.0' and a[peer]!='2.0.0']}
            passed=not any(evidence.values())
            if fixture['opaque']:evidence={'conflicts':[],'blocked':[],'missing':[]}
            elapsed=(fixture['delayMs']+sum(a[n]=='2.0.0' for n in fixture['slow'])*fixture['extraDelayMs'])/1000
        else:
            timeout=(fixture['delayMs']+len(fixture['slow'])*fixture['extraDelayMs'])/1000+15
            result=subprocess.run([shutil.which('node'),'check.cjs'],cwd=project,capture_output=True,text=True,timeout=timeout)
            elapsed=time.perf_counter()-check_start
            if result.returncode not in (0,1): raise RuntimeError(result.stderr)
            evidence=json.loads(result.stdout);passed=result.returncode==0
        recovery=recovery_seconds if not passed else 0
        if recovery and not virtual:time.sleep(recovery)
        virtual_seconds+=elapsed+recovery;recovery_total+=recovery
        row={'attempt':attempt+1,'cohortSize':len(plan.packages),'checkSeconds':round(elapsed,3),
             'passed':passed,'phase':detail.get('selectionPhase'),'planningSeconds':round(planning_seconds,3),
             'atSeconds':virtual_seconds if virtual else time.perf_counter()-started,
             'verifiedPackages':sum(plan.assignment_dict[n]==desired[n] for n in names) if passed else sum(incumbent[n]==desired[n] for n in names),
             'failedWallSeconds':elapsed+recovery if not passed else 0, 'batchLimit':detail.get('batchLimit')}
        regions,region=update_regions(regions,scope='benchmark',config=config,
            candidate={'candidateId':str(attempt),'cohortSelection':detail,'delta':{'changed':dict.fromkeys(plan.packages,'2.0.0')}},
            outcome='PASS' if passed else 'FAIL',stage='tests',evidence='structural' if any(evidence.values()) else 'opaque',
            accepted_delta={'changed':dict.fromkeys(plan.packages,'2.0.0')} if passed else None,
            constraints=[dict.fromkeys(pair,'2.0.0') for pair in evidence['conflicts']])
        row.update({'regionId':region['regionId'] if region else None,'regionLineage':region.get('lineage',[]) if region else [],
                    'operator':detail.get('operator'),'operatorReason':detail.get('operatorReason')})
        history.append(row)
        size_state=next_size(size_state,config,feedback='PASS' if passed else 'FAIL',actual_size=len(plan.packages),scope='benchmark')
        observations.append({'operator':detail.get('operator'),'packages':list(plan.packages),
                             'wallSeconds':elapsed,'outcome':'accepted' if passed else 'rejected'})
        if passed:
            if first_accepted_seconds is None: first_accepted_seconds=virtual_seconds if virtual else time.perf_counter()-started
            incumbent=plan.assignment_dict
            checkpoints.append({'checkpointId':f'C{len(checkpoints)+1}','parentCheckpointId':f'C{len(checkpoints)}',
                'status':'VERIFIED','acceptedDelta':{'changed':dict.fromkeys(plan.packages,'2.0.0')}})
            # These are benchmark state markers, NOT DepLoom verification proofs.
            blocks=set()
        else:
            blocks.add(assignment_fingerprint(plan.assignment_dict))
            for origin,peer in evidence['missing']:
                expansions.append({'baseCheckpointId':f'C{len(checkpoints)}','packages':[origin],
                                   'companions':[f'{peer} = 2.0.0']})
            # The fixture's explicit rules provide exact deterministic clauses;
            # arbitrary project failures must never be promoted this way.
            for pair in evidence['conflicts']:
                clause=dict.fromkeys(pair,'2.0.0')
                if clause not in nogoods:nogoods.append(clause)
            for name in (evidence['blocked'] if profile not in ('large-remainder','variable-cost','fragmented-families') else []):
                clause={name:'2.0.0'}
                if clause not in nogoods:nogoods.append(clause)
        # Benchmark-only oracle maximum stops measurement, never planner input or physical proof.
        if sum(incumbent[n]==desired[n] for n in names)==expected:
            stop_reason='oracle-maximum-reached';break
    else:stop_reason='attempt-budget-exhausted'
    upgrades=sum(incumbent[n]==desired[n] for n in names)
    simulation_wall_seconds=time.perf_counter()-started
    wall_seconds=virtual_seconds if virtual else simulation_wall_seconds
    anytime={str(pct):next((r['atSeconds'] for r in history if r['verifiedPackages']>=expected*pct/100),None) for pct in (25,50,75,90)}
    if upgrades!=expected and stop_reason=='scope-exhausted':stop_reason='coverage-gap'
    result={'profile':profile,'strategy':strategy,'ratio':ratio,'searchSteps':steps,'seed':seed,'upgrades':upgrades,
        'clock':'virtual' if virtual else 'physical','initialPackages':effective_initial,
        'simulationWallSeconds':round(simulation_wall_seconds,3),'recoverySeconds':recovery_total,
        'completionState':stop_reason,'oracleMaximum':expected,'coverageGap':expected-upgrades,'targets':len(names),'checks':len(history),'failedChecks':sum(not x['passed'] for x in history),
        'wallSeconds':round(wall_seconds,3),'planningSeconds':round(planning_seconds,3),
        'timeToFirstAcceptedSeconds':round(first_accepted_seconds,3) if first_accepted_seconds is not None else None,
        'timeToReachableCoverageSeconds':anytime,'progressCurve':[{'seconds':r['atSeconds'],'verifiedPackages':r['verifiedPackages']} for r in history],
        'failedWallSeconds':sum(r['failedWallSeconds'] for r in history),
        'completionSeconds':wall_seconds if upgrades==expected else None,
        'verifiedPackagesPerMinute':round(upgrades*60/wall_seconds,3) if wall_seconds else None,
        'failedCheckSeconds':round(sum(x['checkSeconds'] for x in history if not x['passed']),3),
        'coverageFraction':upgrades/len(names),'agentSeconds':None,'conflictRegions':regions,'modeledRecoverySeconds':recovery_seconds,'history':history}
    write(project/'RESULT.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('history','progressCurve','conflictRegions')}),flush=True)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay-seconds',type=float,default=5)
    parser.add_argument('--reps',type=int,default=2)
    parser.add_argument('--packages',type=int,default=26)
    parser.add_argument('--virtual-time',action='store_true',help='Same oracle, no sleep; seconds are modeled, not measured')
    parser.add_argument('--initial-packages',default='24',help='Comma separated initial sizes, e.g. 8,24')
    parser.add_argument('--recovery-seconds',type=float,default=0,help='Modeled fixed recovery per rejected check; not an actual agent')
    parser.add_argument('--ratios',default='2,16')
    parser.add_argument('--steps',default='8,128')
    parser.add_argument('--profiles',default='clean,large-remainder,cross-conflicts,dense-conflicts')
    parser.add_argument('--strategies',default='size-aware')
    parser.add_argument('--seed-start',type=int,default=0)
    parser.add_argument('--output',default='.dependency-roadmap/qa/greedy-tuning')
    args=parser.parse_args()
    if not 0<=args.delay_seconds<=600 or not 1<=args.reps<=20:parser.error('delay 0..600, reps 1..20')
    if not 26<=args.packages<=512 or not 0<=args.recovery_seconds<=600:parser.error('packages 26..512, recovery 0..600')
    initials=list(map(int,args.initial_packages.split(',')))
    if any(not 1<=n<=24 for n in initials):parser.error('initial packages 1..24')
    root=(ROOT/args.output).resolve()
    if not root.is_relative_to(ROOT):parser.error('output must be inside the repository')
    root.mkdir(parents=True,exist_ok=True)
    run=root/time.strftime('%Y%m%d-%H%M%S');run.mkdir(exist_ok=False)
    grid=list(itertools.product(map(int,args.ratios.split(',')),map(int,args.steps.split(','))))
    if any(not 2<=ratio<=16 or not 8<=steps<=4096 for ratio,steps in grid):parser.error('ratio 2..16, steps 8..4096')
    strategies=args.strategies.split(',')
    if any(s not in (*STRATEGIES,'fixed-8','fixed-24') for s in strategies):parser.error('unknown scheduling strategy')
    profiles=args.profiles.split(',')
    if any(p not in ('clean','large-remainder','cross-conflicts','dense-conflicts','variable-cost','mandatory-companion','fragmented-families','opaque-failure','multi-failed-regions') for p in profiles):parser.error('unknown fixture profile')
    rows=[]
    for rep in range(args.seed_start,args.seed_start+args.reps):
        order=grid if rep%2==0 else list(reversed(grid))
        for profile in profiles:
            for ratio,steps in order:
                for strategy in (strategies if rep%2==0 else list(reversed(strategies))):
                    for initial in initials:
                        rows.append(experiment(run,profile,ratio,steps,rep,args.delay_seconds,strategy,
                            args.packages,args.virtual_time,initial,args.recovery_seconds))
    report={'clock':'virtual' if args.virtual_time else 'physical','scope':'synthetic-scheduler-only','checkDelaySeconds':args.delay_seconds,'repetitions':args.reps,'rows':rows}
    write(run/'SUMMARY.json',report)
    lines=['# Greedy parameter experiment','','Synthetic scheduling benchmark: production planner + manifest writer. Virtual clock uses the same deterministic oracle; physical clock runs Node. No install, audit, actual agent, or production verification proof.','','| Profile | Strategy | Initial | Ratio | Steps | Updates | Checks | Seconds | First accepted | Packages/min | State | Gap |','| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |']
    for r in rows:lines.append(f"| {r['profile']} | {r['strategy']} | {r['initialPackages']} | {r['ratio']} | {r['searchSteps']} | {r['upgrades']}/{r['targets']} | {r['checks']} | {r['wallSeconds']} | {r['timeToFirstAcceptedSeconds']} | {r['verifiedPackagesPerMinute']} | {r['completionState']} | {r['coverageGap']} |")
    (run/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('REPORT '+str(run/'REPORT.md'),flush=True)
if __name__=='__main__':main()

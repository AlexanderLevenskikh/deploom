"""AIMD hints and physical telemetry never replace verification authority."""
import json
from pathlib import Path
import tempfile
import unittest
from cohort_size_policy import next_size
from cohort_attempt_telemetry import append_attempt, stage_measurement, verification_stages, event_offset, agent_measurement
from iterative_cohort_planner import plan_adaptive_cohort
from baseline_constraint_verifier import assignment_fingerprint

class AdaptiveSizeTests(unittest.TestCase):
    config={'cohortMaxPackages':24,'cohortSchedulingStrategy':'adaptive-size'}
    def step(self,state,feedback,n=24,scope='s'):
        return next_size(state,self.config,feedback=feedback,actual_size=n,scope=scope)
    def test_repeated_failures_halve_and_first_failure_allows_switch(self):
        state={};sizes=[]
        for _ in range(8):
            state=self.step(state,'FAIL');sizes.append(state['size'])
        self.assertEqual(sizes,[24,12,12,6,6,3,3,1])
    def test_unknown_never_shrinks(self):
        state=self.step({},'FAIL')
        for _ in range(5):state=self.step(state,'NONE')
        self.assertEqual((state['size'],state['failureStreak']),(24,1))
    def test_success_series_grows_cautiously(self):
        state={'scope':'s','size':6}
        state=self.step(state,'PASS',6);self.assertEqual(state['size'],6)
        state=self.step(state,'PASS',6);self.assertEqual(state['size'],7)
    def test_tiny_success_does_not_restore_large_size(self):
        state={'scope':'s','size':12}
        for _ in range(8):state=self.step(state,'PASS',2)
        self.assertEqual(state['size'],12)
    def test_scope_change_resets_and_restart_preserves(self):
        state=self.step(self.step({},'FAIL'),'FAIL')
        state=json.loads(json.dumps(state))
        self.assertEqual(self.step(state,'NONE')['size'],12)
        self.assertEqual(self.step(state,'NONE',scope='new')['size'],24)
    def plan(self,names,state=None,blocks=(),atoms=(),ledger=None):
        old=dict.fromkeys(names,'1.0.0');desired=dict.fromkeys(names,'2.0.0')
        return plan_adaptive_cohort(incumbent=old,desired=desired,config={**self.config,'cohortAtoms':atoms},
            ledger=ledger or {'cohortSizeState':state or {}},checkpoints=[],base_checkpoint_id='C0',
            blocked_fingerprints=blocks,learned_nogoods=[],fingerprint_fn=assignment_fingerprint)
    def test_failure_switches_to_independent_large_cohort(self):
        names=[f'p{i:02}' for i in range(48)]
        first,_=self.plan(names)
        second,detail=self.plan(names,self.step({},'FAIL'),[assignment_fingerprint(first.assignment_dict)])
        self.assertEqual(len(second.packages),24)
        self.assertFalse(set(first.packages)&set(second.packages))
        self.assertEqual(detail['selectionPhase'],'whole-cohort')
    def test_shrinking_honors_dependency_atom_even_over_soft_size(self):
        names=list('abcdef')
        plan,detail=self.plan(names,{'size':1},atoms=[['a','b','c']])
        self.assertEqual(plan.packages,tuple('abc'))
        self.assertEqual(detail['batchLimit'],1)
    def test_companion_atom_is_not_split_after_rejection(self):
        ledger={'cohortSizeState':{'size':1},'scopeExpansions':[{'baseCheckpointId':'C0','packages':['a'],'companions':['b'],'requiredVersions':{'b':'2.0.0'}}]}
        first,_=self.plan(list('abc'),ledger=ledger)
        self.assertEqual(first.packages,tuple('ab'))
        second,_=self.plan(list('abc'),blocks=[first.assignment_fingerprint],ledger=ledger)
        self.assertEqual(second.packages,('c',))
    def test_hard_cap_rejects_oversized_atom_explicitly(self):
        with self.assertRaisesRegex(ValueError,'COHORT_ATOM_EXCEEDS_CAP'):
            self.plan([f'p{i}' for i in range(25)],atoms=[[f'p{i}' for i in range(25)]])

class AttemptTelemetryTests(unittest.TestCase):
    candidate={'candidateId':'p1','baseCheckpointId':'C0','delta':{'changed':{'a':'2','b':'2'}},'cohortSelection':{'operator':'whole','familyGroups':[['a','b']]}}
    def append(self,ledger,outcome='FAIL',accepted=None,sequence=1):
        return append_attempt(ledger,scope='s',config={'cohortSchedulingStrategy':'adaptive-size'},candidate={**self.candidate,'telemetrySequence':sequence},
            stage='verify-exact',outcome=outcome,seconds=2,stages=[stage_measurement('tsc',1,outcome,'npm run typecheck')],
            accepted_delta=accepted,cumulative_verified=2 if accepted else None,size_feedback='PASS' if accepted else 'FAIL')
    def test_acceptance_is_separate_from_stage_pass_and_resets_progress(self):
        ledger={};self.append(ledger)
        self.assertEqual(ledger['schedulerProgress']['attemptsWithoutVerifiedProgress'],1)
        self.assertEqual(ledger['schedulerProgress']['cumulativeVerifiedPackages'],0)
        self.append(ledger,'PASS',{'changed':{'a':'2','b':'2'}},sequence=2)
        row=ledger['attemptTelemetry'][-1]
        self.assertEqual((row['cumulativeVerifiedPackages'],row['attemptsWithoutVerifiedProgress']),(2,0))
        self.assertEqual((row['cohortSize'],row['familyCount'],row['failureStage']),(2,1,None))
    def test_command_pass_cannot_grow_size_or_reset_verified_progress(self):
        ledger={'cohortSizeState':{'scope':'s','size':12,'successStreak':1}}
        append_attempt(ledger,scope='s',config={},candidate=self.candidate,stage='precheck',outcome='PASS',seconds=1,size_feedback='PASS')
        self.assertEqual(ledger['cohortSizeState']['size'],12)
        self.assertEqual(ledger['schedulerProgress']['cumulativeVerifiedPackages'],0)
    def test_checkpoint_pointer_reconciles_a_missed_progress_event(self):
        from cohort_attempt_telemetry import record_attempt
        from iterative_migration import save_ledger,save_checkpoint,load_ledger
        from cohort_action_cost import observation_scope
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run={'runId':'r','activeCheckpointId':'C1'};config={'targets':{'a':'2'}}
            scope=observation_scope(run,config)
            save_checkpoint(root,{'schemaVersion':1,'checkpointId':'C0','status':'VERIFIED','fullAssignment':{'a':'1'}})
            save_checkpoint(root,{'schemaVersion':1,'checkpointId':'C1','status':'VERIFIED','fullAssignment':{'a':'2'}})
            save_ledger(root,{'schemaVersion':1,'schedulerProgress':{'scope':scope,'checkpointId':'C0','cumulativeVerifiedPackages':0,'physicalAttemptsWithoutVerifiedProgress':15}})
            record_attempt(root,run,config,self.candidate,stage='precheck',outcome='FAIL',seconds=1,size_feedback='FAIL')
            ledger=load_ledger(root)
            self.assertEqual(ledger['schedulerProgress']['cumulativeVerifiedPackages'],1)
            self.assertEqual(ledger['schedulerProgress']['attemptsWithoutVerifiedProgress'],1)
            self.assertTrue((root/'cohort-attempts.jsonl').exists())
    def test_duplicate_row_is_idempotent(self):
        ledger={};self.append(ledger);self.append(ledger)
        self.assertEqual(len(ledger['attemptTelemetry']),1)
        self.assertEqual(ledger['cohortSizeState']['failureStreak'],1)
    def test_unknown_duration_is_unavailable(self):
        self.assertIsNone(stage_measurement('agent',None,'UNKNOWN')['durationSeconds'])
        self.assertIsNone(stage_measurement('install',float('nan'),'UNKNOWN')['durationSeconds'])
    def test_event_tail_filters_assignment_and_uses_real_command_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'events.jsonl';p.write_text('{}\n');offset=event_offset(p)
            with p.open('a') as out:
                for fp in ('other','ours'):
                    out.write(json.dumps({'event':'verify.project-check.finish','assignment':fp,'command':'npm run typecheck','durationMs':1250,'exitCode':1})+'\n')
            rows=verification_stages(p,offset,'ours')
            self.assertEqual(rows,[stage_measurement('tsc',1.25,'FAIL','npm run typecheck')])
    def test_malformed_nonobject_telemetry_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'events.jsonl';p.write_text('42\nnull\n[]\nnot-json\n')
            self.assertEqual(verification_stages(p,0,'ours'),[])
    def test_malformed_agent_lease_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'trial';p.mkdir();(p/'agent-lease.json').write_text('[]')
            self.assertIsNone(agent_measurement(tmp,self.candidate))
    def test_absent_agent_lease_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:self.assertIsNone(agent_measurement(tmp,self.candidate))

if __name__=='__main__':unittest.main()

"""Regression of the C6 feedback/materialization loop, using durable files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from iterative_scope_expansion import parse_companion, prepare_expansions, apply_expansion_assignment
from iterative_cohort_planner import plan_adaptive_cohort
import iterative_migration as migration
from baseline_constraint_verifier import _apply_assignment, _validate_assignment_materialization, assignment_fingerprint


class ScopeExpansionTests(unittest.TestCase):
    def plan(self, incumbent, desired, ledger, blocked=()):
        desired, normalized, issues = prepare_expansions(incumbent, desired, ledger, 'C0', [])
        plan, _ = plan_adaptive_cohort(incumbent=incumbent, desired=desired,
            config={'cohortMaxPackages': 24}, ledger=normalized, checkpoints=[],
            base_checkpoint_id='C0', blocked_fingerprints=blocked,
            learned_nogoods=[], fingerprint_fn=assignment_fingerprint)
        return plan, issues

    def ledger(self, companions, packages=('react-testing',)):
        return {'scopeExpansions': [{'baseCheckpointId':'C0', 'packages':list(packages), 'companions':companions}]}

    def test_new_peer_and_existing_companion_produce_a_different_exact_candidate(self):
        old={'react-testing':'12.0.0','typescript-eslint':'8.11.0'}
        desired={**old,'react-testing':'16.0.0'}
        blocked=[assignment_fingerprint(desired)]
        plan,issues=self.plan(old,desired,self.ledger(['dom = 10.4.2','typescript-eslint = 8.71.0']),blocked)
        self.assertEqual(issues,[])
        self.assertEqual(plan.assignment_dict,{'react-testing':'16.0.0','dom':'10.4.2','typescript-eslint':'8.71.0'})
        self.assertNotIn(assignment_fingerprint(plan.assignment_dict),blocked)

    def test_split_cannot_retry_origin_without_mandatory_peer(self):
        old={'react-testing':'12.0.0','other':'1.0.0'}
        desired={'react-testing':'16.0.0','other':'2.0.0'}
        ledger=self.ledger(['dom = 10.4.2'])
        plan,_=self.plan(old,desired,ledger,[assignment_fingerprint({**desired,'dom':'10.4.2'})])
        self.assertIsNotNone(plan)
        if plan.assignment_dict['react-testing']=='16.0.0':
            self.assertEqual(plan.assignment_dict.get('dom'),'10.4.2')
        self.assertNotEqual(plan.assignment_dict,desired)

    def test_unknown_version_continues_independent_cohort_with_actionable_reason(self):
        plan,issues=self.plan({'a':'1.0.0','b':'1.0.0'},{'a':'2.0.0','b':'2.0.0'},self.ledger(['dom = ^10.0.0'],('a',)))
        self.assertEqual(plan.assignment_dict,{'a':'1.0.0','b':'2.0.0'})
        self.assertEqual(issues[0]['reason'],'SCOPE_EXPANSION_NEEDS_EXACT_VERSION')
        self.assertIn('exact-semver',issues[0]['nextAction'])

    def test_historical_range_uses_later_exact_proposal(self):
        ledger=self.ledger(['dom = ^10.0.0'])
        ledger['scopeExpansions'] += self.ledger(['dom = 10.4.2'])['scopeExpansions']
        plan,issues=self.plan({'react-testing':'12.0.0'},{'react-testing':'16.0.0'},ledger)
        self.assertFalse(issues)
        self.assertEqual(plan.assignment_dict['dom'],'10.4.2')

    def test_proposal_survives_unrelated_accepted_checkpoint_but_not_foreign_base(self):
        old={'a':'1.0.0','b':'2.0.0'}
        desired,ledger,issues=prepare_expansions(old,{'a':'2.0.0','b':'2.0.0'},self.ledger(['dom = 10.4.2'],('a',)),'C1',
            [{'checkpointId':'C1','parentCheckpointId':'C0'}])
        self.assertEqual(desired['dom'],'10.4.2')
        self.assertEqual(ledger['scopeExpansions'][0]['baseCheckpointId'],'C1')
        desired,_,_=prepare_expansions(old,old,self.ledger(['dom = 10.4.2']),'foreign',[])
        self.assertNotIn('dom',desired)

    def test_whole_other_cohort_precedes_splitting_failed_cohort(self):
        old={n:'1.0.0' for n in 'abcdef'};desired={n:'2.0.0' for n in old}
        failed={**old,**{n:'2.0.0' for n in 'abcd'}}
        plan,details=plan_adaptive_cohort(incumbent=old,desired=desired,
            config={'cohortMaxPackages':4},ledger={},checkpoints=[],base_checkpoint_id='C0',
            blocked_fingerprints=[assignment_fingerprint(failed)],learned_nogoods=[],fingerprint_fn=assignment_fingerprint)
        self.assertEqual(plan.packages,('e','f'))
        self.assertEqual(details['selectionPhase'],'whole-cohort')

    def test_large_failed_remainder_is_explored_before_tiny_whole_cohort(self):
        names=[f'p{i:02}' for i in range(26)]
        old=dict.fromkeys(names,'1.0.0');desired=dict.fromkeys(names,'2.0.0')
        failed={**old,**dict.fromkeys(names[:24],'2.0.0')}
        plan,details=plan_adaptive_cohort(incumbent=old,desired=desired,
            config={'cohortMaxPackages':24,'cohortSchedulingStrategy':'size-aware'},ledger={},checkpoints=[],base_checkpoint_id='C0',
            blocked_fingerprints=[assignment_fingerprint(failed)],learned_nogoods=[],fingerprint_fn=assignment_fingerprint)
        self.assertEqual(len(plan.packages),12)
        self.assertEqual(details['selectionPhase'],'size-aware-split')
        self.assertNotEqual(plan.assignment_dict,failed)

    def test_conflict_with_already_accepted_package_is_not_forgotten(self):
        old={'a':'2.0.0','b':'1.0.0','c':'1.0.0'};desired=dict.fromkeys(old,'2.0.0')
        plan,_=plan_adaptive_cohort(incumbent=old,desired=desired,config={},ledger={},checkpoints=[],
            base_checkpoint_id='C1',blocked_fingerprints=[],learned_nogoods=[{'a':'2.0.0','b':'2.0.0'}],
            fingerprint_fn=assignment_fingerprint)
        self.assertEqual(plan.assignment_dict,{'a':'2.0.0','b':'1.0.0','c':'2.0.0'})

    def test_unavailable_companion_tries_previous_exact_alternative(self):
        ledger=self.ledger(['dom = 10.4.1'])
        ledger['scopeExpansions'] += self.ledger(['dom = 10.4.2'])['scopeExpansions']
        desired,_,issues=prepare_expansions({'react-testing':'12.0.0'},{'react-testing':'16.0.0'},ledger,'C0',[],
            unavailable={('dom','10.4.2')})
        self.assertEqual(desired['dom'],'10.4.1')
        self.assertFalse(issues)

    def test_scoped_names_and_exact_versions_are_parsed(self):
        self.assertEqual(parse_companion('@testing-library/dom = 10.4.2'),('@testing-library/dom','10.4.2'))
        self.assertEqual(parse_companion('@scope/package@1.0.0'),('@scope/package','1.0.0'))
        self.assertIsNone(parse_companion('../escape = 1.0.0'))

    def test_actual_manifest_writer_declares_missing_peer_and_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'package.json'
            p.write_text(json.dumps({'private':True,'scripts':{'test':'node test.js'},'devDependencies':{'a':'1.0.0'}}))
            changed=apply_expansion_assignment(root,{'a':'2.0.0','dom':'10.4.2'},{'dom':'10.4.2'},_apply_assignment,_validate_assignment_materialization)
            manifest=json.loads(p.read_text())
            self.assertEqual(manifest['dependencies'],{'dom':'10.4.2'})
            self.assertEqual(manifest['devDependencies'],{'a':'2.0.0'})
            self.assertEqual(manifest['scripts'],{'test':'node test.js'})
            self.assertEqual(changed,['a','dom'])
            self.assertEqual(migration.direct_dependency_assignment(root),{'a':'2.0.0','dom':'10.4.2'})

    def test_fixed_dependency_is_not_overwritten_by_addition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'package.json';raw=json.dumps({'dependencies':{'dom':'file:../dom'}});p.write_text(raw)
            with self.assertRaisesRegex(ValueError,'ALREADY_DECLARED'):
                apply_expansion_assignment(root,{'dom':'10.4.2'},{'dom':'10.4.2'},_apply_assignment,_validate_assignment_materialization)
            self.assertEqual(p.read_text(),raw)

    def test_wrong_addition_identity_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'package.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'ADDITIONS_INVALID'):
                apply_expansion_assignment(root,{'dom':'10.4.2'},{'dom':'9.0.0'},_apply_assignment,_validate_assignment_materialization)
            self.assertEqual(p.read_text(),'{}')

    def test_real_feedback_then_real_plan_preserves_checkpoint_and_policy(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('scope_feedback_fixture', Path(__file__).with_name('test_iterative_finish_baseline_and_feedback_validation.py'))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        _RunDir, _run_dict, _config_dict, _checkpoint_dict, _candidate_dict, _feedback = (
            fixture._RunDir, fixture._run_dict, fixture._config_dict,
            fixture._checkpoint_dict, fixture._candidate_dict, fixture._feedback)
        with tempfile.TemporaryDirectory() as tmp:
            root=_RunDir(Path(tmp));run=_run_dict(phase='REPAIRING',activeCandidateId='cand-0001')
            config=_config_dict();checkpoint=_checkpoint_dict()
            candidate=_candidate_dict(run,fullAssignment={'pkg-a':'2.0.0','pkg-b':'1.1.0'},delta={'changed':{'pkg-a':'2.0.0','pkg-b':'1.1.0'}})
            root.write_run(run);root.write_config(config);root.write_checkpoint(checkpoint);root.write_candidate(candidate)
            feedback=_feedback(run,candidate,kind='NEEDS_COHORT_EXPANSION',proposedScope={'companions':['dom = 10.4.2']})
            migration._apply_feedback_locked(root.root,run,config,feedback)
            self.assertTrue(migration.load_ledger(root.root)['deferrals'])
            with patch.object(migration,'_assert_runtime_unchanged'):
                migration._plan_next_locked(root.root,migration.load_run(root.root),config)
            next_candidate=migration.load_candidate(root.root)
            self.assertEqual(next_candidate['fullAssignment']['dom'],'10.4.2')
            self.assertEqual(next_candidate['scopeExpansionAdditions'],{'dom':'10.4.2'})
            self.assertEqual(migration.load_checkpoint(root.root,'C0'),checkpoint)
            self.assertEqual(migration.load_config(root.root),config)
            self.assertEqual(migration.load_run(root.root)['activeCheckpointId'],'C0')

if __name__=='__main__': unittest.main()

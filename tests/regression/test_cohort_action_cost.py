"""Observed costs affect scheduling order, never verification authority."""
import unittest
from cohort_action_cost import append_observation, estimate_action, observation_scope
from iterative_cohort_planner import plan_adaptive_cohort
from baseline_constraint_verifier import assignment_fingerprint

class CostSchedulingTests(unittest.TestCase):
    def rows(self, operator, names, duration, outcome='accepted'):
        return [dict(operator=operator, packages=list(names), wallSeconds=duration, outcome=outcome) for _ in range(3)]

    def plan(self, strategy, rows, priority=()):
        names=list('abcdefghij');old=dict.fromkeys(names,'1.0.0');desired=dict.fromkeys(names,'2.0.0')
        rejected={**old,**dict.fromkeys(names[:6],'2.0.0')}
        return plan_adaptive_cohort(incumbent=old,desired=desired,
            config={'cohortMaxPackages':6,'cohortSchedulingStrategy':strategy,'priorityPackages':list(priority)},
            ledger={'schedulerObservations':rows},checkpoints=[],base_checkpoint_id='C0',
            blocked_fingerprints=[assignment_fingerprint(rejected)],learned_nogoods=[],fingerprint_fn=assignment_fingerprint)

    def test_observed_fast_split_outweighs_larger_slow_whole(self):
        rows=self.rows('whole','ghij',30)+self.rows('split','abc',1)
        plan,details=self.plan('cost-aware',rows)
        self.assertEqual(len(plan.packages),3)
        self.assertEqual(details['operator'],'split')
        self.assertIsNotNone(details['costEstimate'])

    def test_unmeasured_action_keeps_size_prior(self):
        plan,details=self.plan('cost-aware',self.rows('split','abc',1))
        self.assertEqual(plan.packages,tuple('ghij'))
        self.assertIsNone(details['costEstimate'])

    def test_priority_remains_authoritative_over_speed(self):
        rows=self.rows('whole','ghij',30)+self.rows('split','abc',1)
        plan,_=self.plan('cost-aware',rows,priority=('j',))
        self.assertIn('j',plan.packages)

    def test_whole_first_ignores_split_opportunity(self):
        plan,_=self.plan('whole-first',[])
        self.assertEqual(plan.packages,tuple('ghij'))

    def test_scope_changes_with_policy_runtime_profile_and_run(self):
        run={'runId':'r'};config={'policyHash':'p','runtime':{'version':'22'},'validationScope':{'mode':'selected'}}
        scope=observation_scope(run,config)
        for changed in ({**config,'policyHash':'q'},{**config,'runtime':{'version':'24'}},{**config,'validationScope':{'mode':'all'}}):
            self.assertNotEqual(scope,observation_scope(run,changed))
        self.assertNotEqual(scope,observation_scope({'runId':'another'},config))

    def test_unknown_is_cost_evidence_without_failure_vote(self):
        rows=self.rows('whole','abcd',2,'inconclusive')
        estimate=estimate_action(tuple('abcd'),{}, {}, rows,operator='whole')
        self.assertAlmostEqual(estimate['successProbability'],2/3)

    def test_invalid_costs_do_not_enter_learning(self):
        ledger={};candidate={'delta':{'changed':{'a':'2.0.0'}}}
        for seconds in (-1,float('inf'),float('nan')):
            append_observation(ledger,scope='s',candidate=candidate,seconds=seconds,outcome='accepted')
        self.assertEqual(ledger,{})

if __name__=='__main__':unittest.main()

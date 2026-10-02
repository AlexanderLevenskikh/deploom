import unittest
from unittest import mock

from iterative_target_deferrals import record_unavailable_targets, unavailable_targets


class TargetAvailabilityDeferralsTests(unittest.TestCase):
    def setUp(self):
        self.run = {'runId': 'run-a'}
        self.config = {'policyHash': 'policy-a', 'targets': {'pkg': '99.99.99', '@demo/scoped': '2.0.0'},
                       'verifyConfig': {'registry': 'https://registry.example.invalid'}}
        self.candidate = {'candidateId': 'candidate-a', 'delta': {'changed': {'pkg': '99.99.99'}}}
        self.ledger = {}
        self.output = 'npm error code ETARGET\nnpm error notarget No matching version found for pkg@99.99.99.\n'

    def test_exact_target_is_deferred_without_an_incompatibility_fact(self):
        self.assertTrue(record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output))
        self.assertEqual({'pkg'}, unavailable_targets(self.ledger, self.run, self.config))
        self.assertNotIn('blocks', self.ledger)
        self.assertEqual('99.99.99', self.config['targets']['pkg'])

    def test_scoped_packages_and_legacy_npm_stderr_are_supported(self):
        self.candidate['delta']['changed'] = {'@demo/scoped': '2.0.0'}
        output = 'npm ERR! code ETARGET\nnpm ERR! notarget No matching version found for @demo/scoped@2.0.0.\n'
        self.assertTrue(record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, output))
        self.assertEqual({'@demo/scoped'}, unavailable_targets(self.ledger, self.run, self.config))

    def test_network_cache_timeout_and_peer_failures_do_not_defer_targets(self):
        for code in ('ETIMEDOUT', 'ENOTCACHED', 'EAI_AGAIN', 'ERESOLVE', 'E401'):
            self.assertFalse(record_unavailable_targets(self.ledger, self.run, self.config, self.candidate,
                                                        self.output.replace('ETARGET', code)))
        self.assertEqual({}, self.ledger)

    def test_transitive_or_different_version_error_is_not_a_direct_target_deferral(self):
        for spec in ('other@99.99.99', 'pkg@1.0.0'):
            self.assertFalse(record_unavailable_targets(self.ledger, self.run, self.config, self.candidate,
                                                        self.output.replace('pkg@99.99.99', spec)))

    def test_new_checkpoint_does_not_repeat_the_same_unavailable_target(self):
        record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output)
        self.run['activeCheckpointId'] = 'C5'
        self.assertEqual({'pkg'}, unavailable_targets(self.ledger, self.run, self.config))
        self.assertFalse(record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output))
        self.assertEqual(1, len(self.ledger['targetAvailabilityDeferrals']))

    def test_new_run_policy_or_requested_version_can_retry(self):
        record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output)
        self.assertEqual(set(), unavailable_targets(self.ledger, {'runId': 'run-b'}, self.config))
        self.assertEqual(set(), unavailable_targets(self.ledger, self.run, {**self.config, 'policyHash': 'policy-b'}))
        changed = {**self.config, 'targets': {'pkg': '2.0.0'}}
        self.assertEqual(set(), unavailable_targets(self.ledger, self.run, changed))

    def test_real_planner_keeps_unavailable_target_deferred_across_checkpoints(self):
        from contextlib import ExitStack
        from pathlib import Path
        import iterative_migration as migration
        self.run['activeCheckpointId'] = 'C4'
        self.config['targets'] = {'pkg': '99.99.99', 'z-ready': '2.0.0'}
        record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output)
        checkpoint = {'status': 'VERIFIED', 'fullAssignment': {'pkg': '1.0.0', 'z-ready': '1.0.0'}}
        with ExitStack() as stack:
            for name, value in [('run_budget_ok', True), ('_assert_runtime_unchanged', None),
                                ('load_candidate', None), ('load_checkpoint', checkpoint),
                                ('load_ledger', self.ledger), ('_dependency_diagnostics', {})]:
                stack.enter_context(mock.patch.object(migration, name, return_value=value))
            saved = stack.enter_context(mock.patch.object(migration, 'save_candidate'))
            stack.enter_context(mock.patch.object(migration, 'save_run'))
            events = stack.enter_context(mock.patch.object(migration, '_emit_status'))
            migration._plan_next_locked(Path('unused'), self.run, self.config)
            candidate = saved.call_args.args[1]
            self.assertEqual({'z-ready': '2.0.0'}, candidate['delta']['changed'])
            self.assertEqual('1.0.0', candidate['fullAssignment']['pkg'])
            checkpoint['fullAssignment']['z-ready'] = '2.0.0'
            self.run['activeCheckpointId'] = 'C5'
            saved.reset_mock()
            migration._plan_next_locked(Path('unused'), self.run, self.config)
            saved.assert_not_called()
            self.assertEqual('NO_ACTIONABLE', events.call_args.args[0]['reason'])
            self.assertEqual(2, events.call_args.args[0]['targets'])
        self.assertEqual('99.99.99', self.config['targets']['pkg'])

    def test_registry_context_changes_can_retry_without_persisting_coordinates(self):
        with mock.patch.dict('os.environ', {'npm_config_registry': 'https://registry.example.invalid/private-coordinate'}, clear=True):
            record_unavailable_targets(self.ledger, self.run, self.config, self.candidate, self.output)
            self.assertNotIn('private-coordinate', str(self.ledger))
            self.assertEqual({'pkg'}, unavailable_targets(self.ledger, self.run, self.config))
        with mock.patch.dict('os.environ', {'npm_config_registry': 'https://other.example.invalid'}, clear=True):
            self.assertEqual(set(), unavailable_targets(self.ledger, self.run, self.config))


if __name__ == '__main__':
    unittest.main()

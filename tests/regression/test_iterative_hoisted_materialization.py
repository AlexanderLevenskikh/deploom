"""Iterative materialization observes hoisted packages inside its private owner."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

import iterative_migration as migration
from baseline_constraint_verifier import observed_resolved_assignment, ObservedResolutionError
from iterative_stage_checkpoint import observe_materialized_assignment, operation_journal, trial_key
from project_topology import ProjectTopologyError
from tests.regression.test_iterative_materialize_retry_budget import (
    MaterializeRetryBudgetBase, _config_dict, _checkpoint_dict)


class HoistedMaterializationTests(MaterializeRetryBudgetBase):
    assignment = {'@types/react':'18.3.28', 'pkg-a':'2.0.0'}

    def tree(self, root):
        root = Path(root)
        project = root / 'packages/app'
        project.mkdir(parents=True, exist_ok=True)
        (root/'package.json').write_text(json.dumps({'private':True,'workspaces':['packages/*']}),encoding='utf-8')
        (root/'yarn.lock').write_text('__metadata:\n  version: 8\n',encoding='utf-8')
        (project/'package.json').write_text(json.dumps({'name':'fixture-app','dependencies':self.assignment}),encoding='utf-8')
        for name, version in self.assignment.items():
            manifest = root/'node_modules'/name/'package.json'
            manifest.parent.mkdir(parents=True,exist_ok=True)
            manifest.write_text(json.dumps({'name':name,'version':version}),encoding='utf-8')
        return project

    def test_fresh_controller_materialization_and_recovery_observe_same_hoisted_assignment(self):
        run_dir = self.make_run_dir()
        config = _config_dict()
        migration.save_config(run_dir,config)
        migration.save_checkpoint(run_dir,_checkpoint_dict(assignment=self.assignment,projectRelative='packages/app'))
        candidate = {'schemaVersion':1, 'candidateId':'hoisted','runId':migration.load_run(run_dir)['runId'],
                     'baseCheckpointId':'C0','stage':'PLANNED','fullAssignment':self.assignment,'attemptId':0}
        migration.save_candidate(run_dir,candidate)
        root = migration.trial_workspace_root(run_dir)
        with mock.patch.object(migration,'run_budget_ok',return_value=True), \
             mock.patch.object(migration,'_assert_runtime_unchanged'), \
             mock.patch.object(migration,'_open_checkpoint_source',return_value=SimpleNamespace(root=self.tmp)), \
             mock.patch.object(migration,'_materialize_trial_tree',side_effect=lambda source,target,timeout:self.tree(target)), \
             mock.patch.object(migration,'_run_install',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as install, \
             mock.patch.object(migration,'_emit_status'):
            self.assertEqual(migration._materialize_locked(run_dir,migration.load_run(run_dir),config,SimpleNamespace(timeout_seconds=30)),0)
            saved = migration.load_candidate(run_dir)
            self.assertEqual(saved['stage'],'MATERIALIZED')
            self.assertEqual(saved['materializationRefs']['observedResolvedVersions'],self.assignment)
            # A sealed diagnostic materialization receipt with identical physical inputs.
            journal = operation_journal(run_dir,migration.load_run(run_dir),config,saved,'materialize')
            journal.write('materialize','PASS',{'workspaceRoot':str(root.resolve()),'artifactKey':trial_key(root),
                          'observed':self.assignment,'candidate':saved})
            self.assertEqual(migration._materialize_locked(run_dir,migration.load_run(run_dir),config,SimpleNamespace(timeout_seconds=30)),0)
            self.assertEqual(install.call_count,1,'valid receipt resumes without installing again')

    def test_shadowed_wrong_version_and_missing_packages_are_still_drift(self):
        root = self.tmp/'workspace'
        project = self.tree(root)
        self.assertEqual(observe_materialized_assignment(project,root,self.assignment,observed_resolved_assignment),self.assignment)
        shadow = project/'node_modules/@types/react/package.json'
        shadow.parent.mkdir(parents=True)
        shadow.write_text('{"name":"@types/react","version":"19.0.0"}',encoding='utf-8')
        with self.assertRaises(ObservedResolutionError):
            observe_materialized_assignment(project,root,self.assignment,observed_resolved_assignment)
        shadow.unlink()
        (root/'node_modules/@types/react/package.json').unlink()
        with self.assertRaisesRegex(ObservedResolutionError,'ASSIGNMENT_MISSING'):
            observe_materialized_assignment(project,root,self.assignment,observed_resolved_assignment)

    def test_unowned_ancestor_and_project_outside_trial_are_rejected(self):
        root = self.tmp/'workspace'
        project = self.tree(root)
        with self.assertRaisesRegex(ValueError,'OUTSIDE_TRIAL'):
            observe_materialized_assignment(project,project/'nested',self.assignment,observed_resolved_assignment)
        (root/'package.json').write_text('{"private":true,"workspaces":["other/*"]}',encoding='utf-8')
        with self.assertRaisesRegex(ProjectTopologyError,'CANONICAL_LOCKFILE_MISSING'):
            observe_materialized_assignment(project,root,self.assignment,observed_resolved_assignment)


if __name__ == '__main__':
    unittest.main()

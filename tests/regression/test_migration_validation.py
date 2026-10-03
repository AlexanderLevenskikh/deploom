from __future__ import annotations
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest
from baseline_constraint_verifier import BaselineVerifyConfig, BaselineVerifyResult, BaselineProjectFailure
from migration_validation import EvidenceError, compare, digest, normalize_vitest, validate_profile, verify_initial_control, evidence_command


class TestMigrationValidation(unittest.TestCase):
    def report(self, project, statuses=("passed", "failed")):
        return {"numTotalTests": len(statuses), "numFailedTests": statuses.count("failed"), "testResults": [{"name": str(project / "src" / "unit.test.ts"), "status": "failed" if "failed" in statuses else "passed", "assertionResults": [{"fullName": f"case {i}", "status": status, "failureMessages": ["expected 1 to equal 2"] if status == "failed" else []} for i, status in enumerate(statuses)]}]}

    def test_known_failure_does_not_hide_new_failure(self):
        p=Path.cwd(); before=normalize_vitest(self.report(p),p)
        self.assertEqual(compare(before,before)["existingFailures"],1)
        with self.assertRaisesRegex(EvidenceError,"TEST_REGRESSION"):
            compare(before,normalize_vitest(self.report(p,("failed","failed")),p))

    def test_changed_failure_is_a_regression(self):
        p=Path.cwd(); before=normalize_vitest(self.report(p),p); after=json.loads(json.dumps(before)); after['cases']['src/unit.test.ts::case 1']['failureHash']='different'
        with self.assertRaisesRegex(EvidenceError,'TEST_REGRESSION'): compare(before,after)

    def test_missing_and_skipped_cases_are_not_fixes(self):
        p=Path.cwd(); before=normalize_vitest(self.report(p),p)
        with self.assertRaisesRegex(EvidenceError,'TEST_COVERAGE_LOST'): compare(before,normalize_vitest(self.report(p,('passed',)),p))
        with self.assertRaisesRegex(EvidenceError,'TEST_BECAME_SKIPPED'): compare(before,normalize_vitest(self.report(p,('passed','pending')),p))
        self.assertEqual(compare(before,normalize_vitest(self.report(p,('passed','passed')),p))['existingFailures'],0)

    def test_collection_error_is_not_an_acceptable_baseline(self):
        p=Path.cwd(); report=self.report(p); report['testResults'][0]['assertionResults']=[]
        with self.assertRaisesRegex(EvidenceError,'COLLECTION'): normalize_vitest(report,p)
        report['testResults'][0]['status']='passed'
        report['testResults'][0]['message']='external server missing'
        with self.assertRaisesRegex(EvidenceError,'COLLECTION'): normalize_vitest(report,p)

    def test_unhandled_error_duplicate_id_and_counters_fail_closed(self):
        p=Path.cwd()
        for mutation in ('unhandled','duplicate','counters'):
            raw=self.report(p)
            if mutation=='unhandled': raw['unhandledErrors']=['boom']
            if mutation=='duplicate': raw['testResults'][0]['assertionResults'][1]['fullName']='case 0'
            if mutation=='counters': raw['numTotalTests']=99
            with self.subTest(mutation=mutation), self.assertRaises(EvidenceError): normalize_vitest(raw,p)

    def test_profile_requires_explicit_single_vitest_command(self):
        with self.assertRaises(ValueError): validate_profile({'commands':[]})
        with self.assertRaises(ValueError): validate_profile({'commands':['yarn test && yarn build'],'unitCommand':'yarn test && yarn build','compareExistingFailures':True})
        with self.assertRaises(ValueError): validate_profile({'commands':['yarn build'],'unitCommand':'yarn test','compareExistingFailures':True})
        self.assertFalse(validate_profile({'commands':['yarn build']})['compareExistingFailures'])

    def test_control_seals_then_reverifies_with_hash_bound_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); profile=validate_profile({'commands':['yarn build','yarn test --run src'],'unitCommand':'yarn test --run src','compareExistingFailures':True,'deferredChecks':'E2E need a server'})
            config={'validationProfile':profile,'verifyConfig':{'commands':profile['commands']}}
            calls=[]
            def verify(project,assignment,*,config,**kwargs):
                calls.append(config.commands)
                if len(calls)==1:
                    from migration_validation import internal_command_argv
                    command=config.commands[-1]; parts=internal_command_argv(command)
                    Path(parts[parts.index('--observation')+1].strip('"')).write_text(json.dumps(normalize_vitest(self.report(root),root)),encoding='utf-8')
                    return BaselineVerifyResult(False,'project','red',project_failures=(BaselineProjectFailure(command,1,'failed'),),resolved_state_key='initial')
                return BaselineVerifyResult(True,'passed','compared')
            result=verify_initial_control(root,{},run_config=config,run_dir=root,verify_config=BaselineVerifyConfig(commands=tuple(profile['commands'])),verifier=verify)
            self.assertTrue(result.ok); self.assertEqual(len(calls),2)
            scope=config['validationScope']; self.assertEqual(scope['existingFailures'],1)
            from migration_validation import internal_command_argv
            argv=internal_command_argv(calls[1][-1]); self.assertIn(scope['baselineHash'],argv); self.assertNotIn('--observation',argv)
            self.assertEqual(scope['baselineHash'],digest(json.loads(Path(scope['baselinePath']).read_text(encoding='utf-8'))))

    def test_other_failed_command_and_unknown_are_never_grandfathered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); profile=validate_profile({'commands':['yarn build','yarn test'],'unitCommand':'yarn test','compareExistingFailures':True}); cfg={'validationProfile':profile,'verifyConfig':{'commands':profile['commands']}}
            for kind,failures in [('infrastructure',()),('project',(BaselineProjectFailure('yarn build',1,'error'),))]:
                calls=[]
                def verify(*a,**kw): calls.append(kw); return BaselineVerifyResult(False,kind,'red',project_failures=failures)
                result=verify_initial_control(root,{},run_config=cfg,run_dir=root,verify_config=BaselineVerifyConfig(commands=tuple(profile['commands'])),verifier=verify)
                self.assertFalse(result.ok); self.assertEqual(len(calls),1); self.assertNotIn('validationScope',cfg)

    def test_baseline_tampering_and_missing_file_fail_closed(self):
        from migration_validation import validate_baseline_reference
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'baseline.json'; baseline={'cases':{'case':{'status':'passed'}}}; path.write_text(json.dumps(baseline),encoding='utf-8')
            mapping={'testBaseline':{'path':str(path),'hash':digest(baseline)}}
            validate_baseline_reference(mapping)
            path.write_text('{}',encoding='utf-8')
            with self.assertRaises(EvidenceError): validate_baseline_reference(mapping)
            path.unlink()
            with self.assertRaises(EvidenceError): validate_baseline_reference(mapping)

    def test_repair_only_never_claims_existing_failures_are_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile=validate_profile({'commands':['yarn test'],'unitCommand':'yarn test','compareExistingFailures':True})
            cfg={'repairOnly':True,'validationProfile':profile,'verifyConfig':{'commands':profile['commands']}}
            calls=[]
            def verifier(*a,**kw): calls.append(kw['config'].commands); return BaselineVerifyResult(False,'project','still red')
            result=verify_initial_control(Path(tmp),{},run_config=cfg,run_dir=Path(tmp),verify_config=BaselineVerifyConfig(commands=('yarn test',)),verifier=verifier)
            self.assertFalse(result.ok); self.assertEqual(calls,[('yarn test',)]); self.assertNotIn('validationScope',cfg)

    def test_native_protocol_preserves_spaces_and_unicode_without_shell(self):
        from migration_validation import internal_command_argv
        command=evidence_command('npm run test -- --run src',observation=Path('with spaces')/'тест.json')
        argv=internal_command_argv(command)
        self.assertEqual(argv[argv.index('--command')+1],'npm run test -- --run src')
        self.assertEqual(argv[argv.index('--observation')+1],str(Path('with spaces')/'тест.json'))
        self.assertIsNone(internal_command_argv('npm run build'))

    def test_comparison_semantics_change_invalidates_proof_namespace(self):
        import substrate_identity
        from unittest import mock
        def build(adapter_digest):
            substrate_identity.tool_build_components.cache_clear(); substrate_identity.tool_build_id.cache_clear()
            with mock.patch.object(substrate_identity, '_file_digest', side_effect=lambda path: adapter_digest if path.name == 'migration_validation.py' else 'unchanged'):
                return substrate_identity.tool_build_id()
        try: self.assertNotEqual(build('adapter-before'),build('adapter-after'))
        finally:
            substrate_identity.tool_build_components.cache_clear(); substrate_identity.tool_build_id.cache_clear()

if __name__=='__main__': unittest.main()

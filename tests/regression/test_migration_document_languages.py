"""Document language, missing evidence, reconstruction and deterministic output."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import migration_documents as docs


class MigrationDocumentLanguageTests(unittest.TestCase):
    def data(self):
        before=b'{"name":"fixture","dependencies":{"demo-lib":"1.0.0"}}'
        after=b'{"name":"fixture","dependencies":{"demo-lib":"2.0.0"},"devDependencies":{"demo-tool":"^3.0.0"}}'
        data=docs.reconstruct(before,after)
        data['config']['verifyConfig']={'commands':['npm run lint:types','npm run build','npm run test']}
        return data

    def test_catalogs_complete_and_missing_key_fails_fast(self):
        self.assertGreater(len(docs.check_catalogs()),40)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for language in ['en','ru']:
                value=docs.catalog(language)
                if language=='ru':value.pop('checks')
                (root/(language+'.json')).write_text(json.dumps(value),encoding='utf-8')
            with mock.patch.object(docs,'CATALOG_ROOT',root):
                with self.assertRaisesRegex(docs.DocumentError,'TRANSLATION_KEYS_MISMATCH'):
                    docs.generate_documents(self.data(),root/'output','ru')
                self.assertFalse((root/'output').exists())
        with self.assertRaisesRegex(docs.DocumentError,'LANGUAGE_UNSUPPORTED'):docs.catalog('de')

    def test_ru_whole_set_default_en_and_machine_values_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=self.data()
            with mock.patch.dict(os.environ,{'LANG':'ru_RU.UTF-8','LC_ALL':'ru_RU.UTF-8'}):
                docs.generate_documents(data,root/'english')
            docs.generate_documents(data,root/'russian','ru')
            for name in docs.DOCUMENTS:
                english=(root/'english'/name).read_text(encoding='utf-8')
                russian=(root/'russian/ru'/name).read_text(encoding='utf-8')
                self.assertIn('## Missing information',english)
                self.assertIn('## Чего не хватает',russian)
                self.assertNotIn('## Missing information',russian)
                self.assertNotIn('Data unavailable:',russian)
                self.assertIn('прогона утилиты не было',russian)
                self.assertNotIn('Артефакты расходятся',russian)
                self.assertFalse((root/'russian'/name).exists())
            report=(root/'russian/ru/MIGRATION_REPORT.md').read_text(encoding='utf-8')
            for literal in ['demo-lib','1.0.0','2.0.0','dependencies','npm run lint:types','npm run build','UNVERIFIED',data['after']['manifestHash']]:
                self.assertIn(literal,report)
            for name in ['pre-fix-audit-report.json','post-fix-audit-report.json','documentation-evidence.json']:
                self.assertEqual((root/'english'/name).read_bytes(),(root/'russian'/name).read_bytes())

    def test_absent_c0_audit_still_produces_pre_fix_missing_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'checkpoints').mkdir()
            source=root/'source/tree';source.mkdir(parents=True)
            manifest=source/'package.json';manifest.write_text('{"name":"fixture","dependencies":{"demo-lib":"1.0.0"}}',encoding='utf-8')
            checkpoint={'checkpointId':'C0','status':'VERIFIED','sourceHead':'a'*40,
                        'manifestHash':docs.sha(manifest.read_bytes()),'sourceSnapshotContainer':'source',
                        'fullAssignment':{'demo-lib':'1.0.0'}}
            (root/'checkpoints/C0.json').write_text(docs.stable(checkpoint),encoding='utf-8')
            run={'runId':'iter-fixture','activeCheckpointId':'C0'};config={'documentLanguage':'ru','projectName':'fixture'}
            docs.write_pre_fix_report(root,run,config,checkpoint)
            text=(root/'reports/ru/pre-fix-audit-report.md').read_text(encoding='utf-8')
            self.assertIn('Данные недоступны: `audit.pre`',text)
            self.assertIn('N/A (не зафиксировано)',text)
            self.assertIn('C0',text)
            self.assertIn('a'*40,text)
            self.assertIn('| `manifestHash` | '+docs.sha(manifest.read_bytes())+' |',text)
            self.assertIsNone(json.loads((root/'reports/pre-fix-audit-report.json').read_text(encoding='utf-8'))['audit'])

    def test_manifest_hash_and_count_discrepancies_are_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary);manifest=source/'package.json'
            manifest.write_text('{"dependencies":{"demo-lib":"2.0.0"}}',encoding='utf-8')
            checkpoint={'sourceSnapshotContainer':str(source),'manifestHash':'0'*64,'checkpointId':'C1'}
            facts=docs.source_facts(checkpoint,{'directDeclarations':99})
            self.assertEqual(facts['bindingStatus'],'UNVERIFIED')
            self.assertEqual(facts['directDeclarationsActual'],1)
            data=self.data();data['after']=facts
            text=docs.migration_document(data,'ru')
            self.assertIn('Артефакты расходятся',text)
            self.assertIn('UNVERIFIED',text)
            self.assertIn('0'*64,text)
            self.assertIn(docs.sha(manifest.read_bytes()),text)

    def test_two_manifest_cli_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'old.json').write_text('{"dependencies":{"demo-lib":"1.0.0"}}',encoding='utf-8')
            (root/'new.json').write_text('{"dependencies":{"demo-lib":"2.0.0"}}',encoding='utf-8')
            command=[sys.executable,str(Path(docs.__file__)), '--before-manifest',str(root/'old.json'),
                     '--after-manifest',str(root/'new.json'),'--lang','ru']
            for name in ['first','second']:
                result=subprocess.run([*command,'--output-dir',str(root/name)],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)
            for path in (root/'first').rglob('*'):
                if path.is_file():self.assertEqual(path.read_bytes(),(root/'second'/path.relative_to(root/'first')).read_bytes())
            text=(root/'first/ru/MIGRATION_REPORT.md').read_text(encoding='utf-8')
            self.assertIn('| dependencies | demo-lib | 1.0.0 | 2.0.0 |',text)
            self.assertIn('прогона утилиты не было',text)

    def test_missing_source_does_not_claim_no_updates(self):
        data={'run':{'activeCheckpointId':'C1'},'config':{},'checkpoints':[
            {'checkpointId':'C0','status':'VERIFIED','fullAssignment':{'demo-lib':'1.0.0'},'acceptedDelta':{'changed':{}}},
            {'checkpointId':'C1','parentCheckpointId':'C0','status':'VERIFIED','fullAssignment':{'demo-lib':'2.0.0'},'acceptedDelta':{'changed':{'demo-lib':'2.0.0'}}}],
              'before':{},'after':{}}
        text=docs.migration_document(docs.complete_inputs(data),'en',guide=True)
        self.assertNotIn('No accepted version changes',text)
        self.assertIn('| demo-lib | 1.0.0 | 2.0.0 | C1 | UNKNOWN |',text)
        data['after']={'bindingStatus':'UNVERIFIED'}
        unverified=docs.migration_document(docs.complete_inputs(data),'en',guide=True)
        self.assertIn('| demo-lib | 1.0.0 | 2.0.0 | C1 | UNVERIFIED |',unverified)
        self.assertIn('Data unavailable: `before.',text)

    def test_git_range_reconstruction_reads_exact_blobs_without_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir()
            def git(*args):
                result=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)
                return result.stdout.strip()
            git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
            for version in ['1.0.0','2.0.0']:
                (repo/'package.json').write_text(json.dumps({'name':'fixture','dependencies':{'demo-lib':version}}),encoding='utf-8')
                (repo/'package-lock.json').write_text(json.dumps({'lockfileVersion':3,'packages':{'node_modules/demo-lib':{'version':version}}}),encoding='utf-8')
                git('add','.');git('commit','-m',version)
                if version=='1.0.0':base=git('rev-parse','HEAD')
            head=git('rev-parse','HEAD')
            command=[sys.executable,str(Path(docs.__file__)),'--repository',str(repo),'--range',base+'..'+head,
                     '--lock-path','package-lock.json','--lang','ru','--output-dir',str(root/'output')]
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(git('rev-parse','HEAD'),head)
            self.assertEqual(git('status','--porcelain'),'')
            text=(root/'output/ru/MIGRATION_REPORT.md').read_text(encoding='utf-8')
            self.assertIn('Отчёт реконструирован по истории репозитория',text)
            self.assertIn('node_modules/demo-lib',text)
            self.assertIn('| dependencies | demo-lib | 1.0.0 | 2.0.0 |',text)
            self.assertIn(base+'..'+head,(root/'output/documentation-evidence.json').read_text(encoding='utf-8'))

    def test_committed_manifest_is_checked_against_checkpoint_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir();run_dir=root/'run';run_dir.mkdir()
            (repo/'package.json').write_bytes(b'{"dependencies":{"demo-lib":"2.0.0"}}')
            for args in [('init',),('config','user.email','fixture@example.invalid'),('config','user.name','Fixture'),('add','.'),('commit','-m','fixture')]:
                result=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stderr)
            head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
            source=root/'sealed';source.mkdir();(source/'package.json').write_bytes(b'{"dependencies":{"demo-lib":"3.0.0"}}')
            checkpoint={'checkpointId':'C1','sourceSnapshotContainer':str(source),'manifestHash':docs.sha((source/'package.json').read_bytes())}
            delivery={'status':'done','head':head,'workspaceRoot':str(repo),'projectRelative':'.'}
            (run_dir/'delivery-state.json').write_text(docs.stable(delivery),encoding='utf-8')
            data=docs.run_inputs(run_dir,{'activeCheckpointId':'C1'},{},[checkpoint])
            self.assertEqual(data['committedManifestHash'],docs.sha((repo/'package.json').read_bytes()))
            self.assertEqual(data['committedManifestBinding'],'UNVERIFIED')
            text=docs.migration_document(docs.complete_inputs(data),'ru')
            self.assertIn('Данные недоступны: `committedManifestBinding`',text)
            self.assertIn('Артефакты расходятся',text)

    def test_delivery_refreshes_audits_without_overwriting_primary_explanations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=self.data()
            def audit(high):
                return {'audit':{'complete':True,'packageTotals':{'critical':0,'high':high,'moderate':0,'low':0,'unknown':0}}}
            data['postAudit']=audit(1);docs.generate_documents(data,root,'ru')
            primary=root/'ru/MIGRATION_REPORT.md';primary.write_text('Пояснения разработчика',encoding='utf-8')
            data['postAudit']=audit(2);docs.generate_documents(data,root,'ru',preserve_primary=True)
            self.assertEqual(primary.read_text(encoding='utf-8'),'Пояснения разработчика')
            self.assertIn('| `packageTotals.high` | 2 |',(root/'ru/post-fix-audit-report.md').read_text(encoding='utf-8'))
            self.assertEqual(json.loads((root/'post-fix-audit-report.json').read_text(encoding='utf-8')),audit(2))

    def test_document_language_does_not_reset_negative_evidence_scope(self):
        from iterative_stage_checkpoint import negative_evidence_scope
        checkpoint={'sourceSnapshotKey':'sealed-source'}
        config={'projectDir':'fixture','verifyConfig':{'commands':['npm run lint']},'documentLanguage':'en'}
        with mock.patch('iterative_stage_checkpoint.operation_environment',return_value={'PATH':'stable'}), mock.patch('iterative_stage_checkpoint.command_tool_closure',return_value={'node':'stable'}):
            scope=negative_evidence_scope(Path('unused'),config,'C0',checkpoint=checkpoint)
            self.assertEqual(scope,negative_evidence_scope(Path('unused'),{**config,'documentLanguage':'ru'},'C0',checkpoint=checkpoint))
            self.assertNotEqual(scope,negative_evidence_scope(Path('unused'),{**config,'verifyConfig':{'commands':['npm run test']}},'C0',checkpoint=checkpoint))
            self.assertNotEqual(scope,negative_evidence_scope(Path('unused'),config,'C0',checkpoint={'sourceSnapshotKey':'changed-source'}))

    def test_no_zero_vulnerabilities_claim_for_incomplete_evidence(self):
        report={'audit':{'complete':False,'packageTotals':{'critical':0,'high':0}},'lag':[]}
        text=docs.audit_document(report,'ru',phase='pre')
        self.assertIn('| `packageTotals.critical` | N/A (не зафиксировано) |',text)
        self.assertIn('audit.completeEvidence',text)
        self.assertIn('неполные',text.lower())

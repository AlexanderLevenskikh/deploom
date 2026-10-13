"""Physical verification must retain sibling resources and root-level tooling."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import baseline_constraint_verifier as verifier
import source_snapshot as source

@unittest.skipUnless(shutil.which("node") and shutil.which("npm") and shutil.which("git"), "node/npm/git required")
class VerificationSourceClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deploom-source-closure-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.app = self.repo / "packages/app"
        self.lib = self.repo / "packages/lib"
        self.app.mkdir(parents=True); self.lib.mkdir(parents=True)
        self.write("package.json", json.dumps({"name":"closure-root","private":True,"workspaces":["packages/*"]}))
        self.write("packages/app/package.json", json.dumps({"name":"closure-app","version":"1.0.0","dependencies":{"closure-lib":"1.0.0"}}))
        self.write("packages/lib/package.json", json.dumps({"name":"closure-lib","version":"1.0.0","main":"index.cjs"}))
        self.write("packages/lib/index.cjs", "module.exports=42;\n")
        self.write("packages/lib/src/view.scss", '@use "../../../shared/styles/variables";\n')
        self.write("shared/styles/variables.scss", "$color: red;\n")
        self.write("frontend-tools/test/reporter.mjs", "export default class Reporter {}\n")
        self.write("tools/task-graph.json", '{"build":["closure-lib"]}\n')
        self.write("tsconfig.base.json", '{"compilerOptions":{"strict":true}}\n')
        self.write(".gitignore", "node_modules/\nignored-input/\n")
        self.write("ignored-input/required.txt", "authoritative ignored source\n")
        self.paths=("shared/styles/variables.scss","frontend-tools/test/reporter.mjs","tools/task-graph.json","ignored-input/required.txt")
        self.write("packages/app/check.cjs", """const fs=require('fs'),path=require('path');
const root=path.resolve('../..');
if(require('closure-lib')!==42)process.exit(41);
for(const item of %s)if(!fs.existsSync(path.join(root,item)))throw new Error('MISSING_SOURCE '+item);
(async()=>{const {default:Reporter}=await import(require('url').pathToFileURL(path.join(root,'frontend-tools/test/reporter.mjs')));if(typeof Reporter!=='function')process.exit(42);console.log('CLOSURE_CHECK_PASS')})().catch(e=>{console.error(e);process.exit(43)});
""" % json.dumps(self.paths))
        argv,_=verifier._command_prefix(shutil.which("npm"), ["install","--ignore-scripts","--no-audit","--no-fund"])
        result=subprocess.run(argv,cwd=self.repo,capture_output=True,text=True,timeout=120)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        for args in (("init","-q"),("config","user.name","Closure Fixture"),("config","user.email","fixture@example.invalid"),("add","."),("commit","-qm","closure fixture")):
            subprocess.run(["git","-C",str(self.repo),*args],check=True,capture_output=True)
        self.env=patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"project-gitignore","DEPLOOM_VERIFICATION_ROOT":str(self.base/"storage")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.addCleanup(source.clear_source_snapshot_epochs)

    def write(self, relative, text):
        path=self.repo/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text,encoding="utf-8")

    def config(self, command="node check.cjs"):
        return verifier.BaselineVerifyConfig(project_checks="strict",commands=(command,),timeout_seconds=120,attempt_timeout_seconds=360,proof_cache_dir=str(self.base/"proofs"))

    def test_sibling_styles_root_reporter_and_ignored_input_exist_during_real_check(self):
        observed=[]; contexts=[]; actual_cwds=[]; real_run=verifier._run
        def progress(message):
            if message.startswith("VERIFICATION_EXECUTION_SCOPE "):
                contexts.append(json.loads(message.split(" ",1)[1]))
        def observing_run(argv,cwd,**kwargs):
            if kwargs.get("progress_label")=="node check.cjs":
                actual_cwds.append(str(Path(cwd).resolve()))
                root=Path(cwd).resolve().parents[1]
                observed.append({name:(root/name).is_file() for name in self.paths})
            return real_run(argv,cwd,**kwargs)
        with patch.object(verifier,"_run",observing_run):
            result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True,progress=progress)
        self.assertEqual(actual_cwds,[item["executionCwd"] for item in contexts])
        self.assertTrue(all(item["sourceClosure"]["confidence"] == "complete-under-source-policy" for item in contexts))
        self.assertTrue(observed,"real project check never ran: "+result.summary+"\n"+result.output)
        self.assertTrue(all(all(values.values()) for values in observed),observed)
        self.assertTrue(result.ok,result.summary+"\n"+result.output)
        self.assertEqual("passed",result.kind)

    def test_missing_in_original_is_a_real_project_failure(self):
        (self.repo / self.paths[0]).unlink()
        result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertFalse(result.ok)
        self.assertEqual("project",result.kind,result.summary)
        self.assertIn("MISSING_SOURCE",result.output)
        self.assertIn(self.paths[0],result.output)

    def corrupt_clone(self, *, change_bytes):
        self.write("packages/app/check.cjs", "console.log('NOOP_CHECK_SUCCESS');\n")
        original=verifier._materialize_prepared_workspace_snapshot
        calls=[]
        def broken_copy(snapshot,target,**kwargs):
            project=original(snapshot,target,**kwargs)
            target_file=Path(target)/self.paths[1]
            import stat
            target_file.chmod(target_file.stat().st_mode | stat.S_IWRITE)
            if change_bytes:
                data=target_file.read_bytes(); target_file.write_bytes(data.replace(b"default",b"defaulX"))
            else:
                target_file.unlink()
            calls.append(str(target))
            return project
        with patch.object(verifier,"_materialize_prepared_workspace_snapshot",broken_copy):
            result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertTrue(calls)
        self.assertFalse(result.ok,"corrupted source upper cannot publish zero-exit success")
        self.assertEqual("infrastructure",result.kind,result.summary)
        self.assertIn("VERIFICATION_SOURCE_CLOSURE_MATERIALIZED_MISMATCH",result.summary)
        self.assertIn(self.paths[1],result.summary)

    def test_copy_omission_cannot_pass_a_zero_exit_command(self):
        self.corrupt_clone(change_bytes=False)

    def test_same_size_content_mutation_cannot_pass_a_zero_exit_command(self):
        self.corrupt_clone(change_bytes=True)

    def test_legacy_partial_snapshot_is_unproven_even_for_zero_exit(self):
        self.write("packages/app/check.cjs", "process.exit(0);\n")
        policy=source.SourceInputPolicy(scope_roots=("packages/app","packages/lib"),excluded_relative_paths=("shared","frontend-tools","tools","ignored-input"))
        snapshot=source.capture_source_snapshot(self.app,policy=policy)
        self.addCleanup(source._force_rmtree,snapshot.container)
        with source.bind_source_snapshot(snapshot):
            result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertFalse(result.ok)
        self.assertIn(result.kind,("infrastructure","unknown"))
        self.assertIn("VERIFICATION_SOURCE_CLOSURE_UNPROVEN",result.summary)

    def test_frozen_subject_mutation_after_capture_is_rejected(self):
        with source.verification_source_snapshot_scope(self.app) as snapshot:
            import stat
            path=snapshot.root/self.paths[0]
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.write_text("$color: blue;\n",encoding="utf-8")
            result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertFalse(result.ok)
        self.assertIn(result.kind,("infrastructure","unknown"))
        self.assertIn("SOURCE_SNAPSHOT_CONTENT_MISMATCH",result.summary)

    def test_task_graph_change_invalidates_exact_reuse(self):
        calls=[]; real_run=verifier._run
        def measured(argv,cwd,**kwargs):
            if kwargs.get("progress_label")=="package-manager lifecycle install":calls.append(str(cwd))
            return real_run(argv,cwd,**kwargs)
        with patch.object(verifier,"_run",measured):
            first=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
            self.assertTrue(first.ok,first.summary)
            calls.clear()
            self.write("tools/task-graph.json", '{"build":["closure-lib"],"configurationChanged":true}\n')
            second=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertTrue(second.ok,second.summary)
        self.assertTrue(calls,"changed task graph reused preparation for another source subject")

    def test_external_directory_link_is_not_trusted(self):
        external=self.base/"outside";external.mkdir();(external/"input.txt").write_text("outside",encoding="utf-8")
        link=self.repo/"tools/external"
        if os.name=="nt":
            result=subprocess.run([os.environ.get("COMSPEC","cmd.exe"),"/d","/c","mklink","/J",str(link),str(external)],capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        else:
            link.symlink_to(external,target_is_directory=True)
        result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
        self.assertFalse(result.ok)
        self.assertIn(result.kind,("infrastructure","unknown"))
        self.assertRegex(result.summary,"EXTERNAL_TARGET_UNSUPPORTED|TARGET_ESCAPE")

    def test_workspace_cache_writes_do_not_force_another_lifecycle_install(self):
        manifest=json.loads((self.repo/"package.json").read_text(encoding="utf-8"))
        manifest["scripts"]={"postinstall":"node create-cache.cjs"}
        self.write("package.json",json.dumps(manifest))
        self.write("create-cache.cjs", "const fs=require('fs');fs.mkdirSync('node_modules/.cache/fixture',{recursive:true});fs.writeFileSync('node_modules/.cache/fixture/cache.txt','ephemeral');")
        self.write("packages/app/check.cjs", "const fs=require('fs'),path=require('path');if(require('closure-lib')!==42)process.exit(44);const cache=path.resolve('../../node_modules/.cache/fixture');fs.mkdirSync(cache,{recursive:true});fs.writeFileSync(path.join(cache,'cache.txt'),'check cache');")
        counts=[];real=verifier._run
        for attempt in range(2):
            stages=[]
            def observe(argv,cwd,**kwargs):
                if kwargs.get("progress_label","") in ("package-manager resolver install","package-manager lifecycle install"):
                    stages.append(kwargs["progress_label"])
                return real(argv,cwd,**kwargs)
            with patch.object(verifier,"_run",observe):
                result=verifier.verify_assignment(self.app,{},config=self.config(),run_project_checks=True)
            self.assertTrue(result.ok,result.summary+"\n"+result.output)
            counts.append(stages)
        self.assertEqual(2,len(counts[0]),counts)
        self.assertEqual([],counts[1],counts)
        self.assertFalse((self.repo/"node_modules/.cache/fixture").exists(),"project checks must leave original sources untouched")

    def test_single_package_source_root_stays_local(self):
        app=self.base/"standalone";app.mkdir()
        (app/"package.json").write_text('{"name":"standalone","version":"1.0.0"}',encoding="utf-8")
        (app/"source.cjs").write_text("module.exports=1;\n",encoding="utf-8")
        snapshot=source.capture_source_snapshot(app)
        self.addCleanup(source._force_rmtree,snapshot.container)
        self.assertEqual(app.resolve(),snapshot.capture_root)
        self.assertEqual(Path("."),snapshot.project_relative)
        self.assertFalse((snapshot.root/"repo").exists())

if __name__=="__main__": unittest.main()

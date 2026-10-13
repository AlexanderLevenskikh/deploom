"""Actual Nx dependency task, sibling Sass import and root Vitest reporter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import baseline_constraint_verifier as verifier
import source_snapshot as source

@unittest.skipUnless(os.environ.get("DEPLOOM_TEST_NODE_SOURCE_CLOSURE")=="1", "enable pinned Nx/Sass/Vitest physical acceptance")
class UniversalNodeSourceClosurePhysical(unittest.TestCase):
    def test_real_toolchain_cold_warm_keeps_complete_source_and_exact_dependency_reuse(self):
        with tempfile.TemporaryDirectory(prefix="deploom-node-closure-") as raw:
            base=Path(raw).resolve();root=base/"repo";app=root/"packages/app"
            def write(name,text):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding="utf-8")
            write("package.json",json.dumps({"name":"node-closure-root","private":True,"workspaces":["packages/*"],"devDependencies":{"nx":"21.2.1","sass":"1.89.2","vitest":"3.2.4"}}))
            write("nx.json",json.dumps({"targetDefaults":{"build":{"dependsOn":["^build"],"cache":False}}}))
            write("packages/app/package.json",json.dumps({"name":"closure-app","version":"1.0.0","dependencies":{"closure-style":"1.0.0"},"scripts":{"build":"nx run closure-app:build","test":"vitest run --minWorkers=1 --maxWorkers=1"},"nx":{"targets":{"build":{"executor":"nx:run-commands","options":{"command":"node tools/verify-style.cjs"}}}}}))
            write("packages/style/package.json",json.dumps({"name":"closure-style","version":"1.0.0","main":"index.cjs","nx":{"targets":{"build":{"executor":"nx:run-commands","options":{"command":"node node_modules/sass/sass.js packages/style/src/view.scss dist/style.css"}}}}}))
            write("packages/style/index.cjs","module.exports=42;")
            write("packages/style/src/view.scss",'@use "../../../shared/styles/variables"; .fixture {color: variables.$color;}')
            write("shared/styles/variables.scss","$color: red;")
            write("tools/verify-style.cjs","const fs=require('fs');if(!fs.readFileSync('dist/style.css','utf8').includes('red'))process.exit(44);console.log('DEPENDENT_STYLE_TASK_CONFIRMED');")
            write("frontend-tools/test/reporter.mjs","import fs from 'node:fs';export default class Reporter {onFinished(files, errors){if(!files.length||errors.length)throw new Error('REPORTER_MISSING_COVERAGE');fs.writeFileSync('reporter-observed.txt','root reporter executed');console.log('ROOT_REPORTER_CONFIRMED')}}")
            write("packages/app/vitest.config.mjs","import {defineConfig} from 'vitest/config';import Reporter from '../../frontend-tools/test/reporter.mjs';export default defineConfig({test:{reporters:[new Reporter()],include:['unit.test.ts']}});")
            write("packages/app/unit.test.ts","import {expect,test} from 'vitest';import value from 'closure-style';test('sibling package',()=>expect(value).toBe(42));")
            write(".gitignore","node_modules/\n.nx/\ndist/\nreporter-observed.txt\n")
            env={**os.environ,"NX_DAEMON":"false","NX_NO_CLOUD":"true","NX_TASKS_RUNNER_DYNAMIC_OUTPUT":"false","NX_SKIP_NX_CACHE":"true","CI":"true"}
            argv,_=verifier._command_prefix(shutil.which("npm"),["install","--no-audit","--no-fund"])
            installed=subprocess.run(argv,cwd=root,env=env,capture_output=True,text=True,timeout=300)
            self.assertEqual(0,installed.returncode,installed.stdout+installed.stderr)
            for args in (("init","-q"),("config","user.name","Physical Fixture"),("config","user.email","fixture@example.invalid"),("add","."),("commit","-qm","physical source closure fixture")):
                subprocess.run(["git","-C",str(root),*args],check=True,capture_output=True)
            config=verifier.BaselineVerifyConfig(project_checks="strict",commands=("npm run build","npm run test"),timeout_seconds=180,attempt_timeout_seconds=600,proof_cache_dir=str(base/"proofs"))
            counts=[];measurements=[];real=verifier._run
            for attempt in range(2):
                stages=[];checks=[]
                def observe(argv,cwd,**kwargs):
                    label=kwargs.get("progress_label","")
                    if "install" in label:stages.append(label)
                    if label in config.commands:
                        execution=Path(cwd).resolve();self.assertEqual("app",execution.name)
                        source_root=execution.parents[1]
                        for name in ("shared/styles/variables.scss","frontend-tools/test/reporter.mjs","nx.json","packages/style/src/view.scss"):
                            self.assertTrue((source_root/name).is_file(),name)
                        checks.append(label)
                    return real(argv,cwd,**kwargs)
                started=time.monotonic()
                with patch.dict(os.environ,{"DEPLOOM_VERIFICATION_ROOT":str(base/"storage"),"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}),patch.object(verifier,"_run",observe):
                    result=verifier.verify_assignment(app,{},config=config,run_project_checks=True,runtime_env=env,progress=lambda message: print("PHYSICAL_CLOSURE_PROGRESS "+message,flush=True))
                measurements.append(time.monotonic()-started);counts.append(len(stages))
                self.assertTrue(result.ok,result.summary+"\n"+result.output)
                self.assertEqual("passed",result.kind);self.assertEqual(list(config.commands),checks)
            print("NODE_CLOSURE_INSTALL_STAGE_COUNTS "+json.dumps({"stages":counts,"seconds":measurements}),flush=True)
            self.assertEqual(2,counts[0],counts)
            # Exact warm reuse exists only under guaranteed process-tree supervision
            # (Windows). Best-effort POSIX supervision keeps fresh isolated installs
            # on every run while still executing the real toolchain checks.
            if os.name == 'nt':
                self.assertEqual(0,counts[1],counts)
            else:
                self.assertEqual(counts[0],counts[1],counts)
            self.assertFalse((root/"dist").exists(),"real source tree must not be built")
            print("NODE_CLOSURE_PHYSICAL_MEASUREMENTS "+json.dumps({"coldSeconds":measurements[0],"warmSeconds":measurements[1],"installStages":counts}))
            source.clear_source_snapshot_epochs()

if __name__=="__main__":unittest.main()

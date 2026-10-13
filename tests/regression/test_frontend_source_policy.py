"""Explicit frontend scope excludes owned backend outputs, preserving Node/Nx source."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import source_snapshot as source
from scoped_source_policy import policy_for_project, policy_payload, policy_from_payload
from verification_source_closure import require_complete_source_policy

class FrontendSourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.front = self.root / "front"
        self.front.mkdir(); (self.front / "package.json").write_text('{"name":"front"}')
        self.backend = self.root / "backend"; self.backend.mkdir()
        (self.backend / "server.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"/>')
        for name in ("bin", "obj"):
            (self.backend / name).mkdir(); (self.backend / name / "large.bin").write_bytes(b"x" * 1024 * 1024)
        (self.backend / "source.cs").write_text("class Source {}")
        (self.front / "bin").mkdir(); (self.front / "bin/cli.cjs").write_text("module.exports=42;")
        (self.root / "nx.json").write_text('{"targetDefaults":{"build":{"dependsOn":["^build"]}}}')
        (self.root / ".gitignore").write_text("backend/bin/\nbackend/obj/\n")
        for args in (("init","-q"),("config","user.name","Fixture"),("config","user.email","fixture@example.invalid"),("add","."),("commit","-qm","fixture")):
            subprocess.run(["git","-C",str(self.root),*args],check=True,capture_output=True)
        self.addCleanup(source.clear_source_snapshot_epochs)

    def test_frontend_capture_excludes_only_owned_outputs_and_roundtrips_policy(self):
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend","DEPLOOM_SOURCE_COPY_MAX_BYTES":"65536"}):
            snapshot=source.capture_source_snapshot(self.front)
        self.addCleanup(source._force_rmtree,snapshot.container)
        self.assertEqual(("backend/bin","backend/obj"),snapshot.input_policy.frontend_build_output_paths)
        self.assertEqual(snapshot.input_policy,policy_from_payload(policy_payload(snapshot.input_policy)))
        require_complete_source_policy(snapshot.input_policy)
        for name in ("backend/source.cs","backend/server.csproj","front/bin/cli.cjs","nx.json"):
            self.assertTrue((snapshot.root/name).is_file(),name)
        self.assertFalse((snapshot.root/"backend/bin").exists());self.assertFalse((snapshot.root/"backend/obj").exists())
        self.assertLess(snapshot.byte_count,65536)

    def test_completed_frontend_snapshot_cannot_be_reused_as_repository_authority(self):
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            with source.verification_source_snapshot_scope(self.front,reuse_completed=True) as first:
                self.assertFalse((first.root/"backend/bin").exists())
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"repository"}):
            with source.verification_source_snapshot_scope(self.front,reuse_completed=True) as second:
                self.assertIsNot(first,second)
                self.assertTrue((second.root/"backend/bin/large.bin").is_file())
                self.assertNotEqual(first.key,second.key)

    def test_agent_and_nx_state_are_excluded_but_dot_configuration_and_git_remain(self):
        for name in (".claude/worktrees/copy/source.js", ".codex/logs/session.json", ".nx/cache/output.js"):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text("generated tool state")
        configurations=(".env", ".npmrc", ".yarnrc.yml", ".storybook/main.cjs", ".github/workflows/check.yml")
        for name in configurations:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text("authoritative configuration")
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            snapshot=source.capture_source_snapshot(self.front)
        self.addCleanup(source._force_rmtree,snapshot.container)
        for name in (".claude", ".codex", ".nx"):
            self.assertFalse((snapshot.root/name).exists(),name)
        for name in configurations:
            self.assertTrue((snapshot.root/name).is_file(),name)
        self.assertTrue((snapshot.root/".git/HEAD").is_file(),"Git authority must not be weakened")
        self.assertEqual(snapshot.input_policy,policy_from_payload(policy_payload(snapshot.input_policy)))

    def test_future_backend_and_nx_outputs_do_not_destabilize_capture(self):
        import shutil
        for name in ("bin", "obj"):
            shutil.rmtree(self.backend/name)
        real=source.materialize_private_tree
        def materialize(*args,**kwargs):
            result=real(*args,**kwargs)
            for name in ("backend/bin/new.dll", ".nx/cache/new/output.js", ".claude/worktrees/new/file.js"):
                path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text("transient build output")
            return result
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}),patch.object(source,"materialize_private_tree",materialize):
            snapshot=source.capture_source_snapshot(self.front)
        self.addCleanup(source._force_rmtree,snapshot.container)
        self.assertFalse((snapshot.root/"backend/bin").exists())
        self.assertFalse((snapshot.root/".nx").exists())
        require_complete_source_policy(snapshot.input_policy)

    def test_local_package_in_agent_state_is_not_silently_omitted(self):
        (self.front/"package.json").write_text(json.dumps({"name":"front","dependencies":{"tool":"file:../.claude/skills/tool"}}))
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}),patch("source_snapshot.materialize_private_tree") as copy:
            with self.assertRaisesRegex(source.SourceCaptureError,"SOURCE_REQUIRED_INPUT_EXCLUDED"):
                source.capture_source_snapshot(self.front)
            copy.assert_not_called()

    def test_excluded_missing_input_diagnostic_requires_existing_original_and_bounded_path(self):
        from types import SimpleNamespace
        from verification_source_closure import excluded_source_failure_evidence
        tool=self.root/".claude/tool.cjs";tool.parent.mkdir();tool.write_text("source")
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            policy=policy_for_project(self.front,self.root)
        trial=self.root/"trial";cwd=trial/"front"
        snapshot=SimpleNamespace(input_policy=policy,capture_root=self.root)
        def evidence(output):
            return excluded_source_failure_evidence(snapshot,output,execution_root=trial,execution_cwd=cwd)
        self.assertEqual([".claude/tool.cjs"],evidence("Cannot find module '../.claude/tool.cjs'"))
        self.assertEqual([],evidence("Cannot find module '../.claude/absent.cjs'"))
        self.assertEqual([],evidence("Cannot find module '../../.claude/tool.cjs'"))
        self.assertEqual([],evidence("Cannot find module 'https://example.invalid/.claude/tool.cjs'"))
        self.assertEqual([],evidence("loaded '../.claude/tool.cjs'"))

    def test_unknown_dot_directory_is_source_without_declared_nx_configuration(self):
        (self.root/"nx.json").unlink()
        path=self.root/".nx/custom-tool.cjs";path.parent.mkdir();path.write_text("module.exports=42")
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            snapshot=source.capture_source_snapshot(self.front)
        self.addCleanup(source._force_rmtree,snapshot.container)
        self.assertTrue((snapshot.root/".nx/custom-tool.cjs").is_file())

    def test_repository_default_still_includes_backend_outputs(self):
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"repository"}):
            snapshot=source.capture_source_snapshot(self.front)
        self.addCleanup(source._force_rmtree,snapshot.container)
        self.assertTrue((snapshot.root/"backend/bin/large.bin").is_file())
        self.assertNotEqual(snapshot.policy_key,source.SourceInputPolicy(frontend_build_output_paths=("backend/bin","backend/obj")).key)

    def test_declared_local_dependency_in_backend_output_is_inconclusive_before_copy(self):
        (self.front/"package.json").write_text(json.dumps({"name":"front","dependencies":{"generated":"file:../backend/bin"}}))
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}),patch("source_snapshot.materialize_private_tree") as copy:
            with self.assertRaisesRegex(source.SourceCaptureError,"SOURCE_REQUIRED_INPUT_EXCLUDED"):
                source.capture_source_snapshot(self.front)
            copy.assert_not_called()

    def test_mixed_node_msbuild_owner_preserves_node_bin(self):
        (self.backend/"package.json").write_text('{"name":"node-with-msbuild"}')
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            policy=policy_for_project(self.front,self.root)
        self.assertEqual((),policy.frontend_build_output_paths)

    def test_removed_owner_changes_policy_and_source_authority(self):
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_SCOPE_MODE":"frontend"}):
            before=policy_for_project(self.front,self.root)
            (self.backend/"server.csproj").unlink()
            after=policy_for_project(self.front,self.root)
        self.assertNotEqual(before.key,after.key)
        self.assertEqual((),after.frontend_build_output_paths)

    def test_policy_escape_and_arbitrary_source_exclusions_are_rejected(self):
        for path in ("../backend/bin","backend/source.cs","C:/outside/bin"):
            with self.assertRaises(source.SourceCaptureError):
                policy_from_payload({"mode":"frontend-msbuild-outputs-v1","excludedBuildOutputPaths":[path]})

if __name__ == "__main__": unittest.main()

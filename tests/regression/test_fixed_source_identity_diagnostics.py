"""Sealed local inputs and exact drift evidence never trust live/noisy aliases."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import source_snapshot as source
import verification_proof as proof
from verification_identity_diagnostics import identity_component_diff, safe_identity_error

class FixedSourceIdentityDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="deploom-local-fixed-");self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"app";self.root.mkdir()
        self.lib=self.root/"lib";self.lib.mkdir()
        (self.root/"package.json").write_text(json.dumps({"name":"app","version":"1.0.0","dependencies":{"local-lib":"file:./lib"}}),encoding="utf-8")
        (self.lib/"package.json").write_text('{"name":"local-lib","version":"1.0.0","main":"dist/index.cjs"}',encoding="utf-8")
        (self.lib/"dist").mkdir();(self.lib/"dist/index.cjs").write_text("module.exports=1;",encoding="utf-8")
        self.addCleanup(source.clear_source_snapshot_epochs)

    def test_two_capture_locations_produce_same_fixed_local_identity(self):
        with source.verification_source_snapshot_scope(self.root) as first:
            identity=proof.fixed_resolver_input_identity(first.project_path,manager="npm")
        with source.verification_source_snapshot_scope(self.root) as second:
            self.assertNotEqual(first.root,second.root)
            self.assertEqual(identity,proof.fixed_resolver_input_identity(second.project_path,manager="npm"))

    def test_dist_payload_change_is_authoritative_and_diagnostic_is_specific(self):
        with source.verification_source_snapshot_scope(self.root) as first:
            identity=proof.fixed_resolver_input_identity(first.project_path,manager="npm")
        (self.lib/"dist/index.cjs").write_text("module.exports=2;",encoding="utf-8")
        with source.verification_source_snapshot_scope(self.root) as second:
            observed=proof.fixed_resolver_input_identity(second.project_path,manager="npm")
        self.assertNotEqual(identity["key"],observed["key"])
        evidence=identity_component_diff(identity,observed)
        self.assertTrue(any("local-lib" in row["component"] for row in evidence["changedComponents"]),evidence)

    def test_excluded_dependency_noise_and_live_edit_do_not_change_pinned_local_bytes(self):
        with source.verification_source_snapshot_scope(self.root) as snapshot:
            expected=proof.fixed_resolver_input_identity(snapshot.project_path,manager="npm")
            (self.lib/"dist/index.cjs").write_text("module.exports=99;",encoding="utf-8")
            noise=snapshot.root/".dependency-roadmap";noise.mkdir(exist_ok=True);(noise/"noise").write_text("noise")
            self.assertEqual(expected,proof.fixed_resolver_input_identity(snapshot.project_path,manager="npm"))

    def test_sealed_mutation_between_fingerprint_reads_is_rejected(self):
        with source.verification_source_snapshot_scope(self.root) as snapshot:
            real=proof._fixed_resolver_input_payload
            def mutate(*args,**kwargs):
                payload=real(*args,**kwargs)
                victim=snapshot.root/"lib/dist/index.cjs"
                victim.chmod(victim.stat().st_mode|stat.S_IWRITE);victim.write_text("module.exports=3;")
                return payload
            with patch.object(proof,"_fixed_resolver_input_payload",mutate):
                with self.assertRaisesRegex(proof.SourceIdentityUnavailable,"SOURCE_SNAPSHOT_CONTENT_MISMATCH"):
                    proof.fixed_resolver_input_identity(snapshot.project_path,manager="npm")

    def test_unrelocated_absolute_local_path_is_not_verified_against_different_bytes(self):
        with source.verification_source_snapshot_scope(self.root) as snapshot:
            with self.assertRaisesRegex(proof.SourceIdentityUnavailable,"OUTSIDE_SOURCE_CLOSURE"):
                proof._external_fixed_target_identity(snapshot.project_path,"file:"+str(self.lib))

    def test_diagnostics_do_not_expose_url_credentials_or_query(self):
        text=safe_identity_error("request " + "https://" + "user:secret@registry.example.invalid/private/pkg?token=secret failed")
        self.assertNotIn("secret",text);self.assertNotIn("private",text)
        self.assertIn("registry.example.invalid",text)

if __name__=="__main__":unittest.main()

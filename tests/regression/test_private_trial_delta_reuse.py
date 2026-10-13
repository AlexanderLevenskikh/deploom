"""Unexecuted follow-up regressions: delta reuse restores the exact private source."""
import os
import tempfile
import unittest
from pathlib import Path
from artifact_integrity import build_artifact_tree_integrity
from verification_source_closure import reset_observed_trial_outputs

class PrivateTrialDeltaReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/main.js').write_text('export const n = 1;')
        seal = build_artifact_tree_integrity(self.root, source_only=True)
        self.key = seal.source_content_key
        self.expected = {os.path.normcase(str(e['path'])).replace('\\', '/'):e for e in seal.source_entries}

    def outputs(self):
        (self.root / 'dist').mkdir()
        (self.root / 'dist/result.js').write_text('generated')
        return ('1:dist', '1:dist/result.js', '3:dist/result.js')

    def test_new_outputs_removed_and_exact_source_seal_restored(self):
        self.assertTrue(reset_observed_trial_outputs(self.root, self.expected, self.outputs()))
        self.assertFalse((self.root / 'dist').exists())
        self.assertEqual(self.key, build_artifact_tree_integrity(self.root, source_only=True).source_content_key)

    def test_source_mutation_refuses_reuse_before_any_output_cleanup(self):
        changes = self.outputs()
        (self.root / 'src/main.js').write_text('export const n = 2;')
        self.assertFalse(reset_observed_trial_outputs(self.root, self.expected, (*changes,'3:src/main.js')))
        self.assertTrue((self.root / 'dist/result.js').is_file())

    def test_notification_with_identical_source_bytes_does_not_force_full_hash(self):
        (self.root / 'src/main.js').write_text('export const n = 1;')
        self.assertTrue(reset_observed_trial_outputs(self.root, self.expected, ('3:src/main.js',)))

    def test_dependency_changes_never_become_allowed_outputs(self):
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/package.js').write_text('dependency')
        self.assertFalse(reset_observed_trial_outputs(self.root, self.expected, ('1:node_modules/package.js',)))
        self.assertTrue((self.root / 'node_modules/package.js').exists())

    def test_existing_source_directory_replacement_refuses_reuse(self):
        self.assertFalse(reset_observed_trial_outputs(self.root, self.expected, ('4:src','5:src')))

    def test_traversal_and_windows_aliases_refuse_reuse(self):
        for change in ('1:../outside','1:src/main.js.','1:C:/outside','1:src/main.js:stream'):
            self.assertFalse(reset_observed_trial_outputs(self.root, self.expected, (change,)), change)

if __name__ == '__main__': unittest.main()

import os
import unittest
from unittest.mock import patch

from tests.acceptance.test_prepared_artifact_continuity_physical import PreparedArtifactContinuityPhysical
import block_v_prepared_artifact as store
from artifact_integrity import build_artifact_tree_integrity


@unittest.skipUnless(os.name == "nt", "Windows real watcher regression")
class RepeatedArtifactHit(PreparedArtifactContinuityPhysical):
    def test_four_same_identity_hits_do_not_rehash_own_drain_markers(self):
        self._publish()
        # A watcher may conservatively require one cold validation (e.g. delayed
        # directory metadata notifications). Once validated, repeated drains
        # must not manufacture further invalidations.
        self.assertIsNotNone(self._load())
        with patch.object(store, "build_artifact_tree_integrity", wraps=build_artifact_tree_integrity) as hashing:
            for _ in range(4):
                self.assertIsNotNone(self._load())
            self.assertEqual(0, hashing.call_count)

    def test_recreated_owned_sentinel_cannot_hide_a_new_file(self):
        self._publish()
        self.assertIsNotNone(self._load())
        self.assertIsNotNone(self._load())
        tokens = set().union(*store._VALIDATION_DRAIN_TOKENS.values())
        self.assertTrue(tokens)
        (self.workspace / sorted(tokens)[0]).write_text("foreign persistent file", encoding="utf-8")
        self.assertIsNone(self._load())

"""A3/B5: materialize-failure classification must never fabricate availability.

A bare ``e404`` / ``404 Not Found`` install failure used to be classified as a
resolver ``ETARGET``, and the resolver branch deferred "the exact target"
WHEN ``record_unavailable_targets`` had actually recorded NOTHING — the ledger
entry that was supposed to stop plan-next was never written, so plan-next
re-proposed the SAME fingerprint and the loop replayed forever (B5).

After the fix:

* a generic 404 is an honest ``unknown NOT_FOUND`` and a missing tarball
  (``.tgz``/``.tar.gz``) is ``unknown TARBALL_UNAVAILABLE`` — never resolver;
* a resolver ETARGET is deferred ONLY when the output proves the per-target
  fingerprint (``No matching version found for <name>@<version>`` of a
  changed/target package); an evidence-less ETARGET is reclassified to
  ``unknown RESOLVER_NO_EVIDENCE`` and routed through the terminal
  scope-block path, so plan-next STOPS with evidence instead of looping;
* nothing here becomes a compatibility constraint, and the verified checkpoint
  is never touched.
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from iterative_migration import (
    SCHEMA_VERSION,
    _materialization_failure,
    _materialize_locked,
    _plan_next_locked,
    load_candidate,
    load_ledger,
    load_run,
    save_checkpoint,
    save_config,
    save_run,
)

try:  # unittest discover puts tests/regression on sys.path
    from test_iterative_materialize_retry_budget import (  # noqa: F401
        _checkpoint_dict,
        _config_dict,
        _run_dict,
    )
except ImportError:  # run as the tests.regression package
    from tests.regression.test_iterative_materialize_retry_budget import (  # noqa: F401
        _checkpoint_dict,
        _config_dict,
        _run_dict,
    )


class _Base(unittest.TestCase):
    def make_run_dir(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="iter-materialize-classify-"))
        (root / "trial").mkdir(parents=True, exist_ok=True)
        (root / "checkpoints").mkdir(parents=True, exist_ok=True)
        save_run(root, _run_dict())
        save_config(root, _config_dict())
        save_checkpoint(root, _checkpoint_dict())
        return root

    def install_failure(self, stdout: str, returncode: int = 1) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    @staticmethod
    def materialize_trial_tree_ok(snapshot, ws_root, timeout_seconds):
        ws_root = Path(ws_root)
        ws_root.mkdir(parents=True, exist_ok=True)
        (ws_root / "package.json").write_text('{"name": "trial", "private": true}', encoding="utf-8")

    @contextmanager
    def materialize_patches(self, install):
        patches = [
            mock.patch("iterative_migration.run_budget_ok", return_value=True),
            mock.patch("iterative_migration._assert_runtime_unchanged"),
            mock.patch("iterative_migration.open_source_snapshot", return_value=SimpleNamespace(root=str(self.tmp))),
            mock.patch("iterative_migration._materialize_trial_tree", side_effect=self.materialize_trial_tree_ok),
            mock.patch("iterative_migration._apply_assignment", return_value={}),
            mock.patch("iterative_migration._run_install", return_value=install),
            mock.patch("iterative_migration._runtime_env", return_value={}),
            mock.patch("iterative_migration._emit_status"),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            yield

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="iter-classify-patches-"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


class MaterializeFailureClassificationUnit(_Base):
    def test_generic_404_is_unknown_not_found_never_resolver(self) -> None:
        output = (
            "npm error code E404\n"
            "npm error 404 Not Found - GET https://registry.npmjs.org/pkg - Not found\n"
        )
        self.assertEqual(_materialization_failure(output), ("unknown", "NOT_FOUND"))
        self.assertNotEqual(_materialization_failure(output)[0], "resolver")

    def test_tarball_404_is_unknown_tarball_unavailable(self) -> None:
        output = (
            "npm error code E404\n"
            "npm error 404 Not Found - GET https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz - Not found\n"
        )
        self.assertEqual(_materialization_failure(output), ("unknown", "TARBALL_UNAVAILABLE"))

    def test_genuine_etarget_is_still_resolver(self) -> None:
        output = (
            "npm ERR! code ETARGET\n"
            "npm ERR! notarget No matching version found for pkg-a@99.99.99.\n"
        )
        self.assertEqual(_materialization_failure(output), ("resolver", "ETARGET"))

    def test_infra_causes_still_win_over_404_keywords(self) -> None:
        output = "npm ERR! code ECONNRESET\nnpm ERR! errno -104 Connection reset by peer\nnpm ERR! network request to registry failed\n"
        self.assertEqual(_materialization_failure(output)[0], "infrastructure")


class MaterializeFailureClassificationEndToEnd(_Base):
    def test_eresolve_conflict_defers_exact_candidate_without_false_availability_or_infra_block(self):
        self.assertEqual(("resolver", "CONFLICT"), _materialization_failure("npm error ERESOLVE unable to resolve dependency tree"))
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)
        candidate = load_candidate(run_dir)
        checkpoint_before = (run_dir / 'checkpoints' / 'C0.json').read_bytes()
        events = []
        with self.materialize_patches(self.install_failure("npm error ERESOLVE could not resolve\nConflicting peer dependency: companion@3.0.1")):
            with mock.patch('iterative_migration._emit_status', side_effect=events.append):
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        rejected = load_candidate(run_dir)
        self.assertEqual('REJECTED', rejected['stage'])
        self.assertEqual('resolver-conflict', rejected['conclusion']['kind'])
        self.assertFalse(rejected['conclusion']['exactTargetDeferred'])
        ledger = load_ledger(run_dir)
        self.assertEqual([], ledger.get('targetAvailabilityDeferrals') or [])
        self.assertEqual([], ledger.get('infraBlocks') or [])
        self.assertTrue(ledger['deferrals'][0]['assignmentFingerprint'])
        self.assertEqual('READY', load_run(run_dir)['phase'])
        self.assertIsNone(load_run(run_dir).get('activeCandidateId'))
        self.assertEqual(checkpoint_before, (run_dir / 'checkpoints' / 'C0.json').read_bytes())
        with mock.patch('iterative_migration._emit_status'):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        next_candidate = load_candidate(run_dir)
        if next_candidate and next_candidate['stage'] == 'PLANNED':
            self.assertNotEqual(candidate['fullAssignment'], next_candidate['fullAssignment'])

    def _drive_then_assert_blocked(self, install, expected_code: str) -> None:
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)
        self.assertIsNotNone(load_candidate(run_dir))

        events: list[dict] = []
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append):
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))

        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertTrue(fu, "a materialize.failed event must be emitted")
        self.assertEqual("unknown", fu[-1]["kind"])
        self.assertTrue(fu[-1]["terminalFailure"])
        self.assertFalse(fu[-1].get("retryable", False))
        self.assertNotIn("exactTargetDeferred", fu[-1], "no target facts -> nothing deferred")

        ledger = load_ledger(run_dir)
        self.assertEqual([], ledger.get("targetAvailabilityDeferrals") or [],
                         "no per-target availability deferral may be fabricated")
        self.assertEqual([], ledger.get("deferrals") or [], "no candidate deferral may be fabricated")
        self.assertTrue(ledger.get("infraBlocks"), "an identity-scoped evidence block must be written")

        candidate = load_candidate(run_dir)
        self.assertEqual("REJECTED", candidate["stage"])
        run = load_run(run_dir)
        self.assertIsNotNone(run.get("infraBlocked"), "the no-evidence gate must stop the coordinator")
        self.assertEqual("unknown", run["infraBlocked"]["classification"])
        self.assertEqual(expected_code, run["infraBlocked"]["code"])
        self.assertTrue(run["infraBlocked"].get("assignmentFingerprint"))

        # The loop breaker: an identical plan-next re-proposal must NOT happen —
        # the identity is blocked for the current scope, so plan-next stops with
        # evidence instead of proposing the same candidate again.
        events2: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events2.append):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        self.assertIn("plan-next.infra-blocked", [e.get("event") for e in events2])
        self.assertIsNone(load_candidate(run_dir), "plan-next must not re-propose the blocked fingerprint")

    def test_tarball_404_blocks_with_evidence_no_loop(self) -> None:
        self._drive_then_assert_blocked(
            self.install_failure(
                "npm error code E404\n"
                "npm error 404 Not Found - GET https://registry.npmjs.org/pkg-a/-/pkg-a-2.0.0.tgz - Not found\n"
            ),
            expected_code="TARBALL_UNAVAILABLE",
        )

    def test_generic_404_blocks_with_evidence_no_loop(self) -> None:
        self._drive_then_assert_blocked(
            self.install_failure(
                "npm error code E404\n"
                "npm error 404 Not Found - GET https://registry.npmjs.org/pkg-a - Not found\n"
            ),
            expected_code="NOT_FOUND",
        )

    def test_etarget_without_fingerprint_evidence_reclassifies_and_blocks_no_loop(self) -> None:
        # A genuine ETARGET marker whose output does NOT prove a changed/target
        # version (here: an unrelated package) has no extractable per-target
        # evidence. The old code fabricated the deferral and looped; the fixed
        # branch reclassifies to RESOLVER_NO_EVIDENCE and blocks the identity.
        install = self.install_failure(
            "npm ERR! code ETARGET\n"
            "npm ERR! notarget No matching version found for other-pkg@9.0.0.\n"
        )
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)

        events: list[dict] = []
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append):
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))

        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertEqual("unknown", fu[-1]["kind"])
        self.assertEqual("RESOLVER_NO_EVIDENCE", fu[-1]["code"])
        self.assertNotIn("exactTargetDeferred", fu[-1])
        ledger = load_ledger(run_dir)
        self.assertEqual([], ledger.get("targetAvailabilityDeferrals") or [])
        self.assertEqual([], ledger.get("deferrals") or [])
        run = load_run(run_dir)
        self.assertEqual("RESOLVER_NO_EVIDENCE", run["infraBlocked"]["code"])
        self.assertEqual("unknown", run["infraBlocked"]["classification"])

        events2: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events2.append):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        self.assertIn("plan-next.infra-blocked", [e.get("event") for e in events2])
        self.assertIsNone(load_candidate(run_dir))


if __name__ == "__main__":
    unittest.main()

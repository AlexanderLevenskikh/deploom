"""Part A of the libjs deep rescue (2026-09-27): behavioral tests of the
PRODUCTION selector/orchestrator transitions.

Only Z3 `unknown` / network / registry are mocked. The peer graph
(`_potential_peer_graph`), component splitting (`_graph_components`), candidate
domains, per-component status recording and the checkpoint/identity store are
all production code.

Scenario coverage (matches the audit + the task):
  T1  -- a big unknown FIRST component must not abort the whole round; the
         independent small SAT candidate must still be checked (barrier repro).
  T2  -- a fully pinned component (residual target = queued/prepared fallback)
         must be usable WITHOUT a fresh global solve.
  T3  -- first safe result found, next cohort unknown -> result retained and
         a further independent cohort still considered.
  T4  -- interrupted run resumes the correct checkpoint; a changed
         source/lock/policy invalidates the old authority.

T1 and T2 are the repeatable FAILING tests for the libjs barrier today; they
are the specification Part C turns green. T3 documents the production
state-transition contract in the existing tolerant mode.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import block_v_recovery
import dependency_live_roadmap_generator as roadmap  # noqa: E402
from peer_solver_model import ExactSolveResult  # noqa: E402

REGISTRY = "https://nexus.example/repository/npm-group"


def _row(name: str, current: str = "1.0.0", desired: str = "2.0.0") -> roadmap.DependencyRow:
    return roadmap.DependencyRow(
        project="Demo",
        package_dir=".",
        name=name,
        kind="dev",
        requested_spec="*",
        current_version=current,
        current_source="lockfile",
        latest_version=desired,
        current_vulns="0",
        min_no_critical=current,
        min_no_high=current,
        min_no_vuln=current,
        min_lag_12m=current,
        min_lag_9m=current,
        min_lag_6m=current,
        min_lag_3m=current,
        group=1,
        reason="deep rescue part A",
        notes="",
        target_default=desired,
        target_yellow=desired,
        target_green=desired,
        target_default_reason="desired",
        target_yellow_reason="desired",
        target_green_reason="desired",
    )


def _client(names, *, peer_edges=()):
    """LiveDataClient with offline registry metadata for `names`.

    `peer_edges` is an iterable of (name, peer, spec, version) tuples; the peer
    dependency is attached to that version's metadata so the POTENTIAL peer
    graph links the two packages.
    """
    client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
    edges = {(edge[0], edge[3]): (edge[1], edge[2]) for edge in peer_edges}
    for name in names:
        versions = {}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            meta = {
                "name": name,
                "version": version,
                "dist": {"tarball": f"{REGISTRY}/artifact/{name}/{version}.tgz"},
            }
            if (name, version) in edges:
                peer_name, spec = edges[(name, version)]
                meta["peerDependencies"] = {peer_name: f"^{spec}"}
                meta.setdefault("peerDependenciesMeta", {})[peer_name] = {}
            versions[version] = meta
        client.npm_cache[name] = {"versions": versions}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            client.registry_artifact_cache[(name, version)] = {
                "status": "available",
                "tarballUrl": f"{REGISTRY}/artifact/{name}/{version}.tgz",
            }
    return client


def _pkg_names(model):
    return sorted(getattr(p, "name", str(p)) for p in model.packages)


def _optimal(model, version_by_name):
    assignment = {name: version_by_name[name] for name in _pkg_names(model)}
    return ExactSolveResult(
        backend="z3", status="optimal", assignment=assignment,
        score=model.assignment_score(assignment), elapsed_ms=12,
    )


class T1BigUnknownMustNotAbortSmallCandidate(unittest.TestCase):
    """The libjs barrier: a large peer component comes FIRST and is `unknown`;
    the bootstrap-style call must still check the independent small candidate."""

    def test_barrier_bootstrap_big_unknown_blocks_small_candidate(self):
        client = _client(
            ("big-a", "big-b", "small-c"),
            peer_edges=(("big-a", "big-b", "2.0.0", "2.0.0"),
                        ("big-b", "big-a", "2.0.0", "2.0.0")),
        )
        by_project = {"Demo": [_row("big-a"), _row("big-b"), _row("small-c")]}
        roadmap.capture_desired_targets(by_project)
        solved = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            pkg_names = _pkg_names(model)
            solved.append(pkg_names)
            if "big-a" in pkg_names:
                return ExactSolveResult(
                    backend="z3", status="unknown", detail="no reason given", elapsed_ms=33990,
                )
            return _optimal(model, {"small-c": "1.5.0"})

        statuses: dict = {}
        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            result = roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                solver_statuses_out=statuses,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
            )

        assignment = result["Demo"]["default"]
        # THE barrier assertion: the small independent candidate must be solved
        # and delivered even though the big first component is unknown.
        self.assertEqual({"small-c": "1.5.0"}, assignment)
        self.assertNotIn("big-a", assignment)
        self.assertNotIn("big-b", assignment)
        self.assertIn(["small-c"], solved)
        # the big component is recorded as unknown, never as decided
        self.assertEqual("optimal", statuses["Demo"]["default"].get("small-c"))


class T2QueuedFallbackWithoutNewSolve(unittest.TestCase):
    """A fully pinned component (residual target = the queued fallback) must be
    usable WITHOUT a fresh global solve; solver unavailability must not matter."""

    def test_pinned_residual_component_does_not_require_new_solve(self):
        client = _client(
            ("big-a", "big-b", "small-c"),
            peer_edges=(("big-a", "big-b", "2.0.0", "2.0.0"),
                        ("big-b", "big-a", "2.0.0", "2.0.0")),
        )
        by_project = {"Demo": [_row("big-a"), _row("big-b"), _row("small-c")]}
        roadmap.capture_desired_targets(by_project)
        residual = {"big-a": "1.0.0", "big-b": "1.0.0"}
        solved = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            pkg_names = _pkg_names(model)
            solved.append(pkg_names)
            if "big-a" in pkg_names:
                raise AssertionError(
                    "T2: a fully pinned component must not need a fresh global solve; "
                    "the queued/prepared fallback is checked without it"
                )
            return _optimal(model, {"small-c": "1.5.0"})

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            result = roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                residual_targets_by_project={"Demo": residual},
            )

        assignment = result["Demo"]["default"]
        self.assertEqual({"small-c": "1.5.0", **residual}, assignment)
        self.assertEqual([["small-c"]], solved)  # solve ran for the small one only


class T3FirstSafeResultKeptNextCohortConsidered(unittest.TestCase):
    """A optimal -> B unknown -> C optimal: the retained result keeps A and C
    and records B as unresolved; the next independent cohort is still solved."""

    def test_retained_result_and_next_independent_cohort(self):
        client = _client(("alpha", "beta", "gamma"))
        by_project = {"Demo": [_row("alpha"), _row("beta"), _row("gamma")]}
        roadmap.capture_desired_targets(by_project)
        solved = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            pkg_names = _pkg_names(model)
            solved.append(pkg_names)
            if "beta" in pkg_names:
                return ExactSolveResult(
                    backend="z3", status="unknown", detail="no reason given", elapsed_ms=90,
                )
            return _optimal(model, {name: "1.5.0" for name in pkg_names})

        statuses: dict = {}
        incomplete: list = []
        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            result = roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                solver_statuses_out=statuses,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                partial_on_incomplete=True,
                incomplete_components_out=incomplete,
            )

        assignment = result["Demo"]["default"]
        # result kept: the first safe result AND the following independent cohort
        self.assertEqual({"alpha": "1.5.0", "gamma": "1.5.0"}, assignment)
        # unknown cohort recorded separately, not silently dropped
        beta_entries = [e for e in incomplete if "beta" in e["component"]]
        self.assertEqual(1, len(beta_entries))
        self.assertEqual("unknown", beta_entries[0]["status"])
        self.assertEqual(
            {"alpha": "optimal", "gamma": "optimal"},
            {k: v for k, v in statuses["Demo"]["default"].items() if v == "optimal"},
        )
        self.assertEqual([["alpha"], ["beta"], ["gamma"]], solved)


class T4InterruptedRunResumeAndIdentityInvalidation(unittest.TestCase):
    """Resume restores the correct checkpoint; changed source/lock/policy
    invalidates the old authority (identity mismatch)."""

    def _identity(self, project, mode, source_key, lock_key, policy):
        return block_v_recovery.baseline_run_identity(
            project=project, mode=mode,
            source_snapshot_key=source_key,
            resolver_context_key=lock_key,
            config=policy,
        )

    def test_resume_restores_checkpoint_and_changed_inputs_invalidate(self):
        base_policy = {"targetLevel": "yellow", "minLagOkPct": 80, "lagPolicyMonths": 12}
        with tempfile.TemporaryDirectory() as td:
            progress = Path(td) / "progress.json"
            store = block_v_recovery.BaselineRunRecoveryStore(progress)
            project, mode = "Demo", "yellow"
            lock_key = hashlib.sha256(b"yarn.lock-v1").hexdigest()
            identity = self._identity(project, mode, "d4b8b6d85ad8", lock_key, base_policy)

            first = store.begin(project, mode, identity=identity, policy="auto")
            self.assertFalse(first.found)

            state = block_v_recovery.build_run_state(
                iteration=3,
                learned_constraints=[{"big-a": "1.0.0"}],
                global_exact_exclusions=[],
                confirmed_failed_assignments=[],
            )
            store.checkpoint(project, mode, identity=identity, state=state, status="running")
            store._release_locked(project, mode)

            # same inputs -> the interrupted run resumes exactly where it stopped
            resumed = store.begin(project, mode, identity=identity, policy="auto")
            self.assertTrue(resumed.found)
            self.assertTrue(resumed.resumable)
            self.assertEqual(3, resumed.state.get("iteration"))
            self.assertEqual([{"big-a": "1.0.0"}], resumed.state.get("learnedConstraints"))

            # changed SOURCE commit invalidates the old authority
            plan = store.inspect(
                project, mode,
                identity=self._identity(project, mode, "other-commit", lock_key, base_policy),
            )
            self.assertEqual("identity-mismatch", plan.reason)
            self.assertFalse(plan.resumable)

            # changed LOCKFILE invalidates the old authority
            plan = store.inspect(
                project, mode,
                identity=self._identity(project, mode, "d4b8b6d85ad8",
                                        hashlib.sha256(b"yarn.lock-v2").hexdigest(), base_policy),
            )
            self.assertEqual("identity-mismatch", plan.reason)
            self.assertFalse(plan.resumable)

            # changed POLICY invalidates the old authority
            changed_policy = dict(base_policy, minLagOkPct=90)
            plan = store.inspect(
                project, mode,
                identity=self._identity(project, mode, "d4b8b6d85ad8", lock_key, changed_policy),
            )
            self.assertEqual("identity-mismatch", plan.reason)
            self.assertFalse(plan.resumable)


if __name__ == "__main__":
    unittest.main()

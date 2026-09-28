"""Part D of the tsapp deep rescue (2026-09-27): real small migration cohorts.

Acceptance covered here:
  D1 - a LARGE potential peer component (the search index over all versions)
       splits into small version-specific atomic cohorts when the
       CURRENT -> DESIRED delta does not force the intermediate members to move
       together.
  D2 - necessary atomicity is never silently split: a TARGET version whose
       peer requirement is NOT satisfied by the peer's CURRENT version keeps
       the pair in ONE cohort; the same holds for a peer's CURRENT requirement
       against the moving package's TARGET. A compatible delta moves on its own.
  D3 - resolver-override / fixed inputs never silently move: `fixed_names` are
       excluded from the moving set while the rest of the delta still plans.
"""
from __future__ import annotations

import unittest

import dependency_live_roadmap_generator as roadmap  # noqa: E402

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
        reason="deep rescue part D",
        notes="",
        target_default=desired,
        target_yellow=desired,
        target_green=desired,
        target_default_reason="desired",
        target_yellow_reason="desired",
        target_green_reason="desired",
    )


def _client(names, *, peer_edges=()):
    """Offline metadata client; peer_edges = (name, peer, spec, version[, optional]):
    the peer dependency ``^spec`` is attached to `version`'s metadata only; the
    5th element (True) marks it optional in peerDependenciesMeta."""
    client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
    edges = {}
    for edge in peer_edges:
        edges[(edge[0], edge[3])] = (edge[1], edge[2], edge[4] if len(edge) > 4 else False)
    for name in names:
        versions = {}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            meta = {
                "name": name,
                "version": version,
                "dist": {"tarball": f"{REGISTRY}/artifact/{name}/{version}.tgz"},
            }
            if (name, version) in edges:
                peer_name, spec, optional = edges[(name, version)]
                meta["peerDependencies"] = {peer_name: f"^{spec}"}
                meta.setdefault("peerDependenciesMeta", {})[peer_name] = (
                    {"optional": True} if optional else {}
                )
            versions[version] = meta
        client.npm_cache[name] = {"versions": versions}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            client.registry_artifact_cache[(name, version)] = {
                "status": "available",
                "tarballUrl": f"{REGISTRY}/artifact/{name}/{version}.tgz",
            }
    return client


def _components(graph):
    return roadmap._graph_components(graph)


class D1LargePotentialComponentSmallDeltaClosure(unittest.TestCase):
    def test_potential_component_splits_into_small_delta_cohorts(self):
        # a/b are peer-connected at 2.0.0 (their TARGET must move together);
        # c/d/e are peer-connected at 1.5.0 ONLY. Current = 1.0.0 for all,
        # desired = 2.0.0. The POTENTIAL graph (search index over all versions)
        # is ONE big component; the delta closure must split the 1.5.0-only
        # members away into their own singleton cohorts.
        client = _client(
            ("a", "b", "c", "d", "e"),
            peer_edges=(
                ("a", "b", "2.0.0", "2.0.0"),
                ("b", "a", "2.0.0", "2.0.0"),
                ("b", "c", "1.0.0", "1.5.0"),
                ("c", "b", "1.0.0", "1.5.0"),
                ("c", "d", "1.0.0", "1.5.0"),
                ("d", "c", "1.0.0", "1.5.0"),
                ("d", "e", "1.0.0", "1.5.0"),
                ("e", "d", "1.0.0", "1.5.0"),
                ("e", "c", "1.0.0", "1.5.0"),
            ),
        )
        names = ["a", "b", "c", "d", "e"]
        rows_by_name = {name: _row(name) for name in names}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {
            name: [r.current_version, "1.5.0", "2.0.0"] for name, r in rows_by_name.items()
        }
        current = {name: "1.0.0" for name in names}
        desired = {name: "2.0.0" for name in names}

        potential = roadmap._potential_peer_graph(rows_by_name, domains, client)
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        potential_components = _components(potential)
        delta_components = _components(delta)

        # the search index stays one connected component...
        self.assertEqual(1, len(potential_components))
        # ...while the migration closure splits it into small cohorts
        delta_by_size = {frozenset(c) for c in delta_components}
        self.assertIn(frozenset({"a", "b"}), delta_by_size)
        for name in ("c", "d", "e"):
            self.assertIn(frozenset({name}), delta_by_size)


class D2NecessaryAtomicityNeverSplit(unittest.TestCase):
    def test_target_peer_not_satisfied_by_peer_current_keeps_pair_together(self):
        # a@2.0.0 requires b ^2.0.0; b stays at current 1.0.0. Moving a alone
        # would break a's own peer requirement -> ONE cohort.
        client = _client(
            ("a", "b"),
            peer_edges=(("a", "b", "2.0.0", "2.0.0"),),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(1, len(components))
        self.assertEqual(["a", "b"], components[0])

    def test_peer_current_requirement_against_peer_target_keeps_pair_together(self):
        # b@1.0.0 (current) requires a ^1.0.0; a moves to 2.0.0. Unless b is
        # moved together, b's standing requirement breaks -> ONE cohort.
        client = _client(
            ("a", "b"),
            peer_edges=(("b", "a", "1.0.0", "1.0.0"),),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(1, len(components))
        self.assertEqual(["a", "b"], components[0])

    def test_compatible_delta_moves_independently(self):
        # a@2.0.0 peer on b is satisfied by b's CURRENT (^1.0.0 vs 1.0.0) ->
        # no edge -> both are their own singleton cohorts.
        client = _client(
            ("a", "b"),
            peer_edges=(("a", "b", "1.0.0", "2.0.0"),),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(["a"], components[0])
        self.assertEqual(["b"], components[1])


class D3FixedNamesNeverMove(unittest.TestCase):
    def test_fixed_names_excluded_from_moving_set(self):
        client = _client(("a", "fixed"))
        rows_by_name = {"a": _row("a"), "fixed": _row("fixed", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "fixed": ["1.0.0"]}
        current = {"a": "1.0.0", "fixed": "1.0.0"}
        desired = {"a": "2.0.0", "fixed": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
            fixed_names={"fixed"},
        )
        components = _components(delta)
        self.assertEqual(["a"], components[0])
        self.assertEqual(["fixed"], components[1])


class D4PresentOptionalPeersBindTheMovingCohort(unittest.TestCase):
    """R5: present optional peers lose atomicity when the closure skips them
    entirely. The production resolver materializes OPTIONAL peers into its hard
    model too, so the TRANSITIONAL state conflicts even when the eventual joint
    target pair is compatible: a@2 requiring optional b@^2 while b is still at
    current 1.0.0 fails with PEER_CONFLICT, and so does the reverse. Atomicity
    follows the transitional state, so any present peer whose combination with
    the mover's TARGET is unsatisfiable (while the peer stays at current) binds
    the pair -- independently of its optional flag. An absent optional peer
    still never coerces."""

    def test_transitional_optional_conflict_binds_even_when_joint_target_ok(self):
        # The auditor's counterexample: a@1,b@1; target a@2,b@2; a@2 requires
        # OPTIONAL b@^2 and b@2 requires OPTIONAL a@^2. The joint pair
        # (a@2, b@2) is compatible, yet neither single upgrade is: a@2+b@1 and
        # a@1+b@2 both conflict. The closure must bind {a, b} into ONE cohort so
        # the planner offers the joint move instead of two failed singletons.
        client = _client(
            ("a", "b"),
            peer_edges=(
                ("a", "b", "2.0.0", "2.0.0", True),
                ("b", "a", "2.0.0", "2.0.0", True),
            ),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0", "2.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "2.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(1, len(components))
        self.assertEqual({"a", "b"}, set(components[0]))

    def test_both_moving_optional_targets_unsatisfiable_are_one_cohort(self):
        client = _client(
            ("a", "b"),
            peer_edges=(
                ("a", "b", "3.0.0", "2.0.0", True),
                ("b", "a", "3.0.0", "2.0.0", True),
            ),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0", "2.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "2.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(1, len(components))
        self.assertEqual({"a", "b"}, set(components[0]))

    def test_present_static_optional_peer_with_conflicting_target_binds_pair(self):
        # a@2 declares an OPTIONAL peer on b@^3; b is present but static at
        # current 1.0.0 (not in the desired delta). Moving a alone leaves the
        # transitional a@2+b@1 combination in conflict with a's own optional
        # requirement, so the pair is one cohort.
        client = _client(
            ("a", "b"),
            peer_edges=(("a", "b", "3.0.0", "2.0.0", True),),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(1, len(components))
        self.assertEqual({"a", "b"}, set(components[0]))

    def test_present_optional_peer_satisfied_at_current_never_binds(self):
        # a@2's OPTIONAL peer b@^1.0.0 is satisfied by b's current 1.0.0: the
        # transitional state does NOT conflict, so a moves independently.
        client = _client(
            ("a", "b"),
            peer_edges=(("a", "b", "1.0.0", "2.0.0", True),),
        )
        rows_by_name = {"a": _row("a"), "b": _row("b", desired="1.0.0")}
        roadmap.capture_desired_targets({"Demo": list(rows_by_name.values())})
        domains = {"a": ["1.0.0", "2.0.0"], "b": ["1.0.0"]}
        current = {"a": "1.0.0", "b": "1.0.0"}
        desired = {"a": "2.0.0", "b": "1.0.0"}
        delta = roadmap._progressive_delta_atomic_closure(
            rows_by_name, domains, client, current, desired,
        )
        components = _components(delta)
        self.assertEqual(["a"], components[0])
        self.assertEqual(["b"], components[1])


if __name__ == "__main__":
    unittest.main()

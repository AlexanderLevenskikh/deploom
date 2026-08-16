from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from constraint_verify import (
    GlobalExactExclusionError,
    LocalizationTimeoutError,
    RankedComponentAlternative,
    VerificationUnit,
    assignment_matches_nogood,
    coordinate_global_exact_exclusions,
    merge_nogood_edges,
    parallel_ddmin,
)
import constraint_cache
import dependency_live_roadmap_generator as roadmap
from baseline_constraint_verifier import _apply_assignment


class ConstraintVerifyTests(unittest.TestCase):
    def test_assignment_matches_exact_nogood(self) -> None:
        self.assertTrue(assignment_matches_nogood({"a": "2", "b": "3"}, {"a": "2", "b": "3"}))
        self.assertFalse(assignment_matches_nogood({"a": "2", "b": "4"}, {"a": "2", "b": "3"}))
        self.assertFalse(assignment_matches_nogood({"a": "2"}, {"a": "2", "b": "3"}))

    def test_nogood_connects_independent_solver_components(self) -> None:
        graph = {"a": set(), "b": set(), "c": set()}
        merge_nogood_edges(graph, [{"a": "2", "c": "5"}])
        self.assertEqual({"c"}, graph["a"])
        self.assertEqual({"a"}, graph["c"])
        self.assertEqual(set(), graph["b"])

    def test_parallel_ddmin_finds_interacting_pair(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
            VerificationUnit("c", ("c",)),
            VerificationUnit("d", ("d",)),
        ]
        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            names = {item.id for item in candidate}
            return {"b", "d"}.issubset(names)

        culprit = parallel_ddmin(units, fails, parallelism=4, max_checks=20)
        self.assertEqual({"b", "d"}, {item.id for item in culprit})

    def test_parallel_ddmin_emits_progress_events(self) -> None:
        units = [VerificationUnit(name, (name,)) for name in ("a", "b", "c", "d")]
        events: list[str] = []

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {"b", "d"}.issubset({item.id for item in candidate})

        parallel_ddmin(
            units, fails, parallelism=2, max_checks=20,
            progress=lambda event, _details: events.append(event),
            progress_interval_seconds=1,
        )
        self.assertIn("start", events)
        self.assertIn("wave-start", events)
        self.assertIn("check-finish", events)
        self.assertIn("finish", events)

    def test_parallel_screening_failure_cannot_shrink_without_serial_confirmation(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
        ]

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            # Model a false positive that exists only while the parallel wave is
            # running (shared daemon/cache/port/resource interference).
            return {item.id for item in candidate} == {"a"}

        def isolated_confirmation(_candidate: tuple[VerificationUnit, ...]) -> bool:
            return False

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=isolated_confirmation,
        )
        self.assertEqual({"a", "b"}, {item.id for item in culprit})

    def test_parallel_screening_failure_shrinks_after_serial_confirmation(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
        ]

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return "a" in {item.id for item in candidate}

        culprit = parallel_ddmin(
            units,
            fails,
            parallelism=2,
            max_checks=8,
            confirm_failure=fails,
        )
        self.assertEqual({"a"}, {item.id for item in culprit})

    def test_serial_confirmation_emits_heartbeat(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def slow_confirmation(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(1.15)
            return True

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=slow_confirmation,
            progress=lambda event, _details: events.append(event),
            progress_interval_seconds=1,
            timeout_seconds=5,
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        self.assertIn("confirmation-heartbeat", events)

    def test_resume_reuses_screening_but_still_confirms_fail(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        parallel_calls = 0
        confirmation_calls = 0

        def parallel_screen(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal parallel_calls
            parallel_calls += 1
            raise AssertionError("cached screening result should be reused")

        def confirm(candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal confirmation_calls
            confirmation_calls += 1
            return {item.id for item in candidate} == {"a"}

        state = {
            "schemaVersion": 1,
            "initialUnitIds": ["a", "b"],
            "currentUnitIds": ["a", "b"],
            "granularity": 2,
            "checksStarted": 2,
            "cache": [
                {"unitIds": ["a"], "failed": True, "confirmedFailure": False},
                {"unitIds": ["b"], "failed": False, "confirmedFailure": False},
            ],
            "finished": False,
        }

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=confirm,
            resume_state=state,
        )
        self.assertEqual(0, parallel_calls)
        self.assertEqual(1, confirmation_calls)
        self.assertEqual({"a"}, {item.id for item in culprit})

    def test_checkpoint_can_resume_finished_localization_without_rechecks(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        states: list[dict[str, object]] = []

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        culprit = parallel_ddmin(
            units,
            fails,
            parallelism=2,
            max_checks=8,
            confirm_failure=fails,
            checkpoint=lambda state: states.append(dict(state)),
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        final_state = states[-1]
        self.assertEqual(["a"], final_state["currentUnitIds"])
        self.assertTrue(final_state["finished"])

        calls = 0
        def should_not_run(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal calls
            calls += 1
            return True

        resumed = parallel_ddmin(
            units,
            should_not_run,
            parallelism=2,
            max_checks=8,
            confirm_failure=should_not_run,
            resume_state=final_state,
        )
        self.assertEqual(0, calls)
        self.assertEqual({"a"}, {item.id for item in resumed})

    def test_serial_confirmation_obeys_total_watchdog(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def hangs(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(0.5)
            return True

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units,
                parallel_screen,
                parallelism=2,
                max_checks=8,
                confirm_failure=hangs,
                progress=lambda event, _details: events.append(event),
                progress_interval_seconds=1,
                timeout_seconds=0.1,
            )
        self.assertIn("confirmation-heartbeat", events)
        self.assertIn("timeout", events)

    def test_parallel_ddmin_has_total_watchdog(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]

        def hangs(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(0.25)
            return True

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units, hangs, parallelism=2, max_checks=4,
                timeout_seconds=0.05, progress_interval_seconds=1,
            )

    def test_global_exact_exclusion_keeps_components_independent(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        alternatives = {
            0: [RankedComponentAlternative({"a": "1"}, (7,))],
            1: [RankedComponentAlternative({"b": "1"}, (9,))],
        }

        def next_alt(index, existing):
            rank = len(existing) - 1
            values = alternatives[index]
            return values[rank] if rank < len(values) else None

        assignment, explored = coordinate_global_exact_exclusions(
            initial,
            [{"a": "2", "b": "2"}],
            next_alt,
        )
        self.assertEqual({"a": "2", "b": "1"}, assignment)
        self.assertGreaterEqual(explored, 2)

    def test_global_exact_exclusion_handles_multiple_blocked_tuples(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        alternatives = {
            0: [RankedComponentAlternative({"a": "1"}, (8,))],
            1: [RankedComponentAlternative({"b": "1"}, (9,))],
        }

        def next_alt(index, existing):
            rank = len(existing) - 1
            values = alternatives[index]
            return values[rank] if rank < len(values) else None

        assignment, _explored = coordinate_global_exact_exclusions(
            initial,
            [
                {"a": "2", "b": "2"},
                {"a": "2", "b": "1"},
            ],
            next_alt,
        )
        self.assertEqual({"a": "1", "b": "2"}, assignment)

    def test_global_exact_exclusion_never_strengthens_to_local_block(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        calls: list[tuple[int, tuple[tuple[tuple[str, str], ...], ...]]] = []

        def next_alt(index, existing):
            frozen = tuple(
                tuple(sorted(item.items()))
                for item in existing
            )
            calls.append((index, frozen))
            if index == 0 and len(existing) == 1:
                return RankedComponentAlternative({"a": "1"}, (1,))
            if index == 1 and len(existing) == 1:
                return RankedComponentAlternative({"b": "1"}, (9,))
            return None

        assignment, _ = coordinate_global_exact_exclusions(
            initial,
            [{"a": "2", "b": "2"}],
            next_alt,
        )
        self.assertEqual({"a": "2", "b": "1"}, assignment)
        # The callback receives only the local component history. The global
        # a@2+b@2 exclusion is never projected into an authoritative local a@2
        # or b@2 constraint.
        self.assertIn((1, ((("b", "2"),),)), calls)



class FixedNonRegistryInputTests(unittest.TestCase):
    class _Client:
        registry = "https://registry.example"
        npm_cache: dict[str, object] = {}
        registry_artifact_cache: dict[tuple[str, str], object] = {}

    def _row(
        self,
        name: str,
        spec: str,
        *,
        current: str = "1.0.0",
    ) -> roadmap.DependencyRow:
        return roadmap.DependencyRow(
            project="p",
            package_dir=".",
            name=name,
            kind="runtime",
            requested_spec=spec,
            current_version=current,
            current_source="fixture",
            latest_version=current,
            current_vulns="0",
            min_no_critical="вЂ”",
            min_no_high="вЂ”",
            min_no_vuln="вЂ”",
            min_lag_12m="вЂ”",
            min_lag_9m="вЂ”",
            min_lag_6m="вЂ”",
            min_lag_3m="вЂ”",
            group=5,
            reason="fixture",
            notes="fixture",
        )

    def test_non_registry_specs_are_fixed_inputs(self) -> None:
        for spec in (
            "git+https://example.invalid/lib.git#deadbeef",
            "github:owner/repo#deadbeef",
            "workspace:*",
            "file:../lib",
            "link:../lib",
            "portal:../lib",
            "https://example.invalid/lib.tgz",
        ):
            with self.subTest(spec=spec):
                self.assertTrue(
                    roadmap._is_fixed_dependency_input(
                        self._row("external", spec)
                    )
                )
        self.assertFalse(
            roadmap._is_fixed_dependency_input(
                self._row("registry", "^1.2.3")
            )
        )

    def test_fixed_inputs_are_not_exact_solver_components(self) -> None:
        external_lib = self._row(
            "external_lib",
            "git+https://example.invalid/external_lib.git#deadbeef",
            current="3.0.0",
        )
        react = self._row("react", "^18.2.0", current="18.2.0")
        components: list[tuple[str, ...]] = []

        def exact(
            component,
            _rows_by_name,
            domains,
            _client,
            _mode,
            _learned,
            _config,
            _stability=None,
        ):
            components.append(tuple(component))
            assignment = {
                name: domains[name][0]
                for name in component
            }
            return {
                "status": "optimal",
                "assignment": assignment,
                "changed": 0,
                "hardConstraints": 0,
                "refinements": 0,
                "elapsedMs": 0,
            }

        with patch.object(roadmap, "_run_z3_peer_component", side_effect=exact):
            result = roadmap.resolve_peer_compatibility(
                {"p": [external_lib, react]},
                self._Client(),
                modes=("yellow",),
                apply_results=False,
            )

        assignment = result["p"]["yellow"]
        self.assertNotIn("external_lib", assignment)
        self.assertEqual("18.2.0", assignment["react"])
        self.assertTrue(components)
        self.assertTrue(
            all("external_lib" not in component for component in components),
            components,
        )

    def test_matching_fixed_literal_projects_out_of_solver_clause(self) -> None:
        fixed = {
            "external_lib": self._row(
                "external_lib",
                "git+https://example.invalid/external_lib.git#deadbeef",
                current="3.0.0",
            )
        }
        projected = roadmap._project_constraints_over_fixed_inputs(
            [{"external_lib": "3.0.0", "react": "19.0.0"}],
            fixed,
            project="p",
            mode="yellow",
            source="fixture",
        )
        self.assertEqual([{"react": "19.0.0"}], projected)

    def test_mismatching_fixed_literal_makes_clause_unreachable(self) -> None:
        fixed = {
            "external_lib": self._row(
                "external_lib",
                "git+https://example.invalid/external_lib.git#deadbeef",
                current="3.0.0",
            )
        }
        projected = roadmap._project_constraints_over_fixed_inputs(
            [{"external_lib": "2.0.0", "react": "19.0.0"}],
            fixed,
            project="p",
            mode="yellow",
            source="fixture",
        )
        self.assertEqual([], projected)

    def test_fixed_only_matching_constraint_fails_closed(self) -> None:
        fixed = {
            "external_lib": self._row(
                "external_lib",
                "git+https://example.invalid/external_lib.git#deadbeef",
                current="3.0.0",
            )
        }
        with self.assertRaises(roadmap.BaselineConstraintVerificationError) as error:
            roadmap._project_constraints_over_fixed_inputs(
                [{"external_lib": "3.0.0"}],
                fixed,
                project="p",
                mode="yellow",
                source="fixture",
            )
        self.assertIn("FIXED_INPUT_CONSTRAINT_CONFLICT", str(error.exception))

    def test_verifier_does_not_rewrite_fixed_git_manifest_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manifest = {
                "dependencies": {
                    "external_lib": "git+https://example.invalid/external_lib.git#deadbeef",
                    "react": "^18.2.0",
                }
            }
            (project / "package.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            changed = _apply_assignment(
                project,
                {"react": "18.3.1"},
            )
            after = json.loads(
                (project / "package.json").read_text(encoding="utf-8")
            )

        self.assertEqual(["react"], changed)
        self.assertEqual(
            "git+https://example.invalid/external_lib.git#deadbeef",
            after["dependencies"]["external_lib"],
        )
        self.assertEqual("18.3.1", after["dependencies"]["react"])

    def test_constraint_cache_schema_invalidates_pre_h_solver_learning(self) -> None:
        self.assertEqual(
            "peer-ir-v2-fixed-inputs",
            constraint_cache.SOLVER_SCHEMA_VERSION,
        )

    def test_fixed_source_lock_change_changes_resolver_environment_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "package.json").write_text(
                json.dumps(
                    {
                        "packageManager": "yarn@1.22.22",
                        "dependencies": {
                            "external_lib": "git+https://example.invalid/external_lib.git#main"
                        },
                    }
                ),
                encoding="utf-8",
            )
            lock = project / "yarn.lock"
            lock.write_text(
                'external_lib@git+https://example.invalid/external_lib.git#main:\\n'
                '  resolved "https://example.invalid/external_lib.git#aaaaaaaa"\\n',
                encoding="utf-8",
            )
            stable_command = {
                "command": "fixture",
                "executable": "fixture",
                "version": "1",
            }
            with patch.object(
                constraint_cache,
                "_command_identity",
                return_value=stable_command,
            ):
                before = constraint_cache.resolver_environment_fingerprint(
                    project,
                    registry="https://registry.example",
                )
                lock.write_text(
                    'external_lib@git+https://example.invalid/external_lib.git#main:\\n'
                    '  resolved "https://example.invalid/external_lib.git#bbbbbbbb"\\n',
                    encoding="utf-8",
                )
                after = constraint_cache.resolver_environment_fingerprint(
                    project,
                    registry="https://registry.example",
                )
        self.assertNotEqual(before, after)

    def test_h_production_source_has_managed_solver_boundary(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "solver_rows_by_name, fixed_rows_by_name = _partition_solver_inputs(rows_by_name)",
            source,
        )
        self.assertIn(
            "for name, row in solver_rows_by_name.items():",
            source,
        )
        self.assertIn(
            "graph = _potential_peer_graph(solver_rows_by_name, domains, client)",
            source,
        )
        self.assertIn("FIXED_INPUT_CONSTRAINT_CONFLICT", source)


if __name__ == "__main__":
    unittest.main()

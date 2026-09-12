from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi591_unified_cohort_handoff import plan_cohort_handoff


@dataclass(frozen=True)
class Suggestion:
    packages: tuple[str, ...] = ("plugin",)
    cohort_id: str = "vite-build"
    confidence: float = 0.9


@dataclass(frozen=True)
class Fallback:
    assignment_dict: dict[str, str]
    deferred_packages: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class Decision:
    queue: bool
    reason: str = "new-exact-candidate"


@dataclass(frozen=True)
class Control:
    queue_fallback: bool
    reason: str = "queue-new-fallback"


def build_fallback(*, assignment, current_versions, cohort_packages):
    candidate = dict(assignment)
    deferred = []
    for name in cohort_packages:
        if name in candidate and name in current_versions and candidate[name] != current_versions[name]:
            candidate[name] = current_versions[name]
            deferred.append(name)
    return Fallback(candidate, tuple(sorted(deferred)), bool(deferred))


def infer(**_kwargs):
    return Suggestion()


def queue_decision(fingerprint, **_kwargs):
    return Decision(bool(fingerprint))


def handoff_control(decision):
    return Control(decision.queue)


def fp(assignment):
    return "|".join(f"{k}={v}" for k, v in sorted(assignment.items()))


class AutonomousPreIncumbentRescueTests(unittest.TestCase):
    def setUp(self):
        self.old_mode = os.environ.get("DEPLOOM_BASELINE_CONTROL_MODE")

    def tearDown(self):
        if self.old_mode is None:
            os.environ.pop("DEPLOOM_BASELINE_CONTROL_MODE", None)
        else:
            os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = self.old_mode

    def plan(self, **overrides):
        kwargs = dict(
            predicate="ts-module-resolution:@vitejs/plugin-react",
            direct_packages=("plugin", "other", "must"),
            assignment={"plugin": "2", "other": "2", "must": "2"},
            current_versions={"plugin": "1", "other": "1", "must": "1"},
            policy_by_package={"plugin": "auto", "other": "auto", "must": "required"},
            repeated_count=2,
            has_verified_incumbent=False,
            assignment_fingerprint_fn=fp,
            _infer_cohort=infer,
            _build_fallback=build_fallback,
            _queue_decision=queue_decision,
            _handoff_control=handoff_control,
        )
        kwargs.update(overrides)
        return plan_cohort_handoff(**kwargs)

    def test_autonomous_repeated_predicate_builds_verified_floor_candidate(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "AUTONOMOUS"
        plan = self.plan()
        self.assertTrue(plan.actionable)
        self.assertTrue(plan.bootstrap_floor)
        self.assertEqual("autonomous-pre-incumbent-floor", plan.reason)
        # Auto packages fall back to current; required target is preserved.
        self.assertEqual(
            {"plugin": "1", "other": "1", "must": "2"},
            plan.assignment_dict,
        )
        self.assertEqual(("other", "plugin"), plan.fallback.deferred_packages)

    def test_confirm_significant_keeps_existing_bounded_cohort_behavior(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "CONFIRM_SIGNIFICANT"
        plan = self.plan()
        self.assertFalse(plan.bootstrap_floor)
        self.assertEqual({"plugin": "1", "other": "2", "must": "2"}, plan.assignment_dict)

    def test_existing_incumbent_never_triggers_floor_rescue(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "AUTONOMOUS"
        plan = self.plan(has_verified_incumbent=True)
        self.assertFalse(plan.bootstrap_floor)
        self.assertEqual({"plugin": "1", "other": "2", "must": "2"}, plan.assignment_dict)

    def test_single_observation_does_not_trigger_floor_rescue(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "AUTONOMOUS"
        plan = self.plan(repeated_count=1)
        self.assertFalse(plan.bootstrap_floor)


    def test_budget_reserved_floor_can_trigger_after_first_observation(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "AUTONOMOUS"
        plan = self.plan(
            repeated_count=1,
            bootstrap_floor_requested=True,
        )
        self.assertTrue(plan.actionable)
        self.assertTrue(plan.bootstrap_floor)
        self.assertEqual(
            {"plugin": "1", "other": "1", "must": "2"},
            plan.assignment_dict,
        )

    def test_floor_does_not_depend_on_cohort_inference(self):
        os.environ["DEPLOOM_BASELINE_CONTROL_MODE"] = "AUTONOMOUS"

        def no_cohort(**_kwargs):
            return None

        plan = self.plan(_infer_cohort=no_cohort)
        self.assertTrue(plan.actionable)
        self.assertTrue(plan.bootstrap_floor)
        self.assertIsNone(plan.suggestion)



class GeneratorIntegrationTests(unittest.TestCase):
    def test_generator_passes_incumbent_context_to_both_handoff_paths(self):
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("has_verified_incumbent=("), 2)
        self.assertIn("anytime.incumbent is not None", source)
        self.assertEqual(
            source.count("stagnated = anytime.observe_candidate("),
            source.count("BLOCK_PSI_AUTONOMOUS_PRE_INCUMBENT_EARLY_RESCUE_V2"),
        )
        self.assertIn("bootstrap_floor_requested=True", source)
        self.assertIn("_floor_last_predicted_slot", source)


if __name__ == "__main__":
    unittest.main()

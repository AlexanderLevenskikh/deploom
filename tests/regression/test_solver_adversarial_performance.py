from __future__ import annotations

import random
import time
import unittest

from peer_solver_model import (
    PackageVariable,
    PeerOptimizationModel,
    RequiresAny,
    forbidden,
    solve_reference_exact,
)
from peer_solver_z3 import solve_z3_exact


def package(name: str, versions: int = 3) -> PackageVariable:
    domain = tuple(f"{index}.0.0" for index in range(1, versions + 1))
    scores = tuple(
        (version, (index, -(index % 2)))
        for index, version in enumerate(domain, start=1)
    )
    return PackageVariable(
        name=name,
        current_version=domain[0],
        domain=domain,
        scores=scores,
    )


def small_adversarial_model(seed: int) -> PeerOptimizationModel:
    rng = random.Random(seed)
    packages = tuple(package(f"pkg-{index:02d}") for index in range(6))
    constraints = []
    seen = set()

    for _ in range(12):
        left, right = sorted(rng.sample(range(len(packages)), 2))
        literals = (
            (packages[left].name, packages[left].domain[rng.randrange(3)]),
            (packages[right].name, packages[right].domain[rng.randrange(3)]),
        )
        key = tuple(sorted(literals))
        if key in seen:
            continue
        seen.add(key)
        constraints.append(
            forbidden(key, reason="synthetic-adversarial", provenance="test")
        )

    requirements = tuple(
        RequiresAny(
            trigger=(packages[index].name, packages[index].domain[-1]),
            provider=packages[index + 1].name,
            allowed_versions=packages[index + 1].domain[1:],
            reason="synthetic-peer",
            provenance="test",
        )
        for index in range(len(packages) - 1)
    )

    return PeerOptimizationModel(
        packages=packages,
        constraints=tuple(constraints),
        requirements=requirements,
        objective_width=2,
    )


class ExactSolverAdversarialTests(unittest.TestCase):
    def assert_z3_matches_reference(self, model: PeerOptimizationModel) -> None:
        reference = solve_reference_exact(model, max_states=100_000)
        z3 = solve_z3_exact(model, timeout_ms=10_000)
        self.assertEqual(reference.status, z3.status, (reference, z3))
        if reference.status == "optimal":
            self.assertEqual(reference.score, z3.score)
            self.assertEqual(reference.assignment, z3.assignment)

    def test_z3_matches_independent_reference_across_seeded_models(self) -> None:
        for seed in range(12):
            with self.subTest(seed=seed):
                self.assert_z3_matches_reference(
                    small_adversarial_model(seed)
                )

    def test_package_and_constraint_order_do_not_change_exact_result(self) -> None:
        model = small_adversarial_model(41)
        reversed_model = PeerOptimizationModel(
            packages=tuple(reversed(model.packages)),
            constraints=tuple(reversed(model.constraints)),
            requirements=tuple(reversed(model.requirements)),
            objective_width=model.objective_width,
        )
        first = solve_z3_exact(model, timeout_ms=10_000)
        second = solve_z3_exact(reversed_model, timeout_ms=10_000)
        self.assertEqual("optimal", first.status)
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.score, second.score)
        self.assertEqual(first.assignment, second.assignment)

    def test_unsat_requires_an_exact_unsat_proof(self) -> None:
        pkg = package("only", versions=2)
        model = PeerOptimizationModel(
            packages=(pkg,),
            constraints=(
                forbidden((("only", pkg.domain[0]),), provenance="test"),
                forbidden((("only", pkg.domain[1]),), provenance="test"),
            ),
            objective_width=2,
        )
        result = solve_z3_exact(model, timeout_ms=5_000)
        self.assertEqual("unsat", result.status)
        self.assertIsNone(result.assignment)

    def test_dense_nogood_set_stays_exact(self) -> None:
        rng = random.Random(20260817)
        packages = tuple(package(f"dense-{index:02d}", versions=4) for index in range(14))
        constraints = []
        seen = set()
        for _ in range(180):
            selected = sorted(rng.sample(range(len(packages)), 3))
            literals = tuple(
                (
                    packages[index].name,
                    packages[index].domain[rng.randrange(4)],
                )
                for index in selected
            )
            key = tuple(sorted(literals))
            if key in seen:
                continue
            seen.add(key)
            constraints.append(forbidden(key, provenance="learned-test"))

        model = PeerOptimizationModel(
            packages=packages,
            constraints=tuple(constraints),
            objective_width=2,
        )
        result = solve_z3_exact(model, timeout_ms=12_000)
        self.assertEqual("optimal", result.status, result.detail)
        self.assertFalse(model.assignment_issue(result.assignment or {}))

    def test_medium_synthetic_performance_envelope(self) -> None:
        packages = tuple(package(f"perf-{index:02d}", versions=4) for index in range(36))
        constraints = []
        requirements = []

        for index in range(len(packages) - 1):
            constraints.append(
                forbidden(
                    (
                        (packages[index].name, packages[index].domain[-1]),
                        (packages[index + 1].name, packages[index + 1].domain[-1]),
                    ),
                    provenance="performance-test",
                )
            )
        for index in range(0, len(packages) - 2, 3):
            constraints.append(
                forbidden(
                    (
                        (packages[index].name, packages[index].domain[-2]),
                        (packages[index + 1].name, packages[index + 1].domain[-1]),
                        (packages[index + 2].name, packages[index + 2].domain[-1]),
                    ),
                    provenance="performance-test",
                )
            )
        for index in range(0, len(packages) - 1, 4):
            requirements.append(
                RequiresAny(
                    trigger=(packages[index].name, packages[index].domain[-1]),
                    provider=packages[index + 1].name,
                    allowed_versions=packages[index + 1].domain[1:],
                    provenance="performance-test",
                )
            )

        model = PeerOptimizationModel(
            packages=packages,
            constraints=tuple(constraints),
            requirements=tuple(requirements),
            objective_width=2,
        )

        started = time.perf_counter()
        result = solve_z3_exact(model, timeout_ms=12_000)
        wall_ms = int((time.perf_counter() - started) * 1000)

        print(
            "SOLVER_PERF",
            f"packages={len(packages)}",
            f"candidates={model.candidate_count()}",
            f"constraints={len(constraints)}",
            f"requirements={len(requirements)}",
            f"solverElapsedMs={result.elapsed_ms}",
            f"wallMs={wall_ms}",
            f"status={result.status}",
        )

        self.assertEqual("optimal", result.status, result.detail)
        self.assertLess(wall_ms, 15_000)
        self.assertFalse(model.assignment_issue(result.assignment or {}))


if __name__ == "__main__":
    unittest.main()

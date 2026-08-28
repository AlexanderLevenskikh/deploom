"""WS10: a certified generalized clause is only valid over the domain it was
certified against.

Bounded universal certification proves a predicate for every omitted-variable
value in the CURRENT finite domain. It says nothing about values that domain did
not contain, so a registry refetch, intent change or replan that grows the
domain must revoke the certificate until it is proven again.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as gen


class _Row:
    def __init__(self, name: str) -> None:
        self.name = name


class LearnedClauseDomainGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = {"left": _Row("left"), "right": _Row("right")}
        self.clause = {"left": "1.0.0", "right": "2.0.0"}

    def _fingerprint(self, domains):
        with patch.object(
            gen, "_candidate_domain", lambda row, mode, client: domains[row.name]
        ):
            return gen._clause_domain_fingerprint(
                self.clause, self.rows, "yellow", client=None
            )

    def test_same_domain_yields_the_same_fingerprint(self) -> None:
        domains = {"left": ["1.0.0", "1.1.0"], "right": ["2.0.0"]}
        self.assertEqual(self._fingerprint(domains), self._fingerprint(domains))

    def test_domain_growth_changes_the_fingerprint(self) -> None:
        before = self._fingerprint({"left": ["1.0.0", "1.1.0"], "right": ["2.0.0"]})
        after = self._fingerprint(
            {"left": ["1.0.0", "1.1.0", "1.2.0"], "right": ["2.0.0"]}
        )
        self.assertNotEqual(before, after, "a grown domain kept its fingerprint")

    def test_domain_ordering_does_not_change_the_fingerprint(self) -> None:
        a = self._fingerprint({"left": ["1.0.0", "1.1.0"], "right": ["2.0.0"]})
        b = self._fingerprint({"left": ["1.1.0", "1.0.0"], "right": ["2.0.0"]})
        self.assertEqual(a, b, "fingerprint must not depend on candidate order")

    def test_grown_domain_revokes_the_certified_clause(self) -> None:
        domains = {"left": ["1.0.0", "1.1.0"], "right": ["2.0.0"]}
        certified = self._fingerprint(domains)
        clauses = [dict(self.clause)]
        fingerprints = {gen.assignment_fingerprint(self.clause): certified}

        domains["left"] = ["1.0.0", "1.1.0", "1.2.0"]
        with patch.object(
            gen, "_candidate_domain", lambda row, mode, client: domains[row.name]
        ):
            dropped = gen._drop_uncertified_generalized_clauses(
                clauses, fingerprints, self.rows, "yellow", None
            )
        self.assertEqual(len(dropped), 1, "stale certificate stayed authoritative")
        self.assertEqual(clauses, [], "revoked clause still applied to the solver")
        self.assertEqual(fingerprints, {})

    def test_unchanged_domain_keeps_the_certified_clause(self) -> None:
        domains = {"left": ["1.0.0", "1.1.0"], "right": ["2.0.0"]}
        certified = self._fingerprint(domains)
        clauses = [dict(self.clause)]
        fingerprints = {gen.assignment_fingerprint(self.clause): certified}
        with patch.object(
            gen, "_candidate_domain", lambda row, mode, client: domains[row.name]
        ):
            dropped = gen._drop_uncertified_generalized_clauses(
                clauses, fingerprints, self.rows, "yellow", None
            )
        self.assertEqual(dropped, [], "a still-valid certificate was revoked")
        self.assertEqual(len(clauses), 1)

    def test_uncertified_clauses_are_left_alone(self) -> None:
        """Exact-assignment clauses carry no domain certificate and must stay."""
        clauses = [dict(self.clause)]
        with patch.object(
            gen, "_candidate_domain", lambda row, mode, client: ["9.9.9"]
        ):
            dropped = gen._drop_uncertified_generalized_clauses(
                clauses, {}, self.rows, "yellow", None
            )
        self.assertEqual(dropped, [])
        self.assertEqual(len(clauses), 1)


if __name__ == "__main__":
    unittest.main()

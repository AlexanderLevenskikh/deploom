"""Shared workspace-glob matcher: segment semantics, braces, globstar, unsupported grammar.

Covers B3 (`packages/*` must not own `packages/group/deep`) and B4 (brace
patterns `{apps,packages}/*`), plus the explicit typed failure for extglob.
"""
from __future__ import annotations

import unittest

from workspace_glob import (
    expand_braces,
    match_workspace_pattern,
    unsupported_workspace_pattern,
)


class BraceExpansionTests(unittest.TestCase):
    def test_plain_pattern_is_unchanged(self) -> None:
        self.assertEqual(["packages/*"], expand_braces("packages/*"))

    def test_simple_alternatives(self) -> None:
        self.assertEqual(
            ["apps/*", "packages/*"],
            expand_braces("{apps,packages}/*"),
        )

    def test_nested_groups(self) -> None:
        self.assertEqual(
            ["a/d", "b/d", "c/d"],
            expand_braces("{a,{b,c}}/d"),
        )

    def test_negative_prefix_survives_expansion(self) -> None:
        self.assertEqual(["!packages/lib"], expand_braces("!packages/lib"))

    def test_empty_alternative_is_a_literal_slot(self) -> None:
        # `packages/{lib,}` means packages/lib AND packages/ themselves.
        self.assertEqual(["packages/lib", "packages/"], expand_braces("packages/{lib,}"))


class MatchSemanticsTests(unittest.TestCase):
    def test_single_star_stays_within_one_segment(self) -> None:
        self.assertTrue(match_workspace_pattern("packages/app", "packages/*"))
        self.assertFalse(match_workspace_pattern("packages/group/deep", "packages/*"))

    def test_star_does_not_match_top_level_sibling(self) -> None:
        self.assertFalse(match_workspace_pattern("apps/web", "packages/*"))

    def test_globstar_crosses_segments(self) -> None:
        self.assertTrue(match_workspace_pattern("packages/app", "packages/**"))
        self.assertTrue(match_workspace_pattern("packages/group/deep/app", "packages/**"))

    def test_middle_globstar_allows_zero_intermediate_folders(self) -> None:
        self.assertTrue(match_workspace_pattern("packages/app", "packages/**/app"))
        self.assertTrue(match_workspace_pattern("packages/team/app", "packages/**/app"))
        self.assertTrue(match_workspace_pattern("packages/team/group/app", "packages/**/app"))
        self.assertFalse(match_workspace_pattern("packages/app/x", "packages/**/app"))
        self.assertFalse(match_workspace_pattern("vendor/app", "packages/**/app"))

    def test_leading_globstar_allows_zero_intermediate_folders(self) -> None:
        self.assertTrue(match_workspace_pattern("packages/app", "**/packages/app"))
        self.assertTrue(match_workspace_pattern("packages/app", "**/app"))
        self.assertTrue(match_workspace_pattern("app", "**/app"))
        self.assertFalse(match_workspace_pattern("vendor/app", "**/packages/app"))

    def test_brace_patterns_match_both_alternatives(self) -> None:
        self.assertTrue(match_workspace_pattern("apps/web", "{apps,packages}/*"))
        self.assertTrue(match_workspace_pattern("packages/app", "{apps,packages}/*"))
        self.assertFalse(match_workspace_pattern("other/x", "{apps,packages}/*"))

    def test_brace_with_deep_segment_stays_shallow(self) -> None:
        self.assertFalse(match_workspace_pattern("apps/a/b/c", "{apps,packages}/*"))

    def test_globstar_brace_combination(self) -> None:
        self.assertTrue(match_workspace_pattern("apps/web", "{apps,packages}/**"))
        self.assertTrue(match_workspace_pattern("apps/web/deep", "{apps,packages}/**"))

    def test_question_mark_and_class(self) -> None:
        self.assertTrue(match_workspace_pattern("packages/ab", "packages/a?"))
        self.assertFalse(match_workspace_pattern("packages/a", "packages/a?"))
        self.assertFalse(match_workspace_pattern("packages/abc", "packages/a?"))
        self.assertTrue(match_workspace_pattern("packages/a1", "packages/a[0-9]"))
        self.assertFalse(match_workspace_pattern("packages/ax", "packages/a[0-9]"))

    def test_negation_marker_never_directly_matches(self) -> None:
        self.assertFalse(match_workspace_pattern("packages/app", "!packages/app"))

    def test_root_pattern(self) -> None:
        self.assertTrue(match_workspace_pattern("any/deep/path", "**"))
        self.assertTrue(match_workspace_pattern("app", "**"))


class UnsupportedGrammarTests(unittest.TestCase):
    def test_extglob_openers_are_reported(self) -> None:
        for opener in ("?(", "*(", "+(", "@(", "!("):
            self.assertIn(
                "not supported",
                unsupported_workspace_pattern(f"packages/{opener}a|b)"),
                f"extglob opener {opener} must be detected",
            )

    def test_negated_extglob_is_reported(self) -> None:
        self.assertTrue(unsupported_workspace_pattern("!@(a|b)"))

    def test_plain_patterns_are_supported(self) -> None:
        for pattern in ("packages/*", "./packages/*/", "apps/**", "**", "{a,b}/*"):
            self.assertEqual("", unsupported_workspace_pattern(pattern))

    def test_unsupported_extglob_never_matches(self) -> None:
        self.assertFalse(match_workspace_pattern("packages/a", "packages/@(a|b)"))


if __name__ == "__main__":
    unittest.main()

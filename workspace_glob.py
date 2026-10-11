"""Shared workspace-glob matcher for project topology and source scope.

npm/Yarn/pnpm workspace paths are matched with gitignore-style segment
semantics:

* ``*``  matches within ONE path segment (never ``/``),
* ``?``  matches any single non-separator character,
* ``[...]`` matches a character class inside one segment,
* ``**`` (a whole segment) matches across path segments,
* ``{a,b}`` expands to concrete alternatives (nested-safe).

This is deliberately different from Python's ``fnmatch``, whose ``*`` also
matches ``/`` (and would let ``packages/*`` own ``packages/group/deep``, the
B3 defect). Both ``project_topology`` and ``scoped_source_policy`` use this
single matcher so ownership, membership and source-closure always agree.

Unsupported grammar (extglob: ``?( ) *( ) +( ) @( ) !( )``, or ``|`` inside
brace groups) is detected by :func:`unsupported_workspace_pattern` so callers
can raise a typed error instead of silently producing a misleading
missing-lockfile outcome.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import List, Optional, Pattern, Tuple


def expand_braces(pattern: str) -> List[str]:
    """Expand ``{a,b}`` groups into concrete patterns (nested-safe).

    A pattern without braces is returned unchanged as a single alternative.
    An unbalanced ``{`` is treated as a literal (matching minimatch). The
    returned alternatives keep the caller's relative path semantics; they are
    meant to be filtered with :func:`match_workspace_pattern`.
    """
    pattern = (pattern or "").strip()
    if "{" not in pattern:
        return [pattern] if pattern else []
    out: List[str] = []
    _expand_braces(pattern, out)
    return out


def _expand_braces(pattern: str, out: List[str]) -> None:
    group = _first_brace_group(pattern)
    if group is None:
        if pattern not in out:
            out.append(pattern)
        return
    head, inner, tail = group
    for alternative in inner.split(","):
        _expand_braces(head + alternative + tail, out)


def _first_brace_group(pattern: str) -> Optional[Tuple[str, str, str]]:
    """Return ``(head, inner, tail)`` of the FIRST innermost ``{...}`` group.

    ``None`` when the pattern has no well-formed brace group.
    """
    n = len(pattern)
    i = 0
    while i < n:
        if pattern[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        while j < n:
            char = pattern[j]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            return None  # unbalanced '{' -> treat as literal
        inner = pattern[i + 1:j]
        if "{" in inner:
            nested = pattern.find("{", i + 1)
            i = nested if nested != -1 else i + 1
            continue
        return pattern[:i], inner, pattern[j + 1:]
    return None


def unsupported_workspace_pattern(pattern: str) -> str:
    """Return a human reason when ``pattern`` uses unsupported glob grammar.

    Returns ``""`` for supported grammar. Extglob (``?( ) *( ) +( ) @( ) !( )``)
    and ``|`` alternation inside brace groups cannot be expressed by the
    segment matcher; a typed failure is honest where a silent miss is not.
    """
    text = (pattern or "").strip()
    if not text:
        return ""
    body = text[1:] if text[0] == "!" else text
    for opener in ("?(", "*(", "+(", "@(", "!("):
        if opener in body:
            return f"extglob `{opener}...)` is not supported"
    if "|" in body:
        pair = _first_brace_group(body)
        if pair is not None and "|" in pair[1]:
            return "alternation `|` inside braces `{...}` is not supported"
    return ""


def _translate_segment(segment: str) -> str:
    """Translate ONE path segment into its regex fragment (segment-local)."""
    if segment == "**":
        return r"(?:[^/]+(?:/[^/]+)*)?"
    out: List[str] = []
    i = 0
    n = len(segment)
    while i < n:
        char = segment[i]
        if char == "*":
            out.append(r"[^/]*")
            i += 1
        elif char == "?":
            out.append(r"[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < n and segment[j] in "!^":
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:
                # unclosed '[' -> literal
                out.append(re.escape("["))
                i += 1
            else:
                cls = segment[i + 1:j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                if "/" in cls:
                    # A separator inside a class would let it cross segments.
                    out.append(re.escape("["))
                    i += 1
                else:
                    out.append("[" + cls + "]")
                    i = j + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


@lru_cache(maxsize=4096)
def _compiled_pattern(pattern: str) -> Pattern[str]:
    segments = [segment for segment in pattern.split("/") if segment]
    if not segments:
        return re.compile(r"^$")
    if "**" not in segments:
        parts = [_translate_segment(segment) for segment in segments]
        return re.compile("^" + "/".join(parts) + "$")
    return re.compile(_globstar_regex(segments))


def _globstar_regex(segments: List[str]) -> str:
    """Regex for a pattern containing whole-segment ``**`` globstars.

    ``**`` (a whole segment) matches ZERO OR MORE path segments, so
    ``packages/**/app`` matches ``packages/app`` (zero intermediate folders), a
    single level (``packages/a/app``) and deep nesting. The slash separators
    around a globstar are absorbed into its token:
    * leading  ``**/x/y``  -> ``(?:[^/]+/)* x/y``
    * middle   ``a/**/z``  -> ``a (?:/[^/]+)*/ z``
    * trailing ``a/**``    -> ``a (?:/[^/]+)*``
    * lone     ``**``      -> ``.*``
    """
    runs: List[Optional[List[str]]] = []
    current: List[str] = []
    for segment in segments:
        if segment == "**":
            if current:
                runs.append(current)
                current = []
            if not runs or runs[-1] is not None:
                runs.append(None)
        else:
            current.append(segment)
    if current:
        runs.append(current)
    if all(run is None for run in runs):
        return r"^.*$"
    out = ""
    first = runs[0]
    if first is None:
        out = r"(?:[^/]+/)*"
        runs = runs[1:]
    for index, item in enumerate(runs):
        if item is None:
            sep = r"(?:/[^/]+)*/" if index < len(runs) - 1 else r"(?:/[^/]+)*"
            out += sep
            continue
        out += "/".join(_translate_segment(segment) for segment in item)
    return "^" + out + "$"


def match_workspace_pattern(relative: str, pattern: str) -> bool:
    """Segment-aware workspace ownership match.

    ``relative`` is a repository-relative POSIX path (may include multiple
    segments). ``pattern`` is a workspace pattern; a leading ``!`` is treated
    as a negation marker and never matches directly (negation is handled by
    the caller, mirroring the historical contract). Brace groups are expanded;
    None matching an alternative means no match.
    """
    relative = (relative or "").strip("/").replace("\\", "/")
    pattern = (pattern or "").strip("/").replace("\\", "/")
    if not relative or not pattern or pattern.startswith("!"):
        return False
    if unsupported_workspace_pattern(pattern):
        return False
    return any(
        _compiled_pattern(alternative).match(relative) is not None
        for alternative in expand_braces(pattern)
    )

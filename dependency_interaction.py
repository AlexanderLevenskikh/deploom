#!/usr/bin/env python3
"""Solver-adjacent interaction IR for dependency compatibility verification.

Hard solver constraints and verification-only interactions are deliberately kept
separate.  A normal npm dependency may legally resolve to a nested copy, so it
must not be converted into a fake peer/equality constraint.  It *does* tell the
Baseline verifier which direct packages should be considered together when a
candidate assignment is package-manager-green but project-red.
"""
from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, Mapping, Set, Tuple


@dataclasses.dataclass(frozen=True, order=True)
class InteractionEdge:
    left: str
    right: str
    kind: str
    provenance: str = "metadata"
    detail: str = ""

    @staticmethod
    def create(left: str, right: str, *, kind: str, provenance: str = "metadata", detail: str = "") -> "InteractionEdge":
        a, b = sorted((str(left), str(right)))
        if not a or not b or a == b:
            raise ValueError("interaction edge requires two distinct package names")
        return InteractionEdge(a, b, str(kind), str(provenance), str(detail))


PEER_REQUIREMENT = "peer-requirement"
DIRECT_SHADOWING = "direct-shadowing"
LEARNED_NOGOOD = "learned-nogood"


def graph_from_edges(names: Iterable[str], edges: Iterable[InteractionEdge]) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = {str(name): set() for name in names}
    for edge in edges:
        if edge.left not in graph or edge.right not in graph:
            continue
        graph[edge.left].add(edge.right)
        graph[edge.right].add(edge.left)
    return graph


def merge_edges(graph: Dict[str, Set[str]], edges: Iterable[InteractionEdge]) -> None:
    for edge in edges:
        if edge.left not in graph or edge.right not in graph:
            continue
        graph[edge.left].add(edge.right)
        graph[edge.right].add(edge.left)


def edge_index(edges: Iterable[InteractionEdge]) -> Mapping[str, Tuple[InteractionEdge, ...]]:
    by_name: Dict[str, list[InteractionEdge]] = {}
    for edge in edges:
        by_name.setdefault(edge.left, []).append(edge)
        by_name.setdefault(edge.right, []).append(edge)
    return {name: tuple(sorted(items)) for name, items in by_name.items()}

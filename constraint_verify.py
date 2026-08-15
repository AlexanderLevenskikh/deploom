#!/usr/bin/env python3
"""Deterministic helpers for Baseline solve-and-verify refinement.

This module deliberately contains no registry/project-specific logic.  The
roadmap generator supplies candidate verification callbacks while this module
handles parallel delta-debugging and learned `nogood` constraints.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import time


Nogood = Dict[str, str]


class LocalizationTimeoutError(RuntimeError):
    """Raised when deterministic conflict localization exceeds its watchdog."""


ProgressCallback = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True)
class VerificationUnit:
    """A set of packages that must be verified together.

    Peer-connected packages are kept in one unit so delta-debugging never tests
    a knowingly incomplete peer cohort and mistakes that artificial failure for
    a real project incompatibility.
    """

    id: str
    packages: Tuple[str, ...]


def assignment_matches_nogood(assignment: Mapping[str, str], nogood: Mapping[str, str]) -> bool:
    """True only when every package/version in *nogood* is present exactly."""
    return bool(nogood) and all(assignment.get(name) == version for name, version in nogood.items())


def merge_nogood_edges(graph: Dict[str, set[str]], nogoods: Iterable[Mapping[str, str]]) -> None:
    """Turn cross-component learned constraints into solver graph edges.

    Once verification proves that A@x + B@y is invalid, A and B must be solved
    in one component; otherwise independent component optimization could keep
    reproducing the same globally invalid combination forever.
    """
    for nogood in nogoods:
        names = sorted(name for name in nogood if name in graph)
        if len(names) < 2:
            continue
        anchor = names[0]
        for name in names[1:]:
            graph[anchor].add(name)
            graph[name].add(anchor)


def _partitions(items: Sequence[VerificationUnit], count: int) -> List[List[VerificationUnit]]:
    count = max(1, min(count, len(items)))
    result: List[List[VerificationUnit]] = [[] for _ in range(count)]
    for index, item in enumerate(items):
        result[index % count].append(item)
    return [part for part in result if part]


def parallel_ddmin(
    units: Sequence[VerificationUnit],
    fails: Callable[[Tuple[VerificationUnit, ...]], bool],
    *,
    parallelism: int = 4,
    max_checks: int = 24,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: float = 15.0,
    timeout_seconds: Optional[float] = None,
) -> Tuple[VerificationUnit, ...]:
    """Return a small failure-inducing subset using parallel delta debugging.

    `fails(subset)` must be deterministic. Candidates at the same ddmin level
    are evaluated concurrently. Results are consumed in deterministic input
    order so increasing parallelism cannot change the selected culprit set.

    The watchdog is deliberately outside the worker callbacks: even if one
    verifier process stops producing output, the coordinator still emits
    heartbeats and eventually raises instead of waiting forever.
    """
    current = tuple(units)
    if len(current) <= 1 or max_checks <= 0:
        return current

    cache: Dict[Tuple[str, ...], bool] = {}
    checks = 0
    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    progress_interval_seconds = max(1.0, float(progress_interval_seconds or 15.0))

    def emit(event: str, **details: object) -> None:
        if progress is None:
            return
        payload = {
            "elapsedSeconds": round(time.monotonic() - started, 1),
            "checksStarted": checks,
            "maxChecks": max_checks,
            "currentUnits": len(current),
            **details,
        }
        progress(event, payload)

    def key(candidate: Sequence[VerificationUnit]) -> Tuple[str, ...]:
        return tuple(sorted(item.id for item in candidate))

    def package_count(candidate: Sequence[VerificationUnit]) -> int:
        return len({name for item in candidate for name in item.packages})

    def evaluate_many(candidates: Sequence[Tuple[VerificationUnit, ...]], *, wave: str) -> List[bool | None]:
        nonlocal checks
        results: List[bool | None] = [None] * len(candidates)
        pending: List[Tuple[int, Tuple[VerificationUnit, ...], int]] = []
        for index, candidate in enumerate(candidates):
            candidate_key = key(candidate)
            if candidate_key in cache:
                results[index] = cache[candidate_key]
            elif checks < max_checks:
                checks += 1
                pending.append((index, candidate, checks))
        if not pending:
            return results

        workers = max(1, min(parallelism, len(pending)))
        emit("wave-start", wave=wave, candidates=len(pending), active=workers)
        pool = ThreadPoolExecutor(max_workers=workers)
        future_map = {}

        def run_candidate(candidate: Tuple[VerificationUnit, ...], check_number: int) -> bool:
            check_started = time.monotonic()
            emit(
                "check-start",
                wave=wave,
                check=check_number,
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )
            try:
                value = bool(fails(candidate))
                emit(
                    "check-finish",
                    wave=wave,
                    check=check_number,
                    failed=value,
                    checkElapsedSeconds=round(time.monotonic() - check_started, 1),
                    candidateUnits=len(candidate),
                    candidatePackages=package_count(candidate),
                )
                return value
            except Exception as exc:
                emit(
                    "check-error",
                    wave=wave,
                    check=check_number,
                    error=f"{type(exc).__name__}: {exc}",
                    checkElapsedSeconds=round(time.monotonic() - check_started, 1),
                )
                raise

        try:
            for index, candidate, check_number in pending:
                future = pool.submit(run_candidate, candidate, check_number)
                future_map[future] = (index, candidate, check_number)

            unfinished = set(future_map)
            last_heartbeat = time.monotonic()
            while unfinished:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    for future in unfinished:
                        future.cancel()
                    emit("timeout", wave=wave, active=len(unfinished), timeoutSeconds=timeout_seconds)
                    raise LocalizationTimeoutError(
                        f"localization exceeded {int(timeout_seconds or 0)}s with {len(unfinished)} check(s) still active"
                    )
                wait_for = progress_interval_seconds
                if deadline is not None:
                    wait_for = max(0.1, min(wait_for, deadline - now))
                done, unfinished = wait(unfinished, timeout=wait_for, return_when=FIRST_COMPLETED)
                if not done:
                    emit("heartbeat", wave=wave, active=len(unfinished), completed=len(future_map) - len(unfinished))
                    last_heartbeat = time.monotonic()
                    continue
                # Consume completed futures in deterministic candidate order.
                ordered_done = sorted(done, key=lambda future: future_map[future][0])
                for future in ordered_done:
                    index, candidate, _check_number = future_map[future]
                    value = bool(future.result())
                    cache[key(candidate)] = value
                    results[index] = value
                if time.monotonic() - last_heartbeat >= progress_interval_seconds:
                    emit("heartbeat", wave=wave, active=len(unfinished), completed=len(future_map) - len(unfinished))
                    last_heartbeat = time.monotonic()
        except Exception:
            for future in future_map:
                future.cancel()
            # Do not let ThreadPoolExecutor.__exit__ turn one failed/hung check
            # into an unbounded wait. Running verifier callbacks have their own
            # hard process/attempt watchdog and will terminate independently.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
            emit("wave-finish", wave=wave, candidates=len(pending), active=0)
        return results

    emit("start", units=len(current), packages=package_count(current), parallelism=parallelism)
    n = 2
    while len(current) >= 2 and checks < max_checks:
        parts = [tuple(part) for part in _partitions(current, n)]
        subset_results = evaluate_many(parts, wave=f"subsets/{n}")
        failing_subset = next((parts[i] for i, value in enumerate(subset_results) if value is True), None)
        if failing_subset is not None:
            current = failing_subset
            emit("shrink", reason="failing-subset", units=len(current), packages=package_count(current))
            n = max(2, n - 1)
            continue

        complements = [tuple(item for item in current if item not in part) for part in parts]
        complements = [candidate for candidate in complements if candidate]
        complement_results = evaluate_many(complements, wave=f"complements/{n}")
        failing_complement = next((complements[i] for i, value in enumerate(complement_results) if value is True), None)
        if failing_complement is not None:
            current = failing_complement
            emit("shrink", reason="failing-complement", units=len(current), packages=package_count(current))
            n = max(2, n - 1)
            continue

        if n >= len(current):
            break
        n = min(len(current), n * 2)

    emit("finish", units=len(current), packages=package_count(current))
    return current

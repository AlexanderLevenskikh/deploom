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
import heapq
import time


Nogood = Dict[str, str]


class LocalizationTimeoutError(RuntimeError):
    """Raised when deterministic conflict localization exceeds its watchdog."""


ProgressCallback = Callable[[str, Mapping[str, object]], None]
CheckpointCallback = Callable[[Mapping[str, object]], None]


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




class GlobalExactExclusionError(RuntimeError):
    """Global exact-assignment coordination stopped with a typed proof reason."""

    def __init__(self, message: str, *, reason: str = "solver-unknown") -> None:
        super().__init__(message)
        self.reason = str(reason or "solver-unknown")


@dataclass(frozen=True)
class RankedComponentAlternative:
    assignment: Mapping[str, str]
    score: Tuple[int, ...]


def coordinate_global_exact_exclusions(
    initial: Sequence[RankedComponentAlternative],
    exclusions: Sequence[Mapping[str, str]],
    next_alternative: Callable[
        [int, Tuple[Mapping[str, str], ...]],
        Optional[RankedComponentAlternative],
    ],
    *,
    max_states: int = 4096,
    progress: Optional[ProgressCallback] = None,
) -> Tuple[Dict[str, str], int]:
    """Solve global exact tuple exclusions without merging solver components.

    Each component owns a ranked stream of exact local assignments. The
    coordinator enumerates the Cartesian product best-first and rejects only
    complete assignments matching a confirmed global exclusion. A local
    component assignment is never forbidden merely because it participated in
    one rejected global tuple.
    """
    if not initial:
        return {}, 0

    component_names: list[Tuple[str, ...]] = []
    score_width: Optional[int] = None
    alternatives: list[list[RankedComponentAlternative]] = []

    seen_names: set[str] = set()
    for index, alternative in enumerate(initial):
        names = tuple(sorted(str(name) for name in alternative.assignment))
        if not names:
            raise ValueError(f"component {index} has no package names")
        overlap = seen_names.intersection(names)
        if overlap:
            raise ValueError(
                "component assignments must be disjoint; overlap="
                + ",".join(sorted(overlap))
            )
        seen_names.update(names)
        if score_width is None:
            score_width = len(alternative.score)
        elif len(alternative.score) != score_width:
            raise ValueError("component score tuples must have the same width")
        component_names.append(names)
        alternatives.append([alternative])

    exact_exclusions = [
        dict(exclusion)
        for exclusion in exclusions
        if exclusion
    ]
    if not exact_exclusions:
        merged: Dict[str, str] = {}
        for alternative in initial:
            merged.update(alternative.assignment)
        return merged, 1

    exhausted: set[int] = set()

    def emit(event: str, **details: object) -> None:
        if progress is not None:
            progress(event, details)

    def ensure(component_index: int, alternative_index: int) -> bool:
        while len(alternatives[component_index]) <= alternative_index:
            if component_index in exhausted:
                return False
            existing = tuple(
                dict(item.assignment)
                for item in alternatives[component_index]
            )
            candidate = next_alternative(component_index, existing)
            if candidate is None:
                exhausted.add(component_index)
                return False

            expected_names = component_names[component_index]
            actual_names = tuple(sorted(str(name) for name in candidate.assignment))
            if actual_names != expected_names:
                raise ValueError(
                    f"component {component_index} alternative changed package identity"
                )
            if score_width is not None and len(candidate.score) != score_width:
                raise ValueError(
                    f"component {component_index} alternative score width changed"
                )
            frozen = tuple(
                (name, str(candidate.assignment[name]))
                for name in expected_names
            )
            if any(
                tuple((name, str(item.assignment[name])) for name in expected_names) == frozen
                for item in alternatives[component_index]
            ):
                raise GlobalExactExclusionError(
                    f"component {component_index} repeated an already ranked assignment",
                    reason="solver-unknown",
                )
            alternatives[component_index].append(candidate)
            emit(
                "component-alternative",
                component=component_index,
                rank=len(alternatives[component_index]) - 1,
            )
        return True

    def compose(state: Tuple[int, ...]) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for component_index, alternative_index in enumerate(state):
            merged.update(
                alternatives[component_index][alternative_index].assignment
            )
        return merged

    def total_score(state: Tuple[int, ...]) -> Tuple[int, ...]:
        width = int(score_width or 0)
        values = [0] * width
        for component_index, alternative_index in enumerate(state):
            score = alternatives[component_index][alternative_index].score
            for score_index, value in enumerate(score):
                values[score_index] += int(value)
        return tuple(values)

    initial_state = tuple(0 for _ in alternatives)
    heap: list[Tuple[Tuple[int, ...], Tuple[int, ...]]] = [
        (tuple(-value for value in total_score(initial_state)), initial_state)
    ]
    queued = {initial_state}
    visited: set[Tuple[int, ...]] = set()
    explored = 0

    while heap:
        _priority, state = heapq.heappop(heap)
        queued.discard(state)
        if state in visited:
            continue
        visited.add(state)
        explored += 1
        if explored > max(1, int(max_states)):
            raise GlobalExactExclusionError(
                f"global exact exclusion coordinator exceeded {max_states} states",
                reason="budget-exhausted",
            )

        assignment = compose(state)
        matched = [
            exclusion
            for exclusion in exact_exclusions
            if assignment_matches_nogood(assignment, exclusion)
        ]
        if not matched:
            emit(
                "accepted",
                exploredStates=explored,
                rankedComponents=len(alternatives),
            )
            return assignment, explored

        emit(
            "rejected-global-exact",
            exploredStates=explored,
            matchedExclusions=len(matched),
        )

        for component_index in range(len(alternatives)):
            next_index = state[component_index] + 1
            if not ensure(component_index, next_index):
                continue
            next_state = list(state)
            next_state[component_index] = next_index
            frozen_state = tuple(next_state)
            if frozen_state in visited or frozen_state in queued:
                continue
            heapq.heappush(
                heap,
                (
                    tuple(-value for value in total_score(frozen_state)),
                    frozen_state,
                ),
            )
            queued.add(frozen_state)

    raise GlobalExactExclusionError(
        "all ranked component combinations were exhausted by global exact exclusions",
        reason="unsat-proven",
    )
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
    confirm_failure: Optional[Callable[[Tuple[VerificationUnit, ...]], bool]] = None,
    resume_state: Optional[Mapping[str, object]] = None,
    checkpoint: Optional[CheckpointCallback] = None,
) -> Tuple[VerificationUnit, ...]:
    """Return a small failure-inducing subset using resumable parallel ddmin.

    Parallel FAIL is screening evidence only when ``confirm_failure`` is set.
    It may be cached across restarts, but it still cannot shrink the search
    until the same candidate fails again in an isolated serial confirmation.

    The checkpoint stores execution evidence, not solver authority.  A resumed
    invocation keeps current subset/cache/check budget, while wall-clock timeout
    starts fresh for the new process.
    """
    initial = tuple(units)
    if len(initial) <= 1 or max_checks <= 0:
        return initial

    unit_by_id = {item.id: item for item in initial}
    initial_ids = tuple(item.id for item in initial)
    if len(unit_by_id) != len(initial_ids):
        raise ValueError("VerificationUnit ids must be unique")

    current = initial
    cache: Dict[Tuple[str, ...], bool] = {}
    confirmed_failure_keys: set[Tuple[str, ...]] = set()
    checks = 0
    n = 2
    resumed_finished = False

    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    progress_interval_seconds = max(1.0, float(progress_interval_seconds or 15.0))

    def key(candidate: Sequence[VerificationUnit]) -> Tuple[str, ...]:
        return tuple(sorted(item.id for item in candidate))

    def package_count(candidate: Sequence[VerificationUnit]) -> int:
        return len({name for item in candidate for name in item.packages})

    def emit(event: str, **details: object) -> None:
        if progress is None:
            return
        progress(event, {
            "elapsedSeconds": round(time.monotonic() - started, 1),
            "checksStarted": checks,
            "maxChecks": max_checks,
            "currentUnits": len(current),
            **details,
        })

    def state_payload(reason: str, *, finished: bool = False) -> Dict[str, object]:
        entries = []
        for candidate_key in sorted(cache):
            entries.append({
                "unitIds": list(candidate_key),
                "failed": bool(cache[candidate_key]),
                "confirmedFailure": candidate_key in confirmed_failure_keys,
            })
        return {
            "schemaVersion": 1,
            "reason": reason,
            "initialUnitIds": list(initial_ids),
            "currentUnitIds": [item.id for item in current],
            "granularity": n,
            "checksStarted": checks,
            "cache": entries,
            "finished": finished,
        }

    def persist(reason: str, *, finished: bool = False) -> None:
        if checkpoint is None:
            return
        try:
            checkpoint(state_payload(reason, finished=finished))
        except Exception as exc:
            # Recovery cache failure must never change the solver result.
            emit("checkpoint-error", reason=reason, error=f"{type(exc).__name__}: {exc}")

    if resume_state is not None and int(resume_state.get("schemaVersion") or 0) == 1:
        raw_initial = resume_state.get("initialUnitIds")
        state_initial = tuple(str(item) for item in raw_initial) if isinstance(raw_initial, list) else ()
        if state_initial == initial_ids:
            raw_current = resume_state.get("currentUnitIds")
            current_ids = tuple(str(item) for item in raw_current) if isinstance(raw_current, list) else ()
            if (
                current_ids
                and len(set(current_ids)) == len(current_ids)
                and all(item_id in unit_by_id for item_id in current_ids)
            ):
                current = tuple(unit_by_id[item_id] for item_id in current_ids)
                try:
                    n = max(2, min(len(current), int(resume_state.get("granularity") or 2)))
                except (TypeError, ValueError):
                    n = 2
                try:
                    checks = max(0, min(max_checks, int(resume_state.get("checksStarted") or 0)))
                except (TypeError, ValueError):
                    checks = 0

                raw_cache = resume_state.get("cache")
                if isinstance(raw_cache, list):
                    for entry in raw_cache:
                        if not isinstance(entry, dict):
                            continue
                        raw_ids = entry.get("unitIds")
                        if not isinstance(raw_ids, list):
                            continue
                        ids = tuple(sorted(str(item) for item in raw_ids))
                        if not ids or any(item_id not in unit_by_id for item_id in ids):
                            continue
                        failed = bool(entry.get("failed"))
                        cache[ids] = failed
                        if failed and bool(entry.get("confirmedFailure")):
                            confirmed_failure_keys.add(ids)

                resumed_finished = bool(resume_state.get("finished"))
                emit(
                    "resume",
                    resumedUnits=len(current),
                    resumedPackages=package_count(current),
                    resumedChecks=checks,
                    cachedResults=len(cache),
                    confirmedFailures=len(confirmed_failure_keys),
                    finished=resumed_finished,
                )

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

        persist("wave-start")
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
                    persist("timeout")
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

                for future in sorted(done, key=lambda item: future_map[item][0]):
                    index, candidate, _check_number = future_map[future]
                    value = bool(future.result())
                    cache[key(candidate)] = value
                    results[index] = value
                    persist("check-finish")

                if time.monotonic() - last_heartbeat >= progress_interval_seconds:
                    emit("heartbeat", wave=wave, active=len(unfinished), completed=len(future_map) - len(unfinished))
                    last_heartbeat = time.monotonic()
        except Exception:
            for future in future_map:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
            emit("wave-finish", wave=wave, candidates=len(pending), active=0)
        return results

    def first_confirmed_failure(
        candidates: Sequence[Tuple[VerificationUnit, ...]],
        results: Sequence[bool | None],
        *,
        wave: str,
    ) -> Optional[Tuple[VerificationUnit, ...]]:
        for index, value in enumerate(results):
            if value is not True:
                continue
            candidate = candidates[index]
            candidate_key = key(candidate)
            if confirm_failure is None or candidate_key in confirmed_failure_keys:
                return candidate

            confirmation_started = time.monotonic()
            emit(
                "confirmation-start",
                wave=wave,
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )

            # Strictly one proof worker. Coordinator remains free to emit output.
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(confirm_failure, candidate)
            try:
                while not future.done():
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        future.cancel()
                        emit(
                            "timeout",
                            wave=wave,
                            phase="confirmation",
                            candidateUnits=len(candidate),
                            candidatePackages=package_count(candidate),
                            timeoutSeconds=timeout_seconds,
                        )
                        persist("confirmation-timeout")
                        raise LocalizationTimeoutError(
                            f"localization exceeded {int(timeout_seconds or 0)}s during serial confirmation"
                        )

                    wait_for = progress_interval_seconds
                    if deadline is not None:
                        wait_for = max(0.1, min(wait_for, deadline - now))
                    done, _ = wait({future}, timeout=wait_for, return_when=FIRST_COMPLETED)
                    if not done:
                        emit(
                            "confirmation-heartbeat",
                            wave=wave,
                            confirmationElapsedSeconds=round(time.monotonic() - confirmation_started, 1),
                            candidateUnits=len(candidate),
                            candidatePackages=package_count(candidate),
                        )

                confirmed = bool(future.result())
            except Exception:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

            emit(
                "confirmation-finish",
                wave=wave,
                confirmed=confirmed,
                confirmationElapsedSeconds=round(time.monotonic() - confirmation_started, 1),
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )

            cache[candidate_key] = confirmed
            if confirmed:
                confirmed_failure_keys.add(candidate_key)
            else:
                confirmed_failure_keys.discard(candidate_key)
            persist("confirmation-finish")

            if confirmed:
                return candidate
        return None

    emit("start", units=len(current), packages=package_count(current), parallelism=parallelism)
    if resumed_finished:
        emit("finish", units=len(current), packages=package_count(current), resumed=True)
        return current

    while len(current) >= 2 and checks < max_checks:
        parts = [tuple(part) for part in _partitions(current, n)]
        subset_wave = f"subsets/{n}"
        subset_results = evaluate_many(parts, wave=subset_wave)
        failing_subset = first_confirmed_failure(parts, subset_results, wave=subset_wave)
        if failing_subset is not None:
            current = failing_subset
            n = max(2, n - 1)
            emit("shrink", reason="failing-subset", units=len(current), packages=package_count(current))
            persist("shrink")
            continue

        complements = [tuple(item for item in current if item not in part) for part in parts]
        complements = [candidate for candidate in complements if candidate]
        complement_wave = f"complements/{n}"
        complement_results = evaluate_many(complements, wave=complement_wave)
        failing_complement = first_confirmed_failure(complements, complement_results, wave=complement_wave)
        if failing_complement is not None:
            current = failing_complement
            n = max(2, n - 1)
            emit("shrink", reason="failing-complement", units=len(current), packages=package_count(current))
            persist("shrink")
            continue

        if n >= len(current):
            break
        n = min(len(current), n * 2)
        persist("granularity")

    emit("finish", units=len(current), packages=package_count(current))
    persist("finish", finished=True)
    return current


#!/usr/bin/env python3
"""Durable diagnostic state for Block V-F predicate-guided active search.

This store is deliberately non-authoritative.  It remembers exact point
observations, bounded probe attempts, and an optional soft version preference so
an interrupted Baseline does not restart diagnostic exploration from zero.
Every entry is bound to the exact Baseline run identity.  Losing or corrupting
this file can only cost time; it can never create a solver clause or proof.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Mapping, Optional, Sequence

from block_v_predicate_search import PredicateObservation

PREDICATE_STATE_SCHEMA = 1
PREDICATE_STATE_AUTHORITY = "DIAGNOSTIC_HINT"
PREDICATE_POINT_AUTHORITY = "POINT_EVIDENCE"


@dataclasses.dataclass(frozen=True)
class PredicateSearchSession:
    project: str
    mode: str
    run_identity: str
    package: str
    predicate: str
    observations: tuple[PredicateObservation, ...] = ()
    attempted_versions: tuple[str, ...] = ()
    preferred_version: str = ""


class PredicateSearchStateStore:
    """Atomic latest diagnostic state keyed by exact run identity.

    No read from this store is proof authority.  Callers must still obtain all
    authoritative Resolver/Project/Constraint evidence through the existing
    proof pipeline.
    """

    def __init__(self, progress_path: Optional[Path]) -> None:
        self.path = (
            progress_path.with_name("baseline-predicate-search.json")
            if progress_path is not None
            else None
        )
        self._lock = threading.RLock()

    @staticmethod
    def _slot(
        project: str,
        mode: str,
        run_identity: str,
        package: str,
        predicate: str,
    ) -> str:
        payload = "\0".join(
            (str(project), str(mode), str(run_identity), str(package).lower(), str(predicate))
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def _empty(self) -> dict[str, object]:
        return {"schemaVersion": PREDICATE_STATE_SCHEMA, "sessions": {}}

    def _read_locked(self) -> dict[str, object]:
        if self.path is None or not self.path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            # Fail open. Diagnostic state loss cannot change correctness.
            return self._empty()
        if (
            not isinstance(payload, dict)
            or int(payload.get("schemaVersion") or 0) != PREDICATE_STATE_SCHEMA
        ):
            return self._empty()
        if not isinstance(payload.get("sessions"), dict):
            payload["sessions"] = {}
        return payload

    def _write_locked(self, payload: Mapping[str, object]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, self.path)

    @staticmethod
    def _observation_to_json(item: PredicateObservation) -> dict[str, object]:
        return {
            "package": item.package,
            "version": item.version,
            "predicate": item.predicate,
            "present": bool(item.present),
            "assignmentFingerprint": item.assignment_fingerprint,
            "otherPredicates": list(item.other_predicates),
            "authority": PREDICATE_POINT_AUTHORITY,
        }

    @staticmethod
    def _observation_from_json(item: object) -> Optional[PredicateObservation]:
        if not isinstance(item, dict):
            return None
        package = str(item.get("package") or "").strip()
        version = str(item.get("version") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        if not package or not version or not predicate:
            return None
        other = item.get("otherPredicates")
        other_predicates = tuple(
            sorted({str(value) for value in other if str(value)})
        ) if isinstance(other, list) else ()
        return PredicateObservation(
            package=package,
            version=version,
            predicate=predicate,
            present=bool(item.get("present")),
            assignment_fingerprint=str(item.get("assignmentFingerprint") or ""),
            other_predicates=other_predicates,
        )

    def load_session(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
    ) -> PredicateSearchSession:
        empty = PredicateSearchSession(
            project=str(project),
            mode=str(mode),
            run_identity=str(run_identity),
            package=str(package),
            predicate=str(predicate),
        )
        if self.path is None:
            return empty
        slot = self._slot(project, mode, run_identity, package, predicate)
        with self._lock:
            payload = self._read_locked()
            sessions = payload.get("sessions")
            raw = sessions.get(slot) if isinstance(sessions, dict) else None
        if not isinstance(raw, dict):
            return empty
        # Redundant identity fields make accidental slot/key bugs fail open.
        if (
            str(raw.get("project") or "") != str(project)
            or str(raw.get("mode") or "") != str(mode)
            or str(raw.get("runIdentity") or "") != str(run_identity)
            or str(raw.get("package") or "").lower() != str(package).lower()
            or str(raw.get("predicate") or "") != str(predicate)
        ):
            return empty
        observations_raw = raw.get("observations")
        observations: list[PredicateObservation] = []
        if isinstance(observations_raw, list):
            for item in observations_raw:
                parsed = self._observation_from_json(item)
                if parsed is not None and parsed not in observations:
                    observations.append(parsed)
        attempted_raw = raw.get("attemptedVersions")
        attempted = tuple(sorted({
            str(item) for item in attempted_raw if str(item)
        })) if isinstance(attempted_raw, list) else ()
        return dataclasses.replace(
            empty,
            observations=tuple(observations),
            attempted_versions=attempted,
            preferred_version=str(raw.get("preferredVersion") or ""),
        )

    def _mutate_session(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
        observations: Optional[Sequence[PredicateObservation]] = None,
        add_attempt: str = "",
        preferred_version: Optional[str] = None,
    ) -> None:
        if self.path is None:
            return
        slot = self._slot(project, mode, run_identity, package, predicate)
        with self._lock:
            payload = self._read_locked()
            sessions = payload.setdefault("sessions", {})
            assert isinstance(sessions, dict)
            existing = sessions.get(slot)
            if not isinstance(existing, dict):
                existing = {
                    "project": str(project),
                    "mode": str(mode),
                    "runIdentity": str(run_identity),
                    "package": str(package),
                    "predicate": str(predicate),
                    "authority": PREDICATE_STATE_AUTHORITY,
                    "observations": [],
                    "attemptedVersions": [],
                    "preferredVersion": "",
                }
            if observations is not None:
                values: list[dict[str, object]] = []
                seen: set[str] = set()
                for item in observations:
                    encoded = self._observation_to_json(item)
                    key = json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if key in seen:
                        continue
                    seen.add(key)
                    values.append(encoded)
                existing["observations"] = values
            attempted = {
                str(item)
                for item in existing.get("attemptedVersions", [])
                if str(item)
            } if isinstance(existing.get("attemptedVersions"), list) else set()
            if add_attempt:
                attempted.add(str(add_attempt))
            existing["attemptedVersions"] = sorted(attempted)
            if preferred_version is not None:
                existing["preferredVersion"] = str(preferred_version)
            existing["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
            sessions[slot] = existing
            self._write_locked(payload)

    def save_observations(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
        observations: Sequence[PredicateObservation],
    ) -> None:
        self._mutate_session(
            project,
            mode,
            run_identity=run_identity,
            package=package,
            predicate=predicate,
            observations=observations,
        )

    def mark_attempt(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
        version: str,
    ) -> None:
        self._mutate_session(
            project,
            mode,
            run_identity=run_identity,
            package=package,
            predicate=predicate,
            add_attempt=version,
        )

    def set_preferred_version(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
        version: str,
    ) -> None:
        self._mutate_session(
            project,
            mode,
            run_identity=run_identity,
            package=package,
            predicate=predicate,
            preferred_version=version,
        )

    def clear_preferred_version(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
        package: str,
        predicate: str,
    ) -> None:
        self._mutate_session(
            project,
            mode,
            run_identity=run_identity,
            package=package,
            predicate=predicate,
            preferred_version="",
        )

    def preferred_versions(
        self,
        project: str,
        mode: str,
        *,
        run_identity: str,
    ) -> dict[str, str]:
        """Return only unambiguous package preferences for this exact run.

        Multiple predicates may exist for one package. If they disagree about a
        preferred version, no diagnostic preference is returned for that package.
        """
        if self.path is None:
            return {}
        with self._lock:
            payload = self._read_locked()
            sessions = payload.get("sessions")
            values = list(sessions.values()) if isinstance(sessions, dict) else []
        by_package: dict[str, set[str]] = {}
        canonical_name: dict[str, str] = {}
        for raw in values:
            if not isinstance(raw, dict):
                continue
            if (
                str(raw.get("project") or "") != str(project)
                or str(raw.get("mode") or "") != str(mode)
                or str(raw.get("runIdentity") or "") != str(run_identity)
            ):
                continue
            package = str(raw.get("package") or "").strip()
            preferred = str(raw.get("preferredVersion") or "").strip()
            if not package or not preferred:
                continue
            key = package.lower()
            canonical_name.setdefault(key, package)
            by_package.setdefault(key, set()).add(preferred)
        return {
            canonical_name[key]: next(iter(versions))
            for key, versions in by_package.items()
            if len(versions) == 1
        }

    def clear_run(self, project: str, mode: str) -> None:
        """Clear all diagnostic sessions for project/mode, regardless of identity."""
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            sessions = payload.get("sessions")
            if not isinstance(sessions, dict):
                return
            doomed = [
                key
                for key, raw in sessions.items()
                if isinstance(raw, dict)
                and str(raw.get("project") or "") == str(project)
                and str(raw.get("mode") or "") == str(mode)
            ]
            for key in doomed:
                sessions.pop(key, None)
            if doomed:
                self._write_locked(payload)

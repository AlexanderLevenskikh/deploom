from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolved_dependency_state import (
    ResolvedDependencyState,
    ResolvedDependencyStateError,
    assert_resolved_dependency_state,
    capture_resolved_dependency_state,
    load_resolved_dependency_state,
    resolved_state_metadata,
    restore_resolved_dependency_state,
)


class ResolvedDependencyStateTests(unittest.TestCase):
    def test_capture_persist_restore_and_detect_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            cache = root / "cache"
            project.mkdir()
            (project / "package.json").write_text(
                json.dumps({"packageManager": "yarn@1.22.22", "dependencies": {"a": "1.0.0"}}),
                encoding="utf-8",
            )
            original = b'a@1.0.0:\n  version "1.0.0"\n'
            (project / "yarn.lock").write_bytes(original)
            observed_hash = hashlib.sha256(b"observed").hexdigest()
            state = capture_resolved_dependency_state(
                project,
                manager="yarn",
                resolver_input_key="a" * 32,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=cache,
            )
            self.assertEqual(hashlib.sha256(original).hexdigest(), state.lockfile_hash)
            loaded = load_resolved_dependency_state(
                resolved_state_metadata(state),
                proof_cache_dir=cache,
            )
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(state.key, loaded.key)

            (project / "yarn.lock").write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(ResolvedDependencyStateError, "DRIFT"):
                assert_resolved_dependency_state(project, loaded)
            restore_resolved_dependency_state(project, loaded)
            self.assertEqual(original, (project / "yarn.lock").read_bytes())
            assert_resolved_dependency_state(project, loaded)

    def test_tampered_content_addressed_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            cache = root / "cache"
            project.mkdir()
            (project / "package.json").write_text('{"packageManager":"npm@11.0.0"}\n', encoding="utf-8")
            (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            state = capture_resolved_dependency_state(
                project,
                manager="npm",
                resolver_input_key="b" * 32,
                observed_resolved_hash="c" * 64,
                proof_cache_dir=cache,
            )
            artifact = cache / state.artifact_relative_path
            artifact.write_text("tampered\n", encoding="utf-8")
            self.assertIsNone(load_resolved_dependency_state(
                resolved_state_metadata(state),
                proof_cache_dir=cache,
            ))

    def test_shared_lockfile_restore_is_mutually_excluded(self) -> None:
        """Identity binds content, but a shared lockfile needs writer exclusion.

        One root lockfile can be restored by many verification attempts of
        different monorepo packages. The resolved-state key proves which bytes
        are correct; it does not by itself serialize writers. Concurrent
        restores of the SAME pathname must be atomic critical sections
        (write+verify), never interleaved, and the surviving file must be one
        complete state rather than a mixture.
        """
        import resolved_dependency_state as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "monorepo"
            project.mkdir()
            (project / "package.json").write_text(
                json.dumps({"packageManager": "yarn@1.22.22", "private": True}),
                encoding="utf-8",
            )
            # Shared lockfile: every state addresses the same pathname but
            # carries different exact bytes (as different resolved sub-projects
            # would).
            states = []
            for index in range(8):
                payload = (
                    f"# resolved by verification attempt {index}\n"
                    f'a@1.0.0:\n  version "1.0.{index}"\n'
                ).encode("utf-8")
                states.append(ResolvedDependencyState(
                    key=f"{index:064d}",
                    resolver_input_key="b" * 32,
                    manager="yarn",
                    lockfile_path="yarn.lock",
                    lockfile_hash=hashlib.sha256(payload).hexdigest(),
                    observed_resolved_hash="c" * 64,
                    artifact_relative_path="",
                    lockfile_bytes=payload,
                ))

            intervals: list[tuple[float, float, str]] = []
            intervals_guard = threading.Lock()
            real_replace = module.os.replace

            def recorded_replace(source, destination):
                with intervals_guard:
                    started = time.perf_counter()
                # Widen the race window so an unlocked build reliably interleaves.
                time.sleep(0.002)
                real_replace(source, destination)
                with intervals_guard:
                    hash_value = hashlib.sha256(
                        Path(destination).read_bytes()
                    ).hexdigest()
                    intervals.append(
                        (started, time.perf_counter(), hash_value[:12])
                    )

            barrier = threading.Barrier(len(states))
            errors: list[BaseException] = []
            errors_guard = threading.Lock()

            def worker(i: int) -> None:
                try:
                    barrier.wait(timeout=10)
                    restore_resolved_dependency_state(project, states[i])
                except BaseException as exc:  # noqa: BLE001 - collected and re-raised
                    with errors_guard:
                        errors.append(exc)

            with mock.patch.object(module.os, "replace", recorded_replace):
                threads = [
                    threading.Thread(target=worker, args=(i,), name=f"restore-{i}")
                    for i in range(len(states))
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

            self.assertEqual([], [str(exc) for exc in errors])

            timings = sorted(intervals, key=lambda item: item[0])
            # The write+replace of every restore must be a non-overlapping
            # critical section: no two restores of the shared pathname
            # interleave.
            for previous, current in zip(timings, timings[1:]):
                self.assertLessEqual(
                    previous[1], current[0],
                    "shared lockfile restores interleaved instead of serializing",
                )

            final_bytes = (project / "yarn.lock").read_bytes()
            winners = [
                state for state in states if state.lockfile_bytes == final_bytes
            ]
            self.assertEqual(1, len(winners),
                             "shared lockfile must end as exactly one complete state")
            assert_resolved_dependency_state(project, winners[0])
            losers = [state for state in states if state is not winners[0]]
            for state in losers:
                with self.assertRaisesRegex(
                    ResolvedDependencyStateError, "DRIFT"
                ):
                    assert_resolved_dependency_state(project, state)


if __name__ == "__main__":
    unittest.main()

"""C4 acceptance: measured cold vs warm prepared-workspace materialization.

The reviewer asked for cold/warm measurements instead of an asserted claim.
This test builds controlled-size project fixtures, publishes a single prepared
snapshot per fixture, then measures the two real consumer paths against the
SAME snapshot:

- cold: `_materialize_prepared_workspace_snapshot(allow_fastpath=False)` — a
  full proof-safe copy of every dependency payload byte;
- warm: the Ω guarded-clone fast path (`allow_fastpath=True`) — the private
  upper is copied, dependency roots are exposed as junctions under a watcher,
  and ZERO dependency payload bytes are copied or hashed;
- seal: `build_dependency_integrity_manifest` — the legacy O(files) per-file
  hash tax that the Ω guarded lower deliberately removed from the fast path.

The honest claim this test establishes is ASYMPTOTIC, and it does so
structurally (deterministic, no timing gates): the guarded path costs
O(number of dependency packages) via one junction per package, while the cold
copy and the legacy seal cost O(number of dependency files). Two fixtures keep
the SAME package count (30) but grow the payload 90 -> 1200 files; cold/seal
must carry all 1200, warm still copies zero bytes and creates the same 30
junctions. Wall-clock numbers are recorded and reported with only very loose
sanity bounds, because CI timing is not a reliable claim by itself.

Windows-only: the guarded lower uses NTFS junctions + ReadDirectoryChangesW.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import prepared_workspace_fastpath as fastpath

BASE_PACKAGES = 30


def _write(tree: Path, relative: str, text: str) -> None:
    path = tree.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _measurement_fixture(prepared: Path, files_per_package: int) -> dict[str, int]:
    _write(prepared, "package.json", '{"name":"fastpath-bench","private":true}\n')
    _write(prepared, "source.txt", "sealed-source\n")
    payload = 0
    for index in range(BASE_PACKAGES):
        name = f"pkg{index:03d}"
        package = prepared / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
        )
        (package / "index.js").write_text(
            f"module.exports = {index};\n", encoding="utf-8"
        )
        payload += 2
        for file_index in range(files_per_package):
            (package / f"chunk{file_index:03d}.js").write_text(
                f"export const chunk = {index * 1000 + file_index};\n",
                encoding="utf-8",
            )
            payload += 1
    scoped = prepared / "node_modules" / "@acme" / "wrapper"
    scoped.mkdir(parents=True)
    (scoped / "package.json").write_text(
        '{"name":"@acme/wrapper","version":"1.0.0"}', encoding="utf-8"
    )
    (scoped / "index.js").write_text("module.exports = 'wrapper';\n", encoding="utf-8")
    payload += 2
    return {
        "payload": payload,
        "total": payload + 2,  # package.json + source.txt
        "junctions": BASE_PACKAGES + 1,  # pkg* + @acme/child
    }


def _count_files(path: Path) -> int:
    total = 0
    for _current, _directories, names in os.walk(path):
        total += len(names)
    return total


def _validate_warm_zero_copy(warm: Path, snapshot: Path, counts: dict[str, int]) -> int:
    """Return the number of junctions the guarded clone created."""
    snap_node_modules = snapshot.resolve() / "node_modules"
    warm_node_modules = warm.resolve() / "node_modules"
    copied_top_level_files = 0
    junctions = 0
    for entry in os.scandir(warm_node_modules):
        if entry.is_file():
            copied_top_level_files += 1
    assert copied_top_level_files == 0, "warm path copied a top-level dependency file"
    for entry in sorted(snap_node_modules.iterdir()):
        alias = warm_node_modules / entry.name
        assert alias.exists(), entry.name
        if entry.name.startswith("@"):
            assert not alias.samefile(entry), "scope directory must stay a private shell"
            for child in sorted(entry.iterdir()):
                assert (alias / child.name).samefile(child), \
                    f"{entry.name}/{child.name} must be a junction"
                junctions += 1
        else:
            assert alias.samefile(entry), f"{entry.name} must be a junction"
            junctions += 1
    assert junctions == counts["junctions"], \
        f"junction count {junctions} != expected {counts['junctions']}"
    return junctions


@unittest.skipUnless(os.name == "nt", "guarded lower fast path is Windows-only")
class PreparedWorkspaceColdWarmMeasurements(unittest.TestCase):
    def test_guarded_path_is_flat_in_payload_files_while_cold_and_seal_are_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sizes = (3, 40)  # 90 vs 1200 payload files over the same 30 packages
            results: dict[str, object] = {}

            for files_per_package in sizes:
                label = f"n{files_per_package}"
                prepared = root / f"prepared-{label}"
                counts = _measurement_fixture(prepared, files_per_package)
                snapshot = verifier.PreparedWorkspaceSnapshot(
                    key=uuid.uuid4().hex,
                    workspace_root=prepared,
                    project_relative=Path("."),
                    source_project=prepared,
                    storage_mode="test",
                    observed_resolved_versions={},
                    observed_resolved_hash="c" * 64,
                    dependency_roots=("node_modules",),
                )

                cold_started = time.perf_counter()
                cold = verifier._materialize_prepared_workspace_snapshot(
                    snapshot, root / f"cold-{label}", allow_fastpath=False,
                )
                cold_ms = (time.perf_counter() - cold_started) * 1000
                self.assertEqual(counts["total"], _count_files(cold),
                                 "cold path must copy every source and dependency file")

                warm_started = time.perf_counter()
                warm = verifier._materialize_prepared_workspace_snapshot(
                    snapshot, root / f"warm-{label}", allow_fastpath=True,
                )
                warm_ms = (time.perf_counter() - warm_started) * 1000
                self.assertTrue(
                    fastpath.guarded_clone_is_active(root / f"warm-{label}"),
                    "warm measurement used the full-copy fallback, not the guarded clone",
                )
                junctions = _validate_warm_zero_copy(
                    warm, snapshot.workspace_root, counts
                )

                seal_started = time.perf_counter()
                legacy_manifest = fastpath.build_dependency_integrity_manifest(
                    snapshot.workspace_root
                )
                seal_ms = (time.perf_counter() - seal_started) * 1000
                self.assertEqual(counts["payload"], len(legacy_manifest))

                guard_result = fastpath.stop_guarded_clone(root / f"warm-{label}")
                self.assertEqual((), guard_result.errors, guard_result.errors)
                self.assertEqual((), guard_result.mutations, guard_result.mutations)
                fastpath.cleanup_guarded_clone(root / f"warm-{label}")

                self.assertLess(cold_ms, 120_000, "cold full copy far too slow")
                self.assertLess(warm_ms, 30_000, "guarded warm path far too slow")
                results[label] = {
                    "coldMs": round(cold_ms, 1),
                    "warmMs": round(warm_ms, 1),
                    "sealMs": round(seal_ms, 1),
                    "payloadFiles": counts["payload"],
                    "coldFiles": _count_files(cold),
                    "junctions": junctions,
                    "warmZeroCopy": True,
                }

            # Structural scaling proof (deterministic): keeping the package
            # count constant while growing payload 90 -> 1200 files must keep
            # the guarded path at one junction per package and zero copies,
            # while cold must carry every one of the 1200 files.
            small, large = results["n3"], results["n40"]
            self.assertLess(small["payloadFiles"], large["payloadFiles"])
            self.assertEqual(30 + 1, small["junctions"])
            self.assertEqual(30 + 1, large["junctions"],
                             "junction count must scale with packages, not files")
            self.assertTrue(small["warmZeroCopy"] and large["warmZeroCopy"])
            self.assertEqual(small["payloadFiles"], small["coldFiles"] - 2)
            self.assertEqual(large["payloadFiles"], large["coldFiles"] - 2)

            self.measurement = results
            print("[C4-measurement] " + json.dumps(copy.deepcopy(results)))


if __name__ == "__main__":
    unittest.main()

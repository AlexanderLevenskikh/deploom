"""ResolutionInventory: parse-once, hash-bound lockfile readings (C1) and
workspace-aware installed-version resolution (A1).

One phase parses the canonical lockfile exactly ONCE and every ``_installed_version``
probe reads it from memory — a lookup must never re-read the lockfile, and a
lockfile change invalidates the phase (a NEW inventory is built, never stale
bytes). A workspace MEMBER's direct dependency is recorded under
``<member>/node_modules/<name>`` in the SHARED root lockfile (lockfileVersion 3);
the member-local key is consulted first, so a member's own resolution
(different from the root's hoisted copy) is what the never-downgrade guard
compares against. Under a legacy ``dependencies`` map a member cannot prove its
own resolution from the root map and abstains (None), never guessing.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from iterative_migration import _ACTIVE_INVENTORY, _discover_targets, _installed_version, manifest_and_lock_hashes
from resolution_inventory import ResolutionInventory


class ResolutionInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="resolution-inventory-"))

    def tearDown(self) -> None:
        _ACTIVE_INVENTORY.reset(_ACTIVE_INVENTORY.set(None))
        _ACTIVE_INVENTORY.set(None)

    def _write_workspace(self) -> Path:
        """root (workspaces [packages/*]) + app/lib members. The root lockfile
        records the ROOT's sample@8.0.0 AND the member app's own sample@8.5.0
        under its member-local key, plus a `link:` entry for the local lib."""
        root = self._tmp / "ws"
        (root / "packages" / "app").mkdir(parents=True, exist_ok=True)
        (root / "packages" / "lib").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "private": True,
                    "workspaces": ["packages/*"],
                    "dependencies": {"sample": "^8.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (root / "packages" / "app" / "package.json").write_text(
            json.dumps(
                {
                    "name": "app", "version": "1.0.0",
                    "dependencies": {"lib": "^1.0.0", "sample": "^8.1.0"},
                }
            ),
            encoding="utf-8",
        )
        (root / "packages" / "lib" / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"}),
            encoding="utf-8",
        )
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                    "packages": {
                        "": {"name": "root", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}},
                        "node_modules/sample": {"version": "8.0.0"},
                        "packages/app": {
                            "name": "app", "version": "1.0.0",
                            "dependencies": {"lib": "^1.0.0", "sample": "^8.1.0"},
                        },
                        "packages/app/node_modules/sample": {"version": "8.5.0"},
                        "packages/app/node_modules/lib": {"link": True, "resolved": "packages/lib"},
                        "packages/lib": {"name": "lib", "version": "1.2.0"},
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_workspace_member_resolves_from_shared_root_member_local_record(self) -> None:
        root = self._write_workspace()
        app = root / "packages" / "app"
        # A1: `_installed_version(member, ...)` used to abstain — no LOCAL
        # lockfile in the member directory. It must now read the SHARED root
        # lockfile and prefer the member-local record (8.5.0), NOT the root's
        # hoisted 8.0.0.
        self.assertEqual(_installed_version(app, "sample"), "8.5.0")
        # The root's own direct dependency keeps the root-local record.
        self.assertEqual(_installed_version(root, "sample"), "8.0.0")
        # A `link:` workspace dependency resolves through the linked record.
        self.assertEqual(_installed_version(app, "lib"), "1.2.0")

    def test_inventory_reports_member_relative_path(self) -> None:
        root = self._write_workspace()
        inventory = ResolutionInventory.for_project(root / "packages" / "app")
        self.assertIsNotNone(inventory)
        assert inventory is not None
        self.assertEqual(inventory.lock_path.resolve(), (root / "package-lock.json").resolve())
        self.assertEqual(inventory.installed_version("sample", "^8.1.0"), "8.5.0")
        self.assertEqual(inventory.installed_version("absent"), None)

    def test_legacy_member_abstains_but_root_resolves(self) -> None:
        # lockfileVersion 1 has no `packages`: the legacy dependencies map only
        # records the ROOT's resolution, so a member cannot prove its own and
        # must abstain — never guess the root's version as the member's.
        root = self._tmp / "legacy-ws"
        (root / "packages" / "app").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "private": True,
                    "workspaces": ["packages/*"],
                    "dependencies": {"sample": "^8.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (root / "packages" / "app" / "package.json").write_text(
            json.dumps({"name": "app", "version": "1.0.0", "dependencies": {"sample": "^8.1.0"}}),
            encoding="utf-8",
        )
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "lockfileVersion": 1, "requires": True,
                    "dependencies": {"sample": {"version": "8.0.0"}},
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(_installed_version(root / "packages" / "app", "sample"))
        self.assertEqual(_installed_version(root, "sample"), "8.0.0")

    def test_inventory_is_hash_bound_and_parsed_once(self) -> None:
        project = self._tmp / "parse-once"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}}),
            encoding="utf-8",
        )
        lock_path = project / "package-lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "name": "x", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "x", "version": "1.0.0"},
                        "node_modules/sample": {"version": "8.5.0"},
                    },
                }
            ),
            encoding="utf-8",
        )
        original_bytes = lock_path.read_bytes()
        inventory = ResolutionInventory.for_project(project)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        self.assertEqual(inventory.installed_version("sample"), "8.5.0")
        original_hash = inventory.lock_hash
        # Rewrite the lockfile underneath: the inventory must keep serving the
        # version it was built from (never a re-read) ...
        lock_path.write_text(
            json.dumps(
                {
                    "name": "x", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "x", "version": "1.0.0"},
                        "node_modules/sample": {"version": "9.0.0"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(inventory.installed_version("sample"), "8.5.0")
        self.assertEqual(inventory.lock_hash, original_hash)
        # ... while a fresh inventory reflects the changed bytes (the phase is
        # invalidated by the change, never served stale resolutions).
        fresh = ResolutionInventory.for_project(project)
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertEqual(fresh.installed_version("sample"), "9.0.0")
        self.assertNotEqual(fresh.lock_hash, original_hash)
        self.assertEqual(fresh.lock_hash, hashlib.sha256(lock_path.read_bytes()).hexdigest())
        self.assertEqual(original_hash, hashlib.sha256(original_bytes).hexdigest())

    def test_belongs_to_distinguishes_projects(self) -> None:
        root = self._write_workspace()
        inventory = ResolutionInventory.for_project(root)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        self.assertTrue(inventory.belongs_to(root))
        self.assertTrue(inventory.belongs_to(str(root)))
        self.assertFalse(inventory.belongs_to(root / "packages" / "app"))
        self.assertFalse(inventory.belongs_to(self._tmp))
        self.assertFalse(inventory.belongs_to(root / "does-not-exist"))

    def test_yarn_berry_and_classic_through_inventory(self) -> None:
        # Berry mapping via the declared selector.
        berry = self._tmp / "berry"
        berry.mkdir(parents=True, exist_ok=True)
        (berry / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}}),
            encoding="utf-8",
        )
        (berry / "yarn.lock").write_text(
            "__metadata:\n"
            "  version: 6\n"
            "  cacheKey: 8\n"
            "\n"
            '"sample@npm:^8.0.0":\n'
            "  version: 8.5.0\n"
            "  resolution: \"sample@npm:8.5.0\"\n"
            "  dependencies: {}\n",
            encoding="utf-8",
        )
        self.assertEqual(_installed_version(berry, "sample", "^8.0.0"), "8.5.0")
        self.assertEqual(_installed_version(berry, "sample"), "8.5.0")

        # Classic direct-selector behavior keeps working through the inventory.
        classic = self._tmp / "classic"
        classic.mkdir(parents=True, exist_ok=True)
        (classic / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}}),
            encoding="utf-8",
        )
        (classic / "yarn.lock").write_text(
            "# yarn lockfile v1\n"
            '"sample@^7.0.0":\n'
            '  version "7.2.0"\n'
            '"sample@^8.0.0":\n'
            '  version "8.5.0"\n',
            encoding="utf-8",
        )
        self.assertIsNone(_installed_version(classic, "sample"))
        self.assertEqual(_installed_version(classic, "sample", "^8.0.0"), "8.5.0")

    def test_no_lockfile_is_abstention_not_error(self) -> None:
        project = self._tmp / "nolock"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}}),
            encoding="utf-8",
        )
        self.assertIsNone(ResolutionInventory.for_project(project))
        self.assertIsNone(_installed_version(project, "sample", "^8.0.0"))

    def test_active_phase_inventory_is_set_during_discovery_and_restored(self) -> None:
        project = self._tmp / "phase"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"absent": "^1.0.0"}}),
            encoding="utf-8",
        )
        (project / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "x", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {"": {"name": "x", "version": "1.0.0"}},
                }
            ),
            encoding="utf-8",
        )
        seen: list = []

        def on_progress(processed: int, total: int) -> None:
            inventory = _ACTIVE_INVENTORY.get()
            seen.append(inventory is not None and inventory.belongs_to(project))

        targets, rows = _discover_targets(project, {"absent": "^1.0.0"}, None, progress=on_progress)
        self.assertEqual(targets, {})
        self.assertTrue(seen, "discovery must run with an active phase inventory")
        self.assertTrue(all(seen), "every probe must see the phase inventory")
        self.assertEqual(rows[0]["status"], "installed-version-unknown")
        self.assertIsNone(_ACTIVE_INVENTORY.get(), "the phase inventory must be restored after discovery")

    def test_parallel_discovery_parses_lockfile_once(self) -> None:
        from unittest.mock import patch

        # C1 parse-once must hold in the PARALLEL branch too: Python 3.14 no
        # longer copies contextvars into ThreadPoolExecutor workers, so the
        # phase inventory is handed to every probe explicitly. Ten packages
        # must build the inventory exactly ONCE.
        project = self._tmp / "parallel-once"
        project.mkdir(parents=True, exist_ok=True)
        names = {f"dep{i}": "^1.0.0" for i in range(10)}
        (project / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": names}), encoding="utf-8"
        )
        (project / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "x", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "x", "version": "1.0.0", "dependencies": names},
                        **{f"node_modules/dep{i}": {"version": "1.0.0"} for i in range(10)},
                    },
                }
            ),
            encoding="utf-8",
        )
        original = ResolutionInventory.for_project
        with patch.object(ResolutionInventory, "for_project", autospec=original) as built:
            built.side_effect = original
            targets, rows = _discover_targets(project, names, None, parallelism=4)
        self.assertEqual(built.call_count, 1, "ten parallel probes must parse the lockfile exactly once")
        self.assertEqual(len(rows), 10)

    def test_intermediate_ancestor_node_modules_wins_over_root(self) -> None:
        # A1: for packages/team/app the nearest hoisted copy is
        # packages/team/node_modules/demo@2.0.0 — the root copy 1.0.0 must NOT
        # win, and neither must a guess from nothing.
        root = self._tmp / "team-ws"
        (root / "packages" / "team" / "app").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps({"name": "root", "version": "1.0.0", "private": True, "workspaces": ["packages/**"]}),
            encoding="utf-8",
        )
        (root / "packages" / "team" / "app" / "package.json").write_text(
            json.dumps({"name": "app", "version": "1.0.0", "dependencies": {"demo": "^2.0.0"}}), encoding="utf-8"
        )
        (root / "packages" / "team" / "package.json").write_text(
            json.dumps({"name": "team", "version": "1.0.0", "dependencies": {"demo": "^2.0.0"}}), encoding="utf-8"
        )
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "root", "version": "1.0.0"},
                        "node_modules/demo": {"version": "1.0.0"},
                        "packages/team": {"name": "team", "version": "1.0.0", "dependencies": {"demo": "^2.0.0"}},
                        "packages/team/node_modules/demo": {"version": "2.0.0"},
                        "packages/team/app": {"name": "app", "version": "1.0.0", "dependencies": {"demo": "^2.0.0"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(_installed_version(root / "packages" / "team" / "app", "demo"), "2.0.0")

    def test_member_local_link_is_not_shadowed_by_root_version(self) -> None:
        # A1: a member-local `link:` portal must resolve through the linked
        # record even when the ROOT carries a real hoisted version of the same
        # name (the root copy is a different instance).
        root = self._tmp / "link-ws"
        (root / "packages" / "app").mkdir(parents=True, exist_ok=True)
        (root / "packages" / "lib").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps({"name": "root", "version": "1.0.0", "private": True, "workspaces": ["packages/*"]}),
            encoding="utf-8",
        )
        (root / "packages" / "app" / "package.json").write_text(
            json.dumps({"name": "app", "version": "1.0.0", "dependencies": {"lib": "^1.0.0"}}), encoding="utf-8"
        )
        (root / "packages" / "lib" / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"}), encoding="utf-8"
        )
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "root", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "root", "version": "1.0.0"},
                        "node_modules/lib": {"version": "9.9.9"},
                        "packages/app": {"name": "app", "version": "1.0.0", "dependencies": {"lib": "^1.0.0"}},
                        "packages/app/node_modules/lib": {"link": True, "resolved": "packages/lib"},
                        "packages/lib": {"name": "lib", "version": "1.7.0"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(_installed_version(root / "packages" / "app", "lib"), "1.7.0")

    def test_member_identity_reacts_to_root_lockfile_change(self) -> None:
        # B1: a workspace MEMBER's migration/delivery identity must bind the
        # SHARED root lockfile — editing the root lockfile changes the hashes
        # returned by `manifest_and_lock_hashes`, never a local-only look.
        root = self._tmp / "member-id"
        (root / "packages" / "app").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps({"name": "root", "version": "1.0.0", "private": True, "workspaces": ["packages/*"]}),
            encoding="utf-8",
        )
        (root / "packages" / "app" / "package.json").write_text(
            json.dumps({"name": "app", "version": "1.0.0", "dependencies": {"demo": "^1.0.0"}}), encoding="utf-8"
        )
        lockfile = root / "package-lock.json"
        lockfile.write_text(
            json.dumps({"name": "root", "version": "1.0.0", "lockfileVersion": 3,
                        "packages": {"": {"name": "root", "version": "1.0.0"},
                                     "packages/app": {"name": "app", "version": "1.0.0"},
                                     "node_modules/demo": {"version": "1.0.0"}}}),
            encoding="utf-8",
        )
        app = root / "packages" / "app"
        manifest_hash, lock_hash_before, lock_path = manifest_and_lock_hashes(app)
        self.assertIsNotNone(lock_path)
        self.assertTrue(str(lock_path).endswith("package-lock.json"))
        lockfile.write_text(
            json.dumps({"name": "root", "version": "1.0.0", "lockfileVersion": 3,
                        "packages": {"": {"name": "root", "version": "1.0.0"},
                                     "packages/app": {"name": "app", "version": "1.0.0"},
                                     "node_modules/demo": {"version": "1.2.0"}}}),
            encoding="utf-8",
        )
        _, lock_hash_after, _ = manifest_and_lock_hashes(app)
        self.assertEqual(manifest_hash, manifest_and_lock_hashes(app)[0])
        self.assertNotEqual(lock_hash_before, lock_hash_after, "root lockfile change must alter member identity")

    def test_lock_hash_covers_raw_bytes_with_bom(self) -> None:
        import hashlib

        # The phase lock_hash must hash the EXACT raw bytes: a UTF-8 BOM
        # stripped from the parsed text must NOT silently change the identity,
        # and a BOM addition must change it even when the decoded text matches.
        project = self._tmp / "bom-hash"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(
            json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"sample": "^8.0.0"}}), encoding="utf-8"
        )
        lock_path = project / "yarn.lock"
        content = (
            "__metadata:\n  version: 6\n  cacheKey: 8\n\n"
            '"sample@npm:^8.0.0":\n  version: 8.5.0\n  resolution: "sample@npm:8.5.0"\n  dependencies: {}\n'
        )
        lock_path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        inventory = ResolutionInventory.for_project(project)
        self.assertIsNotNone(inventory)
        assert inventory is not None
        self.assertEqual(inventory.installed_version("sample", "^8.0.0"), "8.5.0")
        self.assertEqual(inventory.lock_hash, hashlib.sha256(b"\xef\xbb\xbf" + content.encode("utf-8")).hexdigest())
        # Removing the BOM changes the identity although the decoded text is
        # byte-identical.
        lock_path.write_bytes(content.encode("utf-8"))
        without_bom = ResolutionInventory.for_project(project)
        self.assertIsNotNone(without_bom)
        assert without_bom is not None
        self.assertNotEqual(without_bom.lock_hash, inventory.lock_hash)


if __name__ == "__main__":
    unittest.main()

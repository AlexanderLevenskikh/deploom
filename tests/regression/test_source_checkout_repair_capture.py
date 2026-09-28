from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as roadmap
from source_snapshot import SourceInputPolicy, capture_source_snapshot


# R6 review P1#1: the Baseline repair chain's FRESH authoritative
# re-verification runs against an ISOLATED repair checkout (a local-only clone
# of the original project at the exact source commit the episode was pinned
# to) in which the repair agent has EDITED TRACKED files. The generator's
# source guard must keep rejecting such a dirty tree everywhere except the
# explicit, fail-closed repair-capture authorization:
#   DEPLOOM_BASELINE_SOURCE_REPAIR=1 + a pinned DEPLOOM_BASELINE_SOURCE_COMMIT
# that exactly matches HEAD -- and the sealed capture must then carry the
# REPAIRED content as a NEW source identity.
@unittest.skipUnless(shutil.which("git"), "git is required")
class RepairCaptureGuardTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def make_local_only_repo(self, root: Path) -> Path:
        """A repository with NO remotes -- exactly like the isolated repair
        checkout the Desktop creates (clone --shared, origin removed)."""
        repo = root / "repair-checkout"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        self.git(repo, "config", "user.name", "Roadmap Test")
        self.git(repo, "config", "user.email", "roadmap@example.test")
        self.git(repo, "checkout", "-b", "master")
        (repo / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
        self.git(repo, "add", "package.json")
        self.git(repo, "commit", "-m", "initial")
        return repo

    def spec(self, repo: Path) -> roadmap.ProjectSpec:
        return roadmap.ProjectSpec(
            name="Demo",
            path=repo,
            source_branch="master",
            git_remote="origin",
        )

    def test_dirty_tracked_file_is_rejected_without_repair_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_local_only_repo(Path(tmp))
            # The agent's edit is to a TRACKED file, never merely untracked noise.
            (repo / "package.json").write_text('{"name":"demo","version":"9.9.9"}\n', encoding="utf-8")

            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                roadmap.ensure_source_checkout(self.spec(repo))

            self.assertEqual("SOURCE_CHECKOUT_DIRTY", context.exception.code)
            self.assertIn("package.json", context.exception.detail)
            self.assertEqual("master", self.git(repo, "branch", "--show-current").stdout.strip())

    def test_repair_authorization_requires_the_pinned_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_local_only_repo(Path(tmp))
            pinned = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "package.json").write_text('{"name":"demo","version":"9.9.9"}\n', encoding="utf-8")

            # Flag set but pin missing -> fail closed.
            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                with temp_repair_capture(pin=None):
                    roadmap.ensure_source_checkout(self.spec(repo))
            self.assertEqual("SOURCE_CHECKOUT_REPAIR_PIN_MISSING", context.exception.code)

            # Wrong pin (base moved) -> fail closed; never captured.
            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                with temp_repair_capture(pin="0" * 40):
                    roadmap.ensure_source_checkout(self.spec(repo))
            self.assertEqual("SOURCE_CHECKOUT_REPAIR_HEAD_MISMATCH", context.exception.code)

            # Matching pin -> authorized; base commit untouched.
            with temp_repair_capture(pin=pinned):
                metadata = roadmap.ensure_source_checkout(self.spec(repo))
            self.assertTrue(metadata["verified"])
            self.assertTrue(metadata["repairCapture"])
            self.assertTrue(metadata["localOnly"])
            self.assertEqual(pinned, self.git(repo, "rev-parse", "HEAD").stdout.strip())
            # The edit itself is preserved (not stashed/committed/discarded).
            self.assertIn('"9.9.9"', (repo / "package.json").read_text(encoding="utf-8"))
            self.assertEqual("master", self.git(repo, "branch", "--show-current").stdout.strip())

    def test_repair_capture_seals_the_repaired_tree_as_a_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_local_only_repo(Path(tmp))
            pinned = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            # The ORIGINAL identity is the clean tree.
            original = capture_source_snapshot(Path(repo), policy=SourceInputPolicy())
            repaired_text = '{"name":"demo","version":"9.9.9"}\n'
            (repo / "package.json").write_text(repaired_text, encoding="utf-8")

            with temp_repair_capture(pin=pinned):
                roadmap.ensure_source_checkout(self.spec(repo))
                repaired = capture_source_snapshot(Path(repo), policy=SourceInputPolicy())

            # New content -> new sealed identity, holding the repaired bytes.
            self.assertNotEqual(original.key, repaired.key)
            self.assertTrue((repaired.project_path / "package.json").exists())
            self.assertEqual(
                repaired_text,
                (repaired.project_path / "package.json").read_text(encoding="utf-8"),
            )
            self.assertGreaterEqual(repaired.byte_count, len(repaired_text.encode("utf-8")))

    def test_repair_authorization_applies_to_remote_provenance_after_sync(self) -> None:
        # A repair clone keeps no remotes, but the same authorization must hold
        # even if the repair checkout had a remote: the dirty tree must pass
        # the post-sync check too (a regression would leave the guard rejecting
        # every authorized repair capture).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            seed = root / "seed"
            target = root / "target"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
            self.git(seed, "config", "user.name", "Roadmap Test")
            self.git(seed, "config", "user.email", "roadmap@example.test")
            self.git(seed, "checkout", "-b", "master")
            (seed / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            self.git(seed, "add", "package.json")
            self.git(seed, "commit", "-m", "initial")
            self.git(seed, "remote", "add", "origin", str(remote))
            self.git(seed, "push", "-u", "origin", "master")
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
            subprocess.run(["git", "clone", str(remote), str(target)], check=True, capture_output=True)
            self.git(target, "config", "user.name", "Roadmap Test")
            self.git(target, "config", "user.email", "roadmap@example.test")
            pinned = self.git(target, "rev-parse", "HEAD").stdout.strip()
            (target / "package.json").write_text('{"name":"demo","version":"9.9.9"}\n', encoding="utf-8")

            with temp_repair_capture(pin=pinned):
                metadata = roadmap.ensure_source_checkout(
                    roadmap.ProjectSpec(name="Demo", path=target, source_branch="master", git_remote="origin")
                )
            self.assertTrue(metadata["verified"])
            self.assertTrue(metadata["repairCapture"])
            self.assertFalse(metadata.get("localOnly", False))
            self.assertIn('"9.9.9"', (target / "package.json").read_text(encoding="utf-8"))


from contextlib import contextmanager


@contextmanager
def temp_repair_capture(pin: str | None):
    """Set/clear the repair-capture authorization for the duration of a block.

    pin=None means: the repair-capture FLAG is set (a repair re-verification is
    being attempted) but NO pin was provided -- the fail-closed branch that
    must refuse the capture. To run with NO authorization at all, use the plain
    environment (the ordinary SOURCE_CHECKOUT_DIRTY guard applies).
    """
    previous_capture = os.environ.get(roadmap.SOURCE_REPAIR_CAPTURE_ENV)
    previous_pin = os.environ.get(roadmap.SOURCE_REPAIR_PIN_ENV)
    try:
        os.environ[roadmap.SOURCE_REPAIR_CAPTURE_ENV] = "1"
        if pin is None:
            os.environ.pop(roadmap.SOURCE_REPAIR_PIN_ENV, None)
        else:
            os.environ[roadmap.SOURCE_REPAIR_PIN_ENV] = pin
        yield
    finally:
        if previous_capture is None:
            os.environ.pop(roadmap.SOURCE_REPAIR_CAPTURE_ENV, None)
        else:
            os.environ[roadmap.SOURCE_REPAIR_CAPTURE_ENV] = previous_capture
        if previous_pin is None:
            os.environ.pop(roadmap.SOURCE_REPAIR_PIN_ENV, None)
        else:
            os.environ[roadmap.SOURCE_REPAIR_PIN_ENV] = previous_pin


if __name__ == "__main__":
    unittest.main()

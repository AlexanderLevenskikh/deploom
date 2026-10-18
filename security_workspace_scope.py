"""Bound residual dependency repair to selected and reachable workspace inputs."""
import hashlib
import json
from pathlib import Path
from project_topology import _select_owned_lockfile, _workspace_member_manifests

SECTIONS = {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}
HUSKY_GENERATED = {".gitignore", "h", "husky.sh", "applypatch-msg", "commit-msg", "post-applypatch",
                   "post-checkout", "post-commit", "post-merge", "post-rewrite", "pre-applypatch",
                   "pre-auto-gc", "pre-commit", "pre-merge-commit", "pre-push", "pre-rebase", "prepare-commit-msg"}


# Byte fingerprints of upstream Husky 9.1.7 bootstrap (index.js and husky):
# https://github.com/typicode/husky/tree/v9.1.7
# Unknown/custom generated hook code stays blocked, even when Git-ignored.
def standard_husky_output(path):
    if path.is_symlink() or not path.is_file():
        return False
    data = path.read_bytes().replace(b"\r\n", b"\n")
    if path.name == ".gitignore":
        return data in {b"*", b"*\n"}
    digest = hashlib.sha256(data).hexdigest()
    expected = {"h": "70200b200ca709b0622784f93839a5b2872333a917a09afddefd7dc2d8cdc680",
                "husky.sh": "21122903fca7209a13c991e5be68780636e28f1b8f0ae7ea07ed0065dfe37268"}
    return digest == expected.get(path.name, "34fe496008be71d8fdd446b2cce81e4bb0406109c130eafc583fbd9fe33244e2")


def blocked_mutations(root, project_relative, run_id, before, changed, reference_root):
    import iterative_delivery as delivery
    root, reference_root = Path(root).resolve(), Path(reference_root).resolve()
    selected = (root / project_relative).resolve()
    relative = selected.relative_to(root).as_posix()
    prefix = relative + "/" if relative != "." else ""
    docs = f"docs/dependency-migration/{run_id}/"
    outside = {n for n in changed if not (n.startswith(prefix) or n.startswith(docs))}
    if not outside:
        return []
    # Generated Husky bootstrap files are install output only when absent from
    # the pinned source AND untracked/ignored by Git. Tracked hooks stay guarded.
    tracked = set(delivery.git(root, "ls-files", "-z").split("\0"))
    generated = {n for n in outside if Path(n).parent.as_posix() == ".husky/_"
                 and Path(n).name in HUSKY_GENERATED and before.get(n) is None and n not in tracked
                 and standard_husky_output(root / n)}
    if generated:
        ignored = set(delivery.git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0"))
        outside -= generated & ignored
    if not outside:
        return []

    def old_manifest(name):
        path = reference_root / name
        if not path.resolve().is_relative_to(reference_root) or path.is_symlink():
            raise ValueError("invalid reference")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != before.get(name):
            raise ValueError("reference changed")
        return json.loads(data.decode("utf-8-sig"))

    try:
        # Membership and dependency edges come from the pinned source, never
        # from the agent's expanded workspaces declaration or current manifest.
        old_selected = reference_root / project_relative
        manager, lockfile, patterns = _select_owned_lockfile(old_selected, source_boundary=reference_root)
        if manager == old_selected:
            return sorted(outside)
        manifests = _workspace_member_manifests(manager, patterns)
        by_name = {}
        for path in manifests:
            name = path.relative_to(reference_root).as_posix()
            manifest = old_manifest(name)
            by_name[manifest.get("name")] = (name, manifest)
        selected_name = (old_selected / "package.json").relative_to(reference_root).as_posix()
        queue, reachable = [selected_name], set()
        rows = {name: manifest for name, manifest in by_name.values()}
        while queue:
            name = queue.pop()
            if name in reachable:
                continue
            reachable.add(name)
            for section in SECTIONS:
                for dependency in (rows.get(name, {}).get(section) or {}):
                    if dependency in by_name:
                        queue.append(by_name[dependency][0])
        manager_manifest = (manager / "package.json").relative_to(reference_root).as_posix()
        lock_name = lockfile.relative_to(reference_root).as_posix()
        blocked = []
        for name in sorted(outside):
            if name == lock_name:
                continue
            if name not in reachable and name != manager_manifest:
                blocked.append(name)
                continue
            path = root / name
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                blocked.append(name)
                continue
            old = old_manifest(name)
            new = json.loads(path.read_text(encoding="utf-8-sig"))
            permitted = {"overrides", "resolutions"} if name == manager_manifest else SECTIONS
            differences = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
            if not differences.issubset(permitted):
                blocked.append(name)
                continue
            if name != manager_manifest and any(set(old.get(s) or {}) != set(new.get(s) or {}) for s in SECTIONS):
                blocked.append(name)
        return blocked
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return sorted(outside)  # Unavailable topology/evidence never widens scope.

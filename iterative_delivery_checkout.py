"""Own the hook bootstrap boundary of a delivery worktree."""
from pathlib import Path
import subprocess
from uuid import uuid4

LFS_HOOKS = {"post-checkout", "post-commit", "post-merge", "pre-push"}


def lfs_hook_bytes(name):
    return ("#!/bin/sh\n"
            'command -v git-lfs >/dev/null 2>&1 || { printf >&2 "\\n%%s\\n\\n" '
            '"This repository is configured for Git LFS but \'git-lfs\' was not found on your path. '
            'If you no longer wish to use Git LFS, remove this hook by deleting the \'%s\' file '
            "in the hooks directory (set by 'core.hookspath'; usually '.git/hooks').\"; exit 2; }\n"
            'git lfs %s "$@"\n' % (name, name)).encode()


def checkout_hook_args(run_dir, state):
    import iterative_delivery as delivery
    import iterative_migration as core
    # Project hooks must not edit a worktree before its base is recorded.
    # LFS can install its own hooks here without dirtying the source tree.
    directory = run_dir / "delivery" / "checkout-hooks" / uuid4().hex
    if not delivery.contained(run_dir, directory):
        raise RuntimeError("DELIVERY_CHECKOUT_HOOK_PATH_UNSAFE")
    directory.mkdir(parents=True)
    state["checkoutHooksPath"] = str(directory)
    core._write_json_atomic(run_dir / "delivery-state.json", state)
    return ("-c", f"core.hooksPath={directory.as_posix()}")


def bootstrap_husky_ignore(root, source, state):
    import iterative_delivery as delivery
    # Recreate only Husky's standard generated ignore, never an ignored payload.
    # A stranded older checkout may contain LFS's four exact default scripts.
    changed = delivery.git(root, "diff", "--name-only", "HEAD")
    untracked = set(delivery.git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    hooks_result = subprocess.run(["git", "-C", str(root), "config", "--get", "core.hooksPath"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    hook_name = hooks_result.stdout.strip().replace("\\", "/") if hooks_result.returncode == 0 else ""
    parts = Path(hook_name).parts
    if changed or len(parts) < 2 or parts[-2:] != (".husky", "_"):
        return
    target = root / hook_name / ".gitignore"
    original = source / hook_name / ".gitignore"
    if (not delivery.contained(root, target) or not delivery.contained(source, original)
            or target.exists() or not original.is_file() or original.is_symlink()
            or original.read_bytes().replace(b"\r\n", b"\n") != b"*\n"):
        return
    for name in untracked:
        path = Path(name)
        file = root / name
        if (path.parent.as_posix() != Path(hook_name).as_posix() or path.name not in LFS_HOOKS
                or not delivery.contained(root, file) or file.is_symlink()
                or file.read_bytes().replace(b"\r\n", b"\n") != lfs_hook_bytes(path.name)):
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(b"*\n")
    state["generatedCheckoutIgnore"] = target.relative_to(root).as_posix()

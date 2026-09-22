#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
IGNORED = {".git", "node_modules"}


def run(args: list[str], cwd: Path) -> None:
    print("+", " ".join(args), f"(cwd={cwd})")
    subprocess.run(args, cwd=cwd, check=True)


def package_root(repo: Path) -> Path:
    if (repo / "package.json").is_file():
        return repo
    candidates = [
        path.parent
        for path in repo.rglob("package.json")
        if not any(part in IGNORED for part in path.parts)
    ]
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise RuntimeError(f"{repo.name}: expected exactly one package root, got {candidates}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-lockfile", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly allow replacing an existing fixture directory. Each "
        "fixture is verified to stay strictly inside --out before removal.",
    )
    args = parser.parse_args()

    output = Path(args.out).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()

    for template in sorted(path for path in TEMPLATES.iterdir() if path.is_dir()):
        target = output / template.name
        if target.exists():
            if not args.overwrite:
                print(
                    f"[refused] {target} already exists. Pass --overwrite to "
                    f"replace it (verified to stay inside --out).",
                    file=sys.stderr,
                )
                return 2
            # Boundary guard: the removable prefix must be a direct child of
            # --out and must not be (or contain) the output root or an FS root.
            try:
                target.resolve().relative_to(output)
            except ValueError:
                print(f"[error] {target} resolves outside --out {output}.", file=sys.stderr)
                return 3
            if target.resolve() == output or target.resolve().parent != output:
                print(f"[error] refusing to remove {target} (not a direct fixture under --out).", file=sys.stderr)
                return 3
            shutil.rmtree(target)
        shutil.copytree(template, target)
        # Keep fixture manifests non-discoverable inside the DepLoom source
        # repository itself. They become real package.json files only in the
        # materialized standalone Git repositories.
        for manifest in sorted(target.rglob("package.template.json")):
            manifest.rename(manifest.with_name("package.json"))
        pkg = package_root(target)

        if not args.skip_lockfile:
            run([
                "npm.cmd" if os.name == "nt" else "npm",
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ], pkg)

        run(["git", "init", "-b", "master"], target)
        run(["git", "config", "user.email", "deploom-fixture@example.invalid"], target)
        run(["git", "config", "user.name", "DepLoom Fixture"], target)
        run(["git", "add", "--", "."], target)
        run(["git", "commit", "-m", "fixture: initial state"], target)
        print(f"[ready] {target} (package root: {pkg})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

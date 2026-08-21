#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
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
    args = parser.parse_args()

    output = Path(args.out).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    for template in sorted(path for path in TEMPLATES.iterdir() if path.is_dir()):
        target = output / template.name
        if target.exists():
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
                "npm", "install", "--package-lock-only", "--ignore-scripts",
                "--no-audit", "--no-fund",
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

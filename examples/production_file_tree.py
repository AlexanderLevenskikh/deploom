"""Deterministic nested/scoped dependency tree; no proprietary source or registry."""
from pathlib import Path
import json


def generate_dependency_tree(root: Path, *, entries: int = 1024) -> int:
    if entries < 1 or entries > 200_000:
        raise ValueError("entries must be between 1 and 200000")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("fixture destination must be empty")
    (root / "package.json").write_text(json.dumps({"name": "production-mini", "private": True,
        "packageManager": "yarn@1.22.22", "dependencies": {
            f"@fixture/island-{i:02d}": "1.0.0" for i in range(48)}}), encoding="utf-8")
    (root / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    for index in range(entries):
        package = root / "node_modules" / "@fixture" / f"island-{index % 48:02d}"
        if index % 3 == 0:
            package = package / "node_modules" / "@nested" / "runtime"
        package.mkdir(parents=True, exist_ok=True)
        (package / f"file-{index:06d}.js").write_text(f"module.exports = {index};\n", encoding="utf-8")
    return entries + 2

#!/usr/bin/env python3
"""Run tool unit tests and durable regression tests as separate suites."""
from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from cli_io import configure_utf8_stdio


def load_files(files: list[Path]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for index, path in enumerate(files):
        module_name = f"dependency_roadmap_test_{path.stem}_{index}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load test module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


def files_for(suite_name: str) -> list[Path]:
    unit = sorted((ROOT / "tests").glob("test_*.py"))
    regression = sorted((ROOT / "tests" / "regression").glob("test_*.py"))
    acceptance = sorted((ROOT / "tests" / "acceptance").glob("test_*.py"))
    if suite_name == "production-fast":
        return sorted((ROOT / "tests" / "production_fast").glob("test_*.py"))
    if suite_name == "production-stress":
        return sorted((ROOT / "tests" / "production_stress").glob("test_*.py"))
    if suite_name == "unit":
        return unit
    if suite_name == "regression":
        return regression
    if suite_name == "acceptance":
        return acceptance
    return unit + regression + acceptance


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("unit", "regression", "production-fast", "acceptance", "production-stress", "all"), default="unit")
    parser.add_argument("--list", action="store_true", help="List exact test files without running")
    args = parser.parse_args()
    files = files_for(args.suite)
    if args.list:
        for path in files:
            print(path.relative_to(ROOT).as_posix())
        return 0
    if not files:
        print(f"No {args.suite} tests found", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(load_files(files))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

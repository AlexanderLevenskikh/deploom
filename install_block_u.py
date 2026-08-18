#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict

BASELINE_MARKER = "BLOCK_U_VERIFICATION_SUBSTRATE_V1"
GENERATOR_MARKER = "BLOCK_U_EARLY_PROJECT_SCREEN_V1"
REQUIRED_TIME_MARKER = "BLOCK_U_TIME_TO_RESULT_V1"
U2_MARKER = "BLOCK_U2_GRANULAR_FALLBACK_V1"


def locate_root(explicit: str) -> Path:
    candidates: list[Path] = []
    if explicit:
        root = Path(explicit).expanduser().resolve()
        candidates.extend(
            [
                root,
                root / "tool",
                root / "resources" / "tool",
                root / "desktop" / "resources" / "tool",
            ]
        )
    else:
        cwd = Path.cwd().resolve()
        candidates.extend(
            [cwd, cwd / "tool", cwd / "resources" / "tool"]
        )
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(
                Path(local)
                / "Programs"
                / "DepLoom"
                / "resources"
                / "tool"
            )
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if (
            (candidate / "baseline_constraint_verifier.py").is_file()
            and (candidate / "dependency_live_roadmap_generator.py").is_file()
        ):
            return candidate
    raise RuntimeError(
        "Could not locate DepLoom tool root. Pass --root <repo root or resources/tool>."
    )


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected exactly one anchor, found {count}"
        )
    return text.replace(old, new, 1)


def replace_regex_once(
    text: str, pattern: str, replacement: str, label: str
) -> str:
    updated, count = re.subn(
        pattern, replacement, text, count=1, flags=re.S
    )
    if count != 1:
        raise RuntimeError(
            f"{label}: expected exactly one function block, found {count}"
        )
    return updated


def patch_baseline(text: str) -> str:
    if BASELINE_MARKER in text:
        print(f"[skip] {BASELINE_MARKER} already present")
        return text
    if REQUIRED_TIME_MARKER not in text:
        raise RuntimeError(
            "Block U requires BLOCK_U_TIME_TO_RESULT_V1. "
            "Current source is older than the intended v0.2.45+hotfix baseline."
        )

    if U2_MARKER in text:
        print("[info] preserving BLOCK_U2_GRANULAR_FALLBACK_V1")
    else:
        print(
            "[info] BLOCK_U2_GRANULAR_FALLBACK_V1 is not present; "
            "continuing from the current source without inventing that older hotfix"
        )

    import_anchor = '''from prepared_workspace_fastpath import (
    acquire_snapshot_copy_lease,
    build_dependency_integrity_manifest,
    try_acquire_snapshot_cleanup_lease,
    cleanup_guarded_clone,
    guarded_clone_is_active,
    stop_guarded_clone,
    try_materialize_guarded_clone,
)
'''
    import_replacement = import_anchor + f'''# {BASELINE_MARKER}
from verification_process_supervisor import run_supervised
from verification_workspace_backend import (
    materialize_private_tree,
    workspace_backend_summary,
)
'''
    text = replace_once(
        text,
        import_anchor,
        import_replacement,
        "Block U substrate imports",
    )

    # Important: replace only _run itself. U2 inserts its heartbeat-aware
    # _remove_tree_with_progress helper immediately BEFORE _run; replacing from
    # _terminate_process_tree would delete that hotfix.
    runner = '''def _run(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    env: Optional[Mapping[str, str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "command",
    progress_interval_seconds: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Run a verifier child through the cross-platform Block U supervisor."""
    return run_supervised(
        argv,
        cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        base_env=base_env,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )


'''
    text = replace_regex_once(
        text,
        r"def _run\(.*?(?=def clean_ephemeral_verification_caches)",
        runner,
        "Block U supervised command runner",
    )

    snapshot_runner = '''def _run_snapshot_copy(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "snapshot copy",
    progress_interval_seconds: int = 15,
) -> subprocess.CompletedProcess[str]:
    return run_supervised(
        argv,
        cwd,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )


'''
    text = replace_regex_once(
        text,
        r"def _run_snapshot_copy\(.*?(?=def _copy_tree_snapshot)",
        snapshot_runner,
        "Block U supervised snapshot runner",
    )

    copy_impl = '''def _copy_tree_snapshot(
    source: Path,
    target: Path,
    *,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "prepared snapshot copy",
    timeout_seconds: int = 1800,
    progress_interval_seconds: int = 15,
) -> None:
    """Materialize a proof-safe private tree through the platform backend."""
    mode = materialize_private_tree(
        source,
        target,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )
    _emit_progress(
        progress,
        f"{progress_label}: materialization backend={mode}",
    )


'''
    text = replace_regex_once(
        text,
        r"def _copy_tree_snapshot\(.*?(?=def _prepared_snapshot_slot)",
        copy_impl,
        "Block U workspace backend",
    )

    progress_anchor = (
        '    _emit_progress(progress, f"{progress_label}: started; '
        'attemptHardTimeout={config.attempt_timeout_seconds}s")\n'
    )
    if "verification substrate:" not in text:
        text = replace_once(
            text,
            progress_anchor,
            progress_anchor
            + '    _emit_progress(progress, f"{progress_label}: verification substrate: '
            '{workspace_backend_summary()}")\n',
            "Block U substrate telemetry",
        )

    # Safety checks: prior hotfixes must survive verbatim when they existed.
    if U2_MARKER in text:
        for required in (
            U2_MARKER,
            "rerunning ONLY this command",
            "_remove_tree_with_progress(",
            "private_result = verify_assignment(",
        ):
            if required not in text:
                raise RuntimeError(
                    f"Block U would damage U2 contract: missing {required}"
                )
    for required in (
        REQUIRED_TIME_MARKER,
        "_prepared_command_fastpath_allowed(",
        "seal_dependency_integrity=preparation_fastpath_enabled",
        "run_supervised(",
        "materialize_private_tree(",
    ):
        if required not in text:
            raise RuntimeError(
                f"Block U postcondition missing: {required}"
            )
    return text


def patch_generator(text: str) -> str:
    if GENERATOR_MARKER in text:
        print(f"[skip] {GENERATOR_MARKER} already present")
        return text

    start = text.find(
        '                    if config.project_checks != "off" and config.commands:\n'
        '                        combined = verify_assignment('
    )
    if start < 0:
        raise RuntimeError(
            "Cannot find initial combined resolver/project verification block"
        )
    end = text.find(
        '                    if resolver_trial_key and (\n', start
    )
    if end < 0:
        raise RuntimeError("Cannot find resolver cache continuation anchor")

    resolver_only = f'''                    # {GENERATOR_MARKER}
                    # Resolver authority first. Project checks are scheduled below
                    # so adaptive mode can reject a freshly introduced structural
                    # regression before paying for the complete project suite.
                    result = verify_assignment(
                        spec.path,
                        verification_assignment,
                        config=config,
                        run_project_checks=False,
                        remove_packages=removals,
                        progress=lambda message: (
                            progress_reporter.emit(
                                project, mode, "resolver-verification",
                                iteration=iteration,
                                assignment=fingerprint,
                                message=message,
                            ),
                            eprint(f"[info] {{project}}: {{message}}"),
                        ),
                        progress_label=(
                            f"Baseline {{mode}} iteration {{iteration}} "
                            f"resolver {{fingerprint}}"
                        ),
                    )
'''
    text = text[:start] + resolver_only + text[end:]

    old_project = '''                        project_result = (
                            project_preflight_cache.get(project_cache_key)
                            if project_cache_key
                            else None
                        )
                        if project_result is None:
                            project_result = verify_assignment(
                                spec.path, verification_assignment, config=config, run_project_checks=True, remove_packages=removals,
                                progress=lambda message: (progress_reporter.emit(project, mode, "project-preflight", iteration=iteration, assignment=fingerprint, message=message), eprint(f"[info] {project}: {message}")),
                                progress_label=f"Baseline {mode} iteration {iteration} project preflight {fingerprint}",
                            )
                            if (
                                project_result.kind not in {"infrastructure", "unknown"}
                                and resolver_trial_key
                                and project_result.resolved_state_key
                            ):
                                exact_project_key = build_project_trial_key(
                                    resolver_trial_key=resolver_trial_key,
                                    resolved_state_key=project_result.resolved_state_key,
                                    source_snapshot_key=project_source_snapshot_key,
                                    project_checks=config.project_checks,
                                    commands=config.commands,
                                )
                                project_preflight_cache[exact_project_key] = project_result
'''

    new_project = '''                        project_result = (
                            project_preflight_cache.get(project_cache_key)
                            if project_cache_key
                            else None
                        )
                        screen_structural_evidence: Tuple[str, ...] = ()
                        if (
                            project_result is None
                            and config.project_checks == "adaptive"
                            and len(config.commands) > 1
                        ):
                            screen_command = config.commands[0]
                            screen_config = dataclasses.replace(
                                config, commands=(screen_command,)
                            )
                            progress_reporter.emit(
                                project, mode, "adaptive-screen-started",
                                iteration=iteration,
                                assignment=fingerprint,
                                command=screen_command,
                            )
                            screen_result = verify_assignment(
                                spec.path,
                                verification_assignment,
                                config=screen_config,
                                run_project_checks=True,
                                remove_packages=removals,
                                progress=lambda message: (
                                    progress_reporter.emit(
                                        project, mode, "adaptive-screen-running",
                                        iteration=iteration,
                                        assignment=fingerprint,
                                        command=screen_command,
                                        message=message,
                                    ),
                                    eprint(
                                        f"[info] {project}: Baseline adaptive "
                                        f"screen {screen_command}: {message}"
                                    ),
                                ),
                                progress_label=(
                                    f"Baseline {mode} iteration {iteration} "
                                    f"adaptive screen {fingerprint}"
                                ),
                            )
                            if screen_result.kind == "infrastructure":
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_VERIFY_INFRA_ERROR: "
                                    f"{project}/{mode}: adaptive screen "
                                    f"{screen_command}: {screen_result.summary}"
                                )
                            if screen_result.kind == "unknown":
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_VERIFY_UNKNOWN_ERROR: "
                                    f"{project}/{mode}: adaptive screen "
                                    f"{screen_command}: {screen_result.summary}"
                                )
                            if not screen_result.ok:
                                screen_structural_evidence = (
                                    adaptive_structural_evidence(screen_result)
                                )
                            if screen_structural_evidence:
                                project_result = screen_result
                                progress_reporter.emit(
                                    project, mode,
                                    "adaptive-screen-introduced-regression",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    command=screen_command,
                                    structuralEvidence=list(
                                        screen_structural_evidence
                                    ),
                                )
                                eprint(
                                    f"[warn] {project}: adaptive screen rejected "
                                    f"assignment {fingerprint} before remaining "
                                    f"{len(config.commands) - 1} project check(s); "
                                    f"introduced="
                                    f"{', '.join(screen_structural_evidence)}"
                                )
                            else:
                                progress_reporter.emit(
                                    project, mode, "adaptive-screen-complete",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    command=screen_command,
                                    outcome=(
                                        "pass"
                                        if screen_result.ok
                                        else "baseline-preexisting-or-nonstructural"
                                    ),
                                )

                        # Successful candidates still need the full configured
                        # ProjectProof. Screening only short-circuits a freshly
                        # proven introduced structural regression.
                        if project_result is None:
                            project_result = verify_assignment(
                                spec.path, verification_assignment, config=config, run_project_checks=True, remove_packages=removals,
                                progress=lambda message: (progress_reporter.emit(project, mode, "project-preflight", iteration=iteration, assignment=fingerprint, message=message), eprint(f"[info] {project}: {message}")),
                                progress_label=f"Baseline {mode} iteration {iteration} project preflight {fingerprint}",
                            )
                            if (
                                project_result.kind not in {"infrastructure", "unknown"}
                                and resolver_trial_key
                                and project_result.resolved_state_key
                            ):
                                exact_project_key = build_project_trial_key(
                                    resolver_trial_key=resolver_trial_key,
                                    resolved_state_key=project_result.resolved_state_key,
                                    source_snapshot_key=project_source_snapshot_key,
                                    project_checks=config.project_checks,
                                    commands=config.commands,
                                )
                                project_preflight_cache[exact_project_key] = project_result
'''
    text = replace_once(
        text,
        old_project,
        new_project,
        "Block U adaptive project screen",
    )

    text = replace_once(
        text,
        "                        structural_evidence = adaptive_structural_evidence(project_result)\n",
        "                        structural_evidence = (\n"
        "                            screen_structural_evidence\n"
        "                            or adaptive_structural_evidence(project_result)\n"
        "                        )\n",
        "Block U reuse early structural evidence",
    )

    for required in (
        GENERATOR_MARKER,
        "adaptive-screen-introduced-regression",
        "screen_structural_evidence",
        "run_project_checks=False",
    ):
        if required not in text:
            raise RuntimeError(
                f"Block U generator postcondition missing: {required}"
            )
    return text


def _retryable_windows_io(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33}


def _write_bytes_retrying(
    path: Path,
    data: bytes,
    *,
    label: str,
    timeout_seconds: float = 30.0,
) -> None:
    """Overwrite/create one file without relying on rename/delete sharing.

    Direct write is deliberate on Windows: editors, Defender and indexers may
    transiently deny DELETE/rename sharing while still releasing the handle a
    moment later. Retrying the actual write avoids the old os.replace/pyc lock
    failure mode. Originals are held in memory and on disk before commit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    delay = 0.10
    attempts = 0
    while True:
        attempts += 1
        try:
            with open(path, "wb") as handle:
                handle.write(data)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            if attempts > 1:
                print(
                    f"[io] {label}: acquired after {attempts} attempts: {path}"
                )
            return
        except OSError as exc:
            if not _retryable_windows_io(exc):
                raise RuntimeError(
                    f"{label}: cannot write {path}: {exc}"
                ) from exc
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                raise RuntimeError(
                    f"{label}: Windows kept file locked for "
                    f"{elapsed:.1f}s: {path}; winerror={getattr(exc, 'winerror', None)}. "
                    "Close only the process actively using this source tree "
                    "(often a running DepLoom/Python test process); IDE can stay open."
                ) from exc
            if attempts == 1 or attempts % 5 == 0:
                print(
                    f"[io] {label}: transient Windows lock; retrying: "
                    f"{path} (elapsed={elapsed:.1f}s)"
                )
            time.sleep(delay)
            delay = min(1.0, delay * 1.5)


def _syntax_check(source: str, filename: str) -> None:
    # No py_compile: it writes/atomically replaces __pycache__ files and can
    # itself trip WinError 32 under Defender/indexers. compile() is pure memory.
    compile(source, filename, "exec", dont_inherit=True)


def _build_payloads(
    root: Path, package_root: Path
) -> tuple[Dict[Path, bytes], Dict[Path, bytes | None]]:
    baseline = root / "baseline_constraint_verifier.py"
    generator = root / "dependency_live_roadmap_generator.py"

    baseline_text = baseline.read_text(encoding="utf-8")
    generator_text = generator.read_text(encoding="utf-8")
    patched_baseline = patch_baseline(baseline_text)
    patched_generator = patch_generator(generator_text)

    payloads: Dict[Path, bytes] = {
        baseline: patched_baseline.encode("utf-8"),
        generator: patched_generator.encode("utf-8"),
        root / "verification_process_supervisor.py": (
            package_root / "verification_process_supervisor.py"
        ).read_bytes(),
        root / "verification_workspace_backend.py": (
            package_root / "verification_workspace_backend.py"
        ).read_bytes(),
    }

    tests_src = package_root / "tests"
    tests_dst = root / "tests"
    if tests_dst.is_dir():
        for name in (
            "test_block_u_verification_substrate.py",
            "test_block_u2_granular_fallback.py",
            "test_block_u_time_to_result.py",
        ):
            source = tests_src / name
            if source.is_file():
                payloads[tests_dst / name] = source.read_bytes()

    originals: Dict[Path, bytes | None] = {
        path: (path.read_bytes() if path.is_file() else None)
        for path in payloads
    }

    # Validate every Python payload before touching the target tree.
    for path, data in payloads.items():
        if path.suffix == ".py":
            _syntax_check(data.decode("utf-8"), str(path))
    print(f"[validate] in-memory compile PASS ({len(payloads)} file(s))")
    return payloads, originals


def _write_backup(
    root: Path, originals: Dict[Path, bytes | None]
) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / ".dependency-roadmap" / f"block-u-backup-{stamp}-{os.getpid()}"
    backup.mkdir(parents=True, exist_ok=False)
    manifest_lines = []
    for path, data in originals.items():
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        manifest_lines.append(
            f"{'present' if data is not None else 'absent'}\t{relative.as_posix()}"
        )
        if data is not None:
            destination = backup / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    (backup / "manifest.txt").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )
    return backup


def _restore_retrying(
    originals: Dict[Path, bytes | None]
) -> list[str]:
    failures: list[str] = []
    for path, data in originals.items():
        try:
            if data is None:
                if path.exists():
                    # New Block U files can be left harmlessly if Windows denies
                    # deletion; report it instead of lying about rollback.
                    try:
                        path.unlink()
                    except OSError as exc:
                        if _retryable_windows_io(exc):
                            deadline = time.monotonic() + 10.0
                            while path.exists() and time.monotonic() < deadline:
                                time.sleep(0.25)
                                try:
                                    path.unlink()
                                except OSError:
                                    pass
                        if path.exists():
                            failures.append(f"delete {path}: {exc}")
            else:
                _write_bytes_retrying(
                    path,
                    data,
                    label="rollback",
                    timeout_seconds=15.0,
                )
        except Exception as exc:
            failures.append(f"restore {path}: {exc}")
    return failures


def _run_smoke_tests(root: Path) -> None:
    test_file = root / "tests" / "test_block_u_verification_substrate.py"
    if not test_file.is_file():
        print("[validate] smoke tests skipped: tests directory unavailable")
        return
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "unittest",
            "tests.test_block_u_verification_substrate",
            "-v",
        ],
        cwd=str(root),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    print(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(
            f"Block U smoke tests failed: exit={result.returncode}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="")
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="skip the short Block U smoke test; in-memory syntax validation still runs",
    )
    args = parser.parse_args()

    root = locate_root(args.root)
    package_root = Path(__file__).resolve().parent
    print(f"[info] tool root: {root}")

    # Build + syntax-check everything before the first target write.
    try:
        payloads, originals = _build_payloads(root, package_root)
    except Exception as exc:
        print(f"[abort-before-write] {exc}", file=sys.stderr)
        return 1

    backup = _write_backup(root, originals)
    print(f"[backup] {backup}")

    committed: list[Path] = []
    try:
        # New support modules first, main verifier/generator last. If a support
        # module is locked, Baseline source is still untouched.
        ordered = sorted(
            payloads,
            key=lambda path: (
                1
                if path.name in {
                    "baseline_constraint_verifier.py",
                    "dependency_live_roadmap_generator.py",
                }
                else 0,
                str(path),
            ),
        )
        for path in ordered:
            _write_bytes_retrying(
                path,
                payloads[path],
                label="Block U commit",
                timeout_seconds=30.0,
            )
            committed.append(path)
        print(f"[commit] wrote {len(committed)} file(s)")

        # Re-read and compile in memory; never generate .pyc files.
        for path in ordered:
            if path.suffix == ".py":
                _syntax_check(path.read_text(encoding="utf-8"), str(path))
        print("[validate] post-write in-memory compile PASS")

        if not args.skip_tests:
            _run_smoke_tests(root)

        baseline_text = (
            root / "baseline_constraint_verifier.py"
        ).read_text(encoding="utf-8")
        generator_text = (
            root / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        for marker, source in (
            (BASELINE_MARKER, baseline_text),
            (REQUIRED_TIME_MARKER, baseline_text),
            (GENERATOR_MARKER, generator_text),
        ):
            if marker not in source:
                raise RuntimeError(f"post-install marker missing: {marker}")

        print("[done] BLOCK_U_VERIFICATION_SUBSTRATE_V1 applied")
        print("[done] existing Block U time-to-result preserved")
        if U2_MARKER in baseline_text:
            print("[done] existing Block U2 granular fallback preserved")
        else:
            print("[done] U2 was not present before install; no synthetic U2 added")
        print("[done] adaptive early structural screen enabled")
        print("[done] successful candidates still require full ProjectProof")
        print(f"[done] recovery backup kept at: {backup}")
        return 0
    except Exception as exc:
        print(f"[rollback] {exc}", file=sys.stderr)
        failures = _restore_retrying(originals)
        if failures:
            print(
                "[rollback] automatic restore was PARTIAL; use backup: "
                f"{backup}",
                file=sys.stderr,
            )
            for failure in failures:
                print(f"[rollback] {failure}", file=sys.stderr)
        else:
            print("[rollback] original files restored", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from baseline_constraint_verifier import (
    BaselineVerifyConfig,
    _apply_assignment,
    _classify_install_failure,
    _run,
    _terminate_process_tree,
    clean_ephemeral_verification_caches,
    detect_package_manager,
    discover_baseline_project_checks,
    is_structural_project_failure,
    install_args,
    structural_project_failure_signatures,
    BaselineVerifyResult,
    verify_assignment,
)


class BaselineConstraintVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._observed_patch = patch(
            "baseline_constraint_verifier.observed_resolved_assignment",
            return_value={"demo": "2.0.0"},
        )
        self._observed_patch.start()
        self.addCleanup(self._observed_patch.stop)

    def test_config_bounds_parallelism_and_defaults_to_adaptive_project_checks(self) -> None:
        config = BaselineVerifyConfig.from_mapping({"parallelism": 999, "maxIterations": 0})
        self.assertEqual(16, config.parallelism)
        self.assertEqual(1, config.max_iterations)
        self.assertEqual("adaptive", config.project_checks)
        self.assertEqual(600, config.timeout_seconds)
        self.assertEqual(3600, config.attempt_timeout_seconds)
        self.assertEqual(7200, config.localization_timeout_seconds)
        self.assertEqual(15, config.progress_interval_seconds)

    def test_package_manager_detection_prefers_explicit_package_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"packageManager": "yarn@1.22.22"}), encoding="utf-8")
            (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
            self.assertEqual("yarn", detect_package_manager(root))

    def test_missing_package_manager_is_infrastructure_and_never_dependency_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
            )
            (root / "yarn.lock").write_text("", encoding="utf-8")
            with patch("baseline_constraint_verifier.resolve_executable", return_value=None):
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="off"),
                )
            self.assertFalse(result.ok)
            self.assertEqual("infrastructure", result.kind)
            self.assertIn("INFRA_PACKAGE_MANAGER_NOT_FOUND", result.summary)

    def test_failure_classifier_never_learns_network_outage(self) -> None:
        self.assertEqual("infrastructure", _classify_install_failure("error ECONNRESET socket hang up"))
        self.assertEqual("dependency", _classify_install_failure("ERESOLVE conflicting peer dependency"))

    def test_unknown_package_manager_failure_is_not_dependency_evidence(self) -> None:
        self.assertEqual("unknown", _classify_install_failure("resolver crashed with opaque code XYZ-42"))

    def test_lifecycle_install_infrastructure_failure_is_never_project_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 1, stdout="npm error HTTP 502 Bad Gateway"),
            ]
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="strict", commands=("npm run typecheck",)),
                    run_project_checks=True,
                )
            self.assertFalse(result.ok)
            self.assertEqual("infrastructure", result.kind)
            self.assertIn("lifecycle install failed", result.summary)

    def test_lifecycle_install_dependency_failure_remains_dependency_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 1, stdout="ERESOLVE conflicting peer dependency"),
            ]
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="strict", commands=("npm run typecheck",)),
                    run_project_checks=True,
                )
            self.assertFalse(result.ok)
            self.assertEqual("dependency", result.kind)

    def test_unclassified_lifecycle_failure_is_preparation_not_project_debt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 7, stdout="postinstall deterministic toolchain failure"),
            ]
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="strict", commands=("npm run typecheck",)),
                    run_project_checks=True,
                )
            self.assertFalse(result.ok)
            self.assertEqual("preparation", result.kind)
            self.assertIn("lifecycle/preparation failed", result.summary)



    def test_yarn_resolver_only_install_explicitly_disables_scripts(self) -> None:
        self.assertEqual(["install", "--ignore-scripts"], install_args("yarn", ignore_scripts=True))
        self.assertEqual(["install"], install_args("yarn", ignore_scripts=False))
        self.assertEqual(["install", "--frozen-lockfile"], install_args("yarn", ignore_scripts=False, frozen=True))
        self.assertEqual(["ci", "--no-audit", "--no-fund"], install_args("npm", ignore_scripts=False, frozen=True))

    def test_assignment_missing_from_manifest_is_never_a_vacuous_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "ASSIGNMENT_PACKAGE_NOT_DECLARED: added"):
                _apply_assignment(root, {"added": "2.0.0"})

            result = verify_assignment(
                root,
                {"added": "2.0.0"},
                config=BaselineVerifyConfig(project_checks="off"),
            )
            self.assertFalse(result.ok)
            self.assertEqual("unknown", result.kind)
            self.assertIn("ASSIGNMENT_PACKAGE_NOT_DECLARED: added", result.summary)


    def test_assignment_can_materialize_remove_action_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({
                    "dependencies": {"uuid": "9.0.0"},
                    "devDependencies": {"@types/uuid": "8.3.4"},
                }),
                encoding="utf-8",
            )
            changed = _apply_assignment(
                root, {"uuid": "11.1.1", "@types/uuid": "11.0.0"},
                remove_packages={"@types/uuid"},
            )
            manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(["@types/uuid", "uuid"], changed)
            self.assertNotIn("@types/uuid", manifest["devDependencies"])
            self.assertEqual("11.1.1", manifest["dependencies"]["uuid"])

    def test_auto_discovers_small_structural_lint_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "packageManager": "yarn@1.22.22",
                "scripts": {
                    "lint:types": "tsc",
                    "lint:styles": "stylelint src/**/*.css",
                    "lint:scripts": "eslint .",
                    "build": "vite build",
                    "test:unit": "vitest run",
                },
            }), encoding="utf-8")
            self.assertEqual(
                ("yarn lint:types", "yarn lint:styles", "yarn lint:scripts", "yarn build", "yarn test:unit"),
                discover_baseline_project_checks(root),
            )

    def test_auto_discovers_flow_typecheck_without_typescript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "packageManager": "yarn@1.22.22",
                "devDependencies": {"flow-bin": "0.246.0"},
                "scripts": {
                    "flow": "flow",
                    "lint:flow": "flow check --show-all-errors",
                    "lint:scripts": "eslint .",
                },
            }), encoding="utf-8")
            self.assertEqual(
                ("yarn lint:flow", "yarn lint:scripts"),
                discover_baseline_project_checks(root),
            )

    def test_adaptive_classifier_promotes_module_loader_failure_only(self) -> None:
        structural = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:styles",
            output="SyntaxError: Cannot use import statement outside a module",
        )
        migration = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:types",
            output="src/App.tsx(4,3): error TS2339: Property oldProp does not exist on type NewApi",
        )
        self.assertTrue(is_structural_project_failure(structural))
        self.assertFalse(is_structural_project_failure(migration))

    def test_adaptive_classifier_promotes_duplicate_type_universe_failure(self) -> None:
        duplicate_vite = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:types",
            output=(
                "error TS2769: No overload matches this call. "
                "Type 'import(\"C:/repo/node_modules/vite/dist/node/index\").UserConfig' "
                "is not assignable to type "
                "'import(\"C:/repo/node_modules/vitest/node_modules/vite/dist/node/index\").UserConfig'."
            ),
        )
        ordinary_api_migration = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:types",
            output="src/App.tsx(4,3): error TS2769: No overload matches this call",
        )
        self.assertTrue(is_structural_project_failure(duplicate_vite))
        self.assertFalse(is_structural_project_failure(ordinary_api_migration))


    def test_project_preflight_collects_all_command_failures_instead_of_stopping_at_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 0, stdout="lifecycle ok"),
                CompletedProcess(["npm", "run", "types"], 2, stdout="src/App.ts: error TS2339: ordinary source migration"),
                CompletedProcess(["npm", "run", "styles"], 1, stdout=(
                    "SyntaxError: Cannot use import statement outside a module\n"
                    " at C:/repo/node_modules/stylelint-selector-tag-no-without-class/index.js:1:1"
                )),
            ]
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root, {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="adaptive", commands=("npm run types", "npm run styles")),
                    run_project_checks=True,
                )
            self.assertFalse(result.ok)
            self.assertEqual("project", result.kind)
            self.assertEqual(["npm run types", "npm run styles"], [item.command for item in result.project_failures])
            self.assertIn("esm-cjs:stylelint-selector-tag-no-without-class", structural_project_failure_signatures(result))


    def test_project_output_tokens_are_not_infrastructure_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8"
            )
            tokens = ("ENOENT assertion text", "EACCES expected diagnostic", "HTTP 503 fixture")
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 0, stdout="lifecycle ok"),
                *[
                    CompletedProcess(["npm", "run", f"check-{index}"], 1, stdout=token)
                    for index, token in enumerate(tokens, start=1)
                ],
            ]
            commands = tuple(f"npm run check-{index}" for index in range(1, 4))
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root, {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(project_checks="diagnostic", commands=commands),
                    run_project_checks=True,
                )
            self.assertEqual("project", result.kind)
            self.assertEqual(list(commands), [item.command for item in result.project_failures])

    def test_project_launch_oserror_is_typed_without_secondary_unboundlocal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8"
            )
            runs = [
                CompletedProcess(["npm", "install"], 0, stdout="resolver ok"),
                CompletedProcess(["npm", "install"], 0, stdout="lifecycle ok"),
                OSError("synthetic launch failure"),
            ]
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch("baseline_constraint_verifier._run", side_effect=runs):
                result = verify_assignment(
                    root, {"demo": "2.0.0"},
                    config=BaselineVerifyConfig(
                        project_checks="diagnostic", commands=("npm run check",)
                    ),
                    run_project_checks=True,
                )
            self.assertEqual("infrastructure", result.kind)
            self.assertIn("project check launch failed", result.summary)
    def test_module_resolution_structural_signature_is_independent_from_unrelated_baseline_type_error(self) -> None:
        candidate = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:types", command="yarn lint:types",
            output=(
                "src/Old.ts(1,1): error TS2339: Property oldProp does not exist\n"
                "config/vite/plugins.js:3:34 - error TS2307: Cannot find module '@vitejs/plugin-react' or its corresponding type declarations.\n"
                "There are types at 'C:/tmp/node_modules/@vitejs/plugin-react/dist/index.d.ts', but this result could not be resolved under your current 'moduleResolution' setting. "
                "Consider updating to 'node16', 'nodenext', or 'bundler'."
            ),
        )
        control = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn lint:types", command="yarn lint:types",
            output="config/vite/index.js:21: error TS2769: server.https boolean migration debt",
        )
        candidate_signatures = set(structural_project_failure_signatures(candidate))
        control_signatures = set(structural_project_failure_signatures(control))
        self.assertEqual({"ts-module-resolution:@vitejs/plugin-react"}, candidate_signatures - control_signatures)

    def test_assignment_updates_duplicate_direct_declarations_in_all_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "devDependencies": {"typescript": "^5.4.0"},
                        "peerDependencies": {"typescript": ">=5.0.0 <6"},
                    }
                ),
                encoding="utf-8",
            )

            changed = _apply_assignment(root, {"typescript": "5.7.3"})
            manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))

            self.assertEqual(["typescript"], changed)
            self.assertEqual("5.7.3", manifest["devDependencies"]["typescript"])
            self.assertEqual("5.7.3", manifest["peerDependencies"]["typescript"])

    def test_sass_compiler_runtime_api_is_structural_evidence(self) -> None:
        result = BaselineVerifyResult(
            False, "project", "project preflight failed: yarn test:unit",
            output=(
                "TypeError: sass.initAsyncCompiler is not a function\n"
                "    at node_modules/vite/dist/node/chunks/dep.js:123:9"
            ),
        )
        self.assertIn(
            "toolchain-runtime-api:sass.initasynccompiler",
            structural_project_failure_signatures(result),
        )

    def test_ephemeral_verification_caches_are_cleaned_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in (
                "node_modules/.vite",
                "node_modules/.cache",
                "src/node_modules/.vitest",
                "src/node_modules/.cache",
            ):
                target = root / relative
                target.mkdir(parents=True, exist_ok=True)
                (target / "cache.json").write_text("{}", encoding="utf-8")
            removed = clean_ephemeral_verification_caches(root)
            self.assertEqual(
                (
                    "node_modules/.vite",
                    "node_modules/.cache",
                    "src/node_modules/.vitest",
                    "src/node_modules/.cache",
                ),
                removed,
            )
            for relative in removed:
                self.assertFalse((root / relative).exists())
    def test_run_uses_sealed_base_environment_snapshot(self) -> None:
        captured = {}

        class FakeProcess:
            pid = 1234
            returncode = 0

            def __init__(self, _argv, **kwargs):
                captured.update(kwargs.get("env") or {})

            def poll(self):
                return 0

            def communicate(self, timeout=None):
                return ("ok", None)

        with patch("baseline_constraint_verifier.subprocess.Popen", FakeProcess):
            result = _run(
                ["demo"],
                Path("."),
                timeout_seconds=5,
                base_env={"BASE_ONLY": "sealed", "PATH": "fixed"},
                env={"OVERRIDE": "yes"},
            )
        self.assertEqual(0, result.returncode)
        self.assertEqual("sealed", captured["BASE_ONLY"])
        self.assertEqual("yes", captured["OVERRIDE"])
        self.assertEqual("fixed", captured["PATH"])



    def test_windows_process_tree_termination_uses_taskkill_tree_force(self) -> None:
        class FakeProcess:
            pid = 4242
            def __init__(self) -> None:
                self.killed = False
            def poll(self):
                return None if not self.killed else 1
            def kill(self) -> None:
                self.killed = True

        process = FakeProcess()
        with patch("baseline_constraint_verifier.os.name", "nt"), \
                patch("baseline_constraint_verifier.subprocess.run") as run:
            _terminate_process_tree(process)  # type: ignore[arg-type]
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(["taskkill", "/PID", "4242", "/T", "/F"], argv)
        self.assertTrue(process.killed)

    def test_subprocess_runner_emits_heartbeat_and_hard_times_out(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    Path(tmp),
                    timeout_seconds=1,
                    progress=messages.append,
                    progress_label="hang-fixture",
                    progress_interval_seconds=1,
                )
        self.assertTrue(any("HARD_TIMEOUT" in message for message in messages))

    def test_exact_resolver_proof_cache_hit_skips_uncached_verifier(self) -> None:
        from verification_proof import VerificationProofStore, bind_resolved_state_identity, build_verification_proof_identity
        from resolved_dependency_state import capture_resolved_dependency_state, resolved_state_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}),
                encoding="utf-8",
            )
            cache = Path(tmp) / "cas"
            config = BaselineVerifyConfig(project_checks="off", proof_cache_dir=str(cache))
            identity = build_verification_proof_identity(
                root,
                assignment={"demo": "2.0.0"},
                remove_packages=(),
                manager="npm",
                manager_executable=sys.executable,
                registry="",
                project_checks="off",
                commands=(),
                environment=dict(os.environ),
            )
            observed_hash = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            state = capture_resolved_dependency_state(
                root,
                manager="npm",
                resolver_input_key=identity.resolver_input_key,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=cache,
            )
            identity = bind_resolved_state_identity(
                identity,
                state.key,
                project_checks='off',
                commands=(),
            )
            VerificationProofStore(cache).publish_pass(
                "resolver", identity.resolver_input_key, identity,
                metadata={
                    "observedResolvedVersions": {},
                    "observedResolvedHash": observed_hash,
                    **resolved_state_metadata(state),
                },
            )

            with patch("baseline_constraint_verifier.resolve_executable", return_value=sys.executable), \
                    patch("baseline_constraint_verifier._verify_assignment_uncached") as uncached:
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=config,
                    run_project_checks=False,
                )
            self.assertTrue(result.ok)
            self.assertIn("ResolverProof cache hit", result.summary)
            self.assertEqual(state.key, result.resolved_state_key)
            uncached.assert_not_called()

    def test_resolver_cache_hit_marks_combined_verification_for_resolver_skip(self) -> None:
        from verification_proof import VerificationProofStore, bind_resolved_state_identity, build_verification_proof_identity
        from resolved_dependency_state import capture_resolved_dependency_state, resolved_state_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "package.json").write_text(
                json.dumps({
                    "packageManager": "npm@11.0.0",
                    "dependencies": {"demo": "1.0.0"},
                    "scripts": {"typecheck": "tsc"},
                }),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}),
                encoding="utf-8",
            )
            cache = Path(tmp) / "cas"
            config = BaselineVerifyConfig(
                project_checks="adaptive",
                commands=("npm run typecheck",),
                proof_cache_dir=str(cache),
            )
            identity = build_verification_proof_identity(
                root,
                assignment={"demo": "2.0.0"},
                remove_packages=(),
                manager="npm",
                manager_executable=sys.executable,
                registry="",
                project_checks="adaptive",
                commands=("npm run typecheck",),
                environment=dict(os.environ),
            )
            observed_hash = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            state = capture_resolved_dependency_state(
                root,
                manager="npm",
                resolver_input_key=identity.resolver_input_key,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=cache,
            )
            identity = bind_resolved_state_identity(
                identity,
                state.key,
                project_checks='adaptive',
                commands=('npm run typecheck',),
            )
            VerificationProofStore(cache).publish_pass(
                "resolver", identity.resolver_input_key, identity,
                metadata={
                    "observedResolvedVersions": {},
                    "observedResolvedHash": observed_hash,
                    **resolved_state_metadata(state),
                },
            )

            captured = {}
            def fake_uncached(project_dir, assignment, *, config, **kwargs):
                captured["reuse"] = config.reuse_resolver_proof_key
                return BaselineVerifyResult(True, "passed", "fresh lifecycle/project verification")

            with patch("baseline_constraint_verifier.resolve_executable", return_value=sys.executable), \
                    patch("baseline_constraint_verifier._verify_assignment_uncached", side_effect=fake_uncached):
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=config,
                    run_project_checks=True,
                )
            self.assertTrue(result.ok)
            self.assertEqual(identity.resolver_input_key, captured["reuse"])

    def test_resolver_cache_hit_emits_bound_resolved_state_once(self) -> None:
        from verification_proof import VerificationProofStore, bind_resolved_state_identity, build_verification_proof_identity
        from resolved_dependency_state import capture_resolved_dependency_state, resolved_state_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "package.json").write_text(
                json.dumps({"packageManager": "npm@11.0.0", "dependencies": {"demo": "1.0.0"}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}),
                encoding="utf-8",
            )
            cache = Path(tmp) / "cas"
            telemetry = Path(tmp) / "telemetry.jsonl"
            config = BaselineVerifyConfig(
                project_checks="off",
                proof_cache_dir=str(cache),
                telemetry_path=str(telemetry),
            )
            identity = build_verification_proof_identity(
                root,
                assignment={"demo": "2.0.0"},
                remove_packages=(),
                manager="npm",
                manager_executable=sys.executable,
                registry="",
                project_checks="off",
                commands=(),
                environment=dict(os.environ),
            )
            observed_hash = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            state = capture_resolved_dependency_state(
                root,
                manager="npm",
                resolver_input_key=identity.resolver_input_key,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=cache,
            )
            identity = bind_resolved_state_identity(
                identity,
                state.key,
                project_checks='off',
                commands=(),
            )
            VerificationProofStore(cache).publish_pass(
                "resolver", identity.resolver_input_key, identity,
                metadata={
                    "observedResolvedVersions": {},
                    "observedResolvedHash": observed_hash,
                    **resolved_state_metadata(state),
                },
            )

            with patch("baseline_constraint_verifier.resolve_executable", return_value=sys.executable), \
                    patch("baseline_constraint_verifier._verify_assignment_uncached") as uncached:
                result = verify_assignment(
                    root,
                    {"demo": "2.0.0"},
                    config=config,
                    run_project_checks=False,
                )

            self.assertTrue(result.ok)
            uncached.assert_not_called()
            events = [
                json.loads(line)
                for line in telemetry.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            hits = [item for item in events if item.get("event") == "proof.cache.hit"]
            self.assertTrue(hits)
            self.assertEqual(state.key, hits[-1].get("resolvedStateKey"))

    def test_remove_only_assignment_is_verified_as_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({
                    "packageManager": "npm@11.0.0",
                    "devDependencies": {"@types/demo": "1.0.0"},
                }),
                encoding="utf-8",
            )
            # _run is mocked in this fixture, so it cannot create npm's real
            # lockfile for ResolvedState capture. Seed the deterministic fixture
            # lock explicitly; production still requires the package manager's
            # post-resolve bytes.
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {}}),
                encoding="utf-8",
            )
            with patch("baseline_constraint_verifier.resolve_executable", return_value="npm"), \
                    patch(
                        "baseline_constraint_verifier._run",
                        return_value=CompletedProcess(["npm", "install"], 0, stdout="ok"),
                    ):
                result = verify_assignment(
                    root,
                    {"@types/demo": "1.0.0"},
                    remove_packages=("@types/demo",),
                    config=BaselineVerifyConfig(project_checks="off"),
                )
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("passed", result.kind)

    def test_yarn_resolution_warning_is_not_authoritative_dependency_failure(self) -> None:
        output = (
            'warning Resolution field "es5-ext@0.10.50" is incompatible '
            'with requested version "es5-ext@^0.10.62"\n'
            'error command failed with opaque exit 1'
        )
        self.assertEqual("unknown", _classify_install_failure(output))

    def test_yarn_peer_warning_code_is_not_authoritative_dependency_failure(self) -> None:
        self.assertEqual(
            "unknown",
            _classify_install_failure(
                "YN0060: react is listed with version 18 which does not satisfy a peer request"
            ),
        )

    def test_fatal_yarn_no_candidates_remains_dependency_failure(self) -> None:
        self.assertEqual(
            "dependency",
            _classify_install_failure(
                "YN0082: │ demo@npm:^9.0.0: No candidates found"
            ),
        )




class ObservedResolutionProofTests(unittest.TestCase):
    def test_full_direct_observation_detects_resolutions_drift(self) -> None:
        import baseline_constraint_verifier as verifier
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "dependencies": {"demo": "2.0.0"},
                "resolutions": {"demo": "1.5.0"},
            }), encoding="utf-8")
            installed = root / "node_modules" / "demo"
            installed.mkdir(parents=True)
            (installed / "package.json").write_text(
                json.dumps({"name": "demo", "version": "1.5.0"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                verifier.ObservedResolutionError,
                "OBSERVED_RESOLVED_ASSIGNMENT_DRIFT",
            ):
                verifier.observed_resolved_assignment(root, {"demo": "2.0.0"})

    def test_full_direct_observation_has_stable_peer_optional_and_remove_sentinels(self) -> None:
        import baseline_constraint_verifier as verifier
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "optionalDependencies": {"optional-demo": "2.0.0"},
                "peerDependencies": {"peer-demo": "3.0.0"},
            }), encoding="utf-8")
            observed = verifier.observed_resolved_assignment(
                root,
                {
                    "optional-demo": "2.0.0",
                    "peer-demo": "3.0.0",
                    "removed-demo": "1.0.0",
                },
                remove_packages=("removed-demo",),
            )
            self.assertEqual("<optional-not-installed>", observed["optional-demo"])
            self.assertEqual("<peer-only>", observed["peer-demo"])
            self.assertEqual("<removed>", observed["removed-demo"])
            self.assertEqual(64, len(verifier.observed_resolved_hash(observed)))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import block_vex_storage
import dependency_live_roadmap_generator as roadmap


class BaselineUnknownResolverHotfixTests(unittest.TestCase):
    def test_yarn_cache_policy_is_storage_profile_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = verifier.BaselineVerifyConfig(
                proof_cache_dir=str(root / "baseline-proofs")
            )

            ordinary = block_vex_storage.VerificationStorageProfile(
                root=root / "ordinary",
                filesystem="ntfs",
            )
            with mock.patch.object(
                block_vex_storage,
                "verification_storage_profile",
                return_value=ordinary,
            ):
                self.assertEqual(
                    {},
                    verifier._package_manager_cache_environment(config, "yarn"),
                )
                npm = verifier._package_manager_cache_environment(config, "npm")
                self.assertIn("npm_config_cache", npm)

            optimized = block_vex_storage.VerificationStorageProfile(
                root=root / "refs",
                filesystem="refs",
                refs_same_volume_capable=True,
            )
            with mock.patch.object(
                block_vex_storage,
                "verification_storage_profile",
                return_value=optimized,
            ):
                yarn = verifier._package_manager_cache_environment(config, "yarn")
                self.assertIn("YARN_CACHE_FOLDER", yarn)
                self.assertIn(
                    "package-manager-artifacts",
                    yarn["YARN_CACHE_FOLDER"].replace("\\", "/"),
                )

    def test_known_yarn_network_and_auth_failures_are_infrastructure(self) -> None:
        cases = (
            "There appears to be trouble with your network connection. Retrying...",
            'ResponseError: Request failed "401 Unauthorized"',
            'Request failed "403 Forbidden"',
            "getaddrinfo ENOTFOUND registry.example.test",
            "ESOCKETTIMEDOUT",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual("infrastructure", verifier._classify_install_failure(text))

        self.assertEqual(
            "dependency",
            verifier._classify_install_failure("ERESOLVE unable to resolve dependency tree"),
        )
        self.assertEqual(
            "unknown",
            verifier._classify_install_failure("package manager exited unexpectedly"),
        )

    def test_unknown_failure_diagnostic_tail_is_redacted(self) -> None:
        # Build credential-shaped samples at runtime so the public sanitizer
        # never sees literal secret-looking credentials in repository text.
        bearer = "Authorization: " + "Bearer " + "abc" + "123"
        npm_auth = "_" + "auth" + "Token" + "=" + "def" + "456"
        credential_url = (
            "https://" + "alice" + ":" + "secret" + "@" + "example.test/pkg"
        )
        query_url = (
            "https://registry.example.test/pkg?"
            + "token" + "=" + "ghi" + "789"
        )
        raw = "\n".join((
            bearer, npm_auth, credential_url, query_url,
            "error package manager exited unexpectedly", "",
        ))
        safe = roadmap._sanitized_baseline_failure_tail(raw)
        self.assertNotIn("abc123", safe)
        self.assertNotIn("def456", safe)
        self.assertNotIn("alice:secret", safe)
        self.assertNotIn("ghi789", safe)
        self.assertIn("<redacted>", safe)
        self.assertIn("package manager exited unexpectedly", safe)

    def test_unknown_baseline_error_surfaces_diagnostic_and_is_non_retryable(self) -> None:
        generator_source = Path(roadmap.__file__).read_text(encoding="utf-8")
        desktop_source = (
            ROOT / "desktop" / "electron" / "main.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("Resolver diagnostic tail (sanitized)", generator_source)

        fn_start = desktop_source.index("function nonRetryableDeterministicFailure")
        fn_end = desktop_source.index("async function executeCommand", fn_start)
        non_retryable = desktop_source[fn_start:fn_end]
        self.assertIn("BASELINE_VERIFY_UNKNOWN_ERROR", non_retryable)


if __name__ == "__main__":
    unittest.main()

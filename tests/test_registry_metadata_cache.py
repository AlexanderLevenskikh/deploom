from __future__ import annotations

import hashlib
import http.server
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dependency_live_roadmap_generator as roadmap

from registry_metadata_cache import (
    CACHE_SCHEMA_VERSION,
    load_packument,
    store_packument,
)

PACKUMENT = {
    "name": "pkg-a",
    "dist-tags": {"latest": "1.2.3"},
    "versions": {
        "1.0.0": {"engines": {"node": ">=12"}},
        "1.2.3": {"engines": {"node": ">=14"}},
    },
}


def _stub_server(packument, unknown_paths=()):
    counts = {"hits": 0}
    unknown = set(unknown_paths)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            counts["hits"] += 1
            path = self.path.split("?", 1)[0].strip("/")
            if path in unknown or not path:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(packument).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, counts


def _registry_url(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server, thread):
    try:
        server.shutdown()
    except OSError:
        pass
    try:
        server.server_close()
    except OSError:
        pass
    thread.join(timeout=2.0)


def _client(registry, cache_dir, ttl=600.0, tool="test-tool"):
    return roadmap.LiveDataClient(
        registry,
        timeout=10,
        batch_size=100,
        sleep_sec=0.0,
        use_system_proxy=False,
        metadata_cache_dir=cache_dir,
        metadata_cache_ttl=ttl,
        metadata_cache_tool_version=tool,
    )


class RegistryMetadataCacheDirectTests(unittest.TestCase):
    def test_roundtrip_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            store_packument(cache_dir, "https://reg.example/npm", "pkg-a", PACKUMENT, tool_version="tool-1", now=1_000_000.0)
            self.assertEqual(load_packument(cache_dir, "https://reg.example/npm", "pkg-a", ttl_seconds=600, now=1_000_500.0), PACKUMENT)
            self.assertIsNone(load_packument(cache_dir, "https://reg.example/npm", "pkg-a", ttl_seconds=600, now=1_000_600.001))
            self.assertIsNone(load_packument(cache_dir, "https://reg.example/npm", "pkg-a", ttl_seconds=0, now=1_000_500.0))
            reg_dir = cache_dir / hashlib.sha256("https://reg.example/npm".encode()).hexdigest()[:16]
            raw = json.loads((reg_dir / (hashlib.sha256("pkg-a".encode()).hexdigest() + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], CACHE_SCHEMA_VERSION)
            self.assertEqual(raw["registry"], "https://reg.example/npm")
            self.assertEqual(raw["name"], "pkg-a")
            self.assertEqual(raw["toolVersion"], "tool-1")
            self.assertIn("fetchedAt", raw)
            self.assertIn("fetchedAtUnix", raw)

    def test_registry_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            store_packument(cache_dir, "https://reg-a", "pkg-a", PACKUMENT, now=1_000_000.0)
            self.assertIsNone(load_packument(cache_dir, "https://reg-b", "pkg-a", ttl_seconds=600, now=1_000_100.0))
            self.assertEqual(load_packument(cache_dir, "https://reg-a", "pkg-a", ttl_seconds=600, now=1_000_100.0), PACKUMENT)

    def test_corrupt_and_mismatched_entries_are_misses(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            reg_dir = cache_dir / hashlib.sha256("https://reg".encode()).hexdigest()[:16]
            reg_dir.mkdir(parents=True)
            bad = reg_dir / (hashlib.sha256("pkg-a".encode()).hexdigest() + ".json")
            bad.write_text("not json", encoding="utf-8")
            self.assertIsNone(load_packument(cache_dir, "https://reg", "pkg-a", 600, now=1_000_100.0))
            wrong_name = reg_dir / (hashlib.sha256("pkg-b".encode()).hexdigest() + ".json")
            wrong_name.write_text(
                json.dumps({"schema": CACHE_SCHEMA_VERSION, "registry": "https://reg", "name": "pkg-a", "data": PACKUMENT, "fetchedAtUnix": 1_000_000.0}),
                encoding="utf-8",
            )
            self.assertIsNone(load_packument(cache_dir, "https://reg", "pkg-b", 600, now=1_000_100.0))

    def test_non_dict_payloads_never_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            store_packument(cache_dir, "https://reg", "pkg-a", "not-a-dict", now=1_000_000.0)
            self.assertIsNone(load_packument(cache_dir, "https://reg", "pkg-a", 600, now=1_000_100.0))
            store_packument(None, "https://reg", "pkg-a", PACKUMENT, now=1_000_000.0)
            self.assertIsNone(load_packument(None, "https://reg", "pkg-a", 600, now=1_000_100.0))

    def test_store_never_raises_when_cache_dir_cannot_be_created(self):
        # P1 (#2): mkdir in store_packument is outside the protective try, so a
        # path that cannot become a directory (here: a non-directory occupying
        # the cache root) would propagate OSError into the fetch caller and turn
        # a successful registry response into an infrastructure error.
        with tempfile.TemporaryDirectory() as temp:
            blocked = Path(temp) / "cache-root-file"
            blocked.write_text("occupied", encoding="utf-8")
            store_packument(blocked, "https://reg.example/npm", "pkg-a", PACKUMENT, tool_version="tool-1", now=1_000_000.0)
            # Best-effort: nothing persisted, no exception escaped.
            self.assertEqual(load_packument(blocked, "https://reg.example/npm", "pkg-a", 600, now=1_000_100.0), None)

    def test_concurrent_stores_are_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"

            def store(i):
                store_packument(cache_dir, "https://reg", f"pkg-{i}", {"i": i}, tool_version=f"t{i}", now=1_000_000.0)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(store, range(16)))
            for i in range(16):
                self.assertEqual(load_packument(cache_dir, "https://reg", f"pkg-{i}", 600, now=1_000_100.0), {"i": i})

    def test_cache_module_never_serves_install_or_audit(self):
        source = Path(roadmap.__file__).parent / "registry_metadata_cache.py"
        raw = source.read_text(encoding="utf-8")
        # Scan code only: the module docstring documents the guarantee, so
        # strip it before asserting which identifiers the code may reference.
        marker = '"""'
        first = raw.find(marker)
        if first != -1:
            second = raw.find(marker, first + 3)
            body = raw[second + 3:] if second != -1 else raw
        else:
            body = raw
        for forbidden in ("subprocess", "yarn", "manual_dependency_audit", "npm", "tarfile", "lockfile"):
            self.assertNotIn(forbidden, body, f"planning packument cache code must not touch {forbidden}")


class RegistryMetadataCacheClientTests(unittest.TestCase):
    def test_second_invocation_is_network_free(self):
        server, thread, counts = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            reg = _registry_url(server)
            first = _client(reg, cache_dir)
            self.assertEqual(first.fetch_npm_metadata("pkg-a"), PACKUMENT)
            self.assertEqual(counts["hits"], 1)
            second = _client(reg, cache_dir)
            self.assertEqual(second.fetch_npm_metadata("pkg-a"), PACKUMENT)
            self.assertEqual(counts["hits"], 1, "a warm planning cache must serve the second invocation without a network fetch")
            self.assertEqual(second.metadata_cache_stats(), (1, 0))

    def test_successful_fetch_with_unwritable_cache_is_still_success(self):
        # P1 (#2): an unusable cache directory must not degrade a successful
        # registry fetch into an infrastructure error or extra retries.
        server, thread, counts = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            blocked = Path(temp) / "cache-root-file"
            blocked.write_text("occupied", encoding="utf-8")
            client = _client(_registry_url(server), blocked)
            self.assertEqual(client.fetch_npm_metadata("pkg-a"), PACKUMENT)
            self.assertEqual(counts["hits"], 1)

    def test_measurement_before_after(self):
        server, thread, counts = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            reg = _registry_url(server)
            no_cache_first = _client(reg, None)
            no_cache_first.fetch_npm_metadata("pkg-a")
            no_cache_first.fetch_npm_metadata("pkg-b")
            no_cache_second = _client(reg, None)
            no_cache_second.fetch_npm_metadata("pkg-a")
            no_cache_second.fetch_npm_metadata("pkg-b")
            self.assertEqual(counts["hits"], 4, "without the planning cache every invocation refetches")

            server2, thread2, counts2 = _stub_server(PACKUMENT)
            self.addCleanup(_stop_server, server2, thread2)
            cache_dir = Path(temp) / "cache"
            warm = _client(_registry_url(server2), cache_dir)
            warm.fetch_npm_metadata("pkg-a")
            warm.fetch_npm_metadata("pkg-b")
            warm2 = _client(_registry_url(server2), cache_dir)
            warm2.fetch_npm_metadata("pkg-a")
            warm2.fetch_npm_metadata("pkg-b")
            self.assertEqual(counts2["hits"], 2, "with the planning cache the second invocation needs zero fetches")

    def test_registry_isolation_between_live_clients(self):
        server_a, thread_a, counts_a = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server_a, thread_a)
        server_b, thread_b, counts_b = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server_b, thread_b)
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            _client(_registry_url(server_a), cache_dir).fetch_npm_metadata("pkg-a")
            self.assertEqual(counts_a["hits"], 1)
            _client(_registry_url(server_b), cache_dir).fetch_npm_metadata("pkg-a")
            self.assertEqual(counts_b["hits"], 1, "a different registry must not be served from another registry's entry")

    def test_ttl_expiry_refetches(self):
        server, thread, counts = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            reg = _registry_url(server)
            _client(reg, cache_dir, ttl=0.05).fetch_npm_metadata("pkg-a")
            self.assertEqual(counts["hits"], 1)
            time.sleep(0.08)
            _client(reg, cache_dir, ttl=0.05).fetch_npm_metadata("pkg-a")
            self.assertEqual(counts["hits"], 2, "an expired planning entry must be refetched, not reused")

    def test_four_oh_four_is_not_cached_as_absence(self):
        server, thread, counts = _stub_server(PACKUMENT, unknown_paths=("missing-pkg",))
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            reg = _registry_url(server)
            self.assertIsNone(_client(reg, cache_dir).fetch_npm_metadata("missing-pkg"))
            self.assertEqual(counts["hits"], 1)
            self.assertIsNone(_client(reg, cache_dir).fetch_npm_metadata("missing-pkg"))
            self.assertEqual(counts["hits"], 2, "an unavailable package must not be invented from a stale cache")

    def test_prefetch_seeds_from_planning_cache(self):
        server, thread, counts = _stub_server(PACKUMENT)
        self.addCleanup(_stop_server, server, thread)
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp) / "cache"
            reg = _registry_url(server)
            _client(reg, cache_dir).fetch_npm_metadata("pkg-a")
            self.assertEqual(counts["hits"], 1)
            resumed = _client(reg, cache_dir)
            roadmap._prefetch_registry_metadata(
                resumed,
                [("pkg-a", "prod", "^1.0.0")],
                progress_label="resume-test",
            )
            self.assertEqual(counts["hits"], 1, "prefetch must serve a fresh planning entry from disk")
            self.assertEqual(resumed.npm_cache.get("pkg-a"), PACKUMENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)

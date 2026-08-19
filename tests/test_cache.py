"""
Pure-ish tests for the per-target cache in ``pwneye.core.storage.cache``.

The cache persists successful credentials, so three properties matter beyond
"it round-trips": the on-disk file must be owner-only (0600), a crash mid-write
must not leave a partial/temp file behind (atomic replace), and a host string
must be sanitized before it becomes a filename. Each test runs against an
isolated temp CACHE_DIR so nothing touches ``~/.pwneye``.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pwneye.core.storage import cache


class CacheTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        # _cache_path / save_target / load_target all read the module-level
        # CACHE_DIR name bound at import time, so patch it there.
        self._patch = mock.patch.object(cache, "CACHE_DIR", self.cache_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class PathSanitizationTests(CacheTestBase):
    def test_plain_ip_maps_to_yaml_file(self):
        self.assertEqual(
            cache.get_target_cache_path("1.2.3.4"),
            self.cache_dir / "1.2.3.4.yaml",
        )

    def test_dangerous_characters_are_replaced(self):
        # A host must never smuggle a path separator into the cache filename.
        path = cache.get_target_cache_path("../a/b:c")
        self.assertEqual(path.parent, self.cache_dir)
        self.assertNotIn("/", path.name[:-5])  # ignore the ".yaml" suffix
        self.assertEqual(path.name, ".._a_b_c.yaml")


class RoundTripTests(CacheTestBase):
    def test_missing_target_returns_none(self):
        self.assertIsNone(cache.load_target("nope.example"))

    def test_save_then_load_preserves_structure(self):
        cache.save_target("1.2.3.4", {"onvif": {"supported": True}})
        loaded = cache.load_target("1.2.3.4")
        self.assertEqual(loaded["target"]["host"], "1.2.3.4")
        self.assertTrue(loaded["onvif"]["supported"])
        self.assertIn("rtsp", loaded)
        self.assertIn("first_seen", loaded["target"])
        self.assertIn("last_seen", loaded["target"])

    def test_saved_file_is_owner_only(self):
        cache.save_target("host-a", {})
        mode = cache.get_target_cache_path("host-a").stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_save_leaves_no_temp_files_behind(self):
        cache.save_target("host-b", {})
        leftovers = [p.name for p in self.cache_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_corrupt_yaml_returns_none(self):
        path = cache.get_target_cache_path("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("key: [unterminated", encoding="utf-8")
        self.assertIsNone(cache.load_target("broken"))

    def test_count_and_clear(self):
        cache.save_target("h1", {})
        cache.save_target("h2", {})
        self.assertEqual(cache.count_entries(), 2)
        self.assertEqual(cache.clear_all(), 2)
        self.assertEqual(cache.count_entries(), 0)


class OnvifCacheTests(CacheTestBase):
    def test_upsert_and_get_auth(self):
        cache.upsert_onvif_success(
            "1.2.3.4", port=80, username="admin", password="secret",
            manufacturer="Hik", streams=["rtsp://a", "rtsp://a", "rtsp://b"],
        )
        auth = cache.get_cached_onvif_auth(cache.load_target("1.2.3.4"))
        self.assertEqual(auth["port"], 80)
        self.assertEqual(auth["username"], "admin")
        self.assertEqual(auth["password"], "secret")
        self.assertEqual(auth["manufacturer"], "Hik")
        # Streams are de-duplicated while preserving order.
        self.assertEqual(auth["streams"], ["rtsp://a", "rtsp://b"])

    def test_get_auth_none_when_data_missing(self):
        self.assertIsNone(cache.get_cached_onvif_auth(None))

    def test_get_auth_none_when_fields_incomplete(self):
        cache.save_target("1.2.3.4", {"onvif": {"port": 80, "auth": {"username": "x"}}})
        self.assertIsNone(cache.get_cached_onvif_auth(cache.load_target("1.2.3.4")))

    def test_discovery_only_persists_manufacturer(self):
        cache.upsert_onvif_discovery("1.2.3.4", manufacturer="Dahua")
        self.assertEqual(
            cache.get_cached_onvif_manufacturer(cache.load_target("1.2.3.4")),
            "Dahua",
        )
        # A discovery with no manufacturer is a no-op (no file written).
        cache.upsert_onvif_discovery("5.6.7.8", manufacturer=None)
        self.assertIsNone(cache.load_target("5.6.7.8"))


class RtspCacheTests(CacheTestBase):
    def test_upsert_and_get_auth(self):
        cache.upsert_rtsp_success(
            "1.2.3.4", port=554, username="u", password="p",
            path="/live", protocol="tcp", url="rtsp://u:p@1.2.3.4:554/live",
        )
        auth = cache.get_cached_rtsp_auth(cache.load_target("1.2.3.4"))
        self.assertEqual(auth["port"], 554)
        self.assertEqual(auth["url"], "rtsp://u:p@1.2.3.4:554/live")
        self.assertEqual(auth["protocol"], "tcp")

    def test_get_auth_none_when_any_field_missing(self):
        cache.save_target("1.2.3.4", {
            "rtsp": {"port": 554, "path": "/live", "protocol": "tcp",
                     "auth": {"username": "u", "password": "p"}},  # url missing
        })
        self.assertIsNone(cache.get_cached_rtsp_auth(cache.load_target("1.2.3.4")))

    def test_banner_round_trip(self):
        cache.upsert_rtsp_banner("1.2.3.4", port=554, banner="RTSP/1.0 200 OK Dahua")
        banner = cache.get_cached_rtsp_banner(cache.load_target("1.2.3.4"))
        self.assertEqual(banner["port"], 554)
        self.assertIn("Dahua", banner["value"])

    def test_channels_are_deduplicated_and_normalized(self):
        cache.upsert_rtsp_channels("1.2.3.4", channels=[
            {"channel": 1, "url": "rtsp://a/1", "port": 554, "path": "/1", "protocol": "tcp"},
            {"channel": 1, "url": "rtsp://a/1-dup"},          # duplicate id -> dropped
            {"channel": 2, "url": "rtsp://a/2"},
            {"channel": 3, "url": None},                       # no url -> dropped
            {"channel": None, "url": "rtsp://a/x"},            # no id -> dropped
        ])
        channels = cache.get_cached_rtsp_channels(cache.load_target("1.2.3.4"))
        ids = [c["channel"] for c in channels]
        self.assertEqual(ids, [1, 2])

    def test_get_channels_empty_when_data_missing(self):
        self.assertEqual(cache.get_cached_rtsp_channels(None), [])


if __name__ == "__main__":
    unittest.main()

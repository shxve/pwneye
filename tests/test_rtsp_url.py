"""
Pure-function tests for RTSP URL construction, parsing, and defensive header
handling in ``pwneye.core.network.rtsp``.

These functions decide where a credentialed probe actually connects, so a bug
here means either a probe against the wrong host or a mangled password. All
targets are pure (no sockets), so the suite is fast and hermetic.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import unittest

from pwneye.core.network.rtsp import (
    MAX_RTSP_BODY_BYTES,
    _parse_content_length,
    add_rtsp_auth,
    build_rtsp_url,
    parse_rtsp_url,
)


class BuildRtspUrlTests(unittest.TestCase):
    def test_no_credentials_emits_bare_url(self):
        self.assertEqual(
            build_rtsp_url("1.2.3.4", 554, "/live"),
            "rtsp://1.2.3.4:554/live",
        )

    def test_empty_string_credentials_are_treated_as_none(self):
        # The explicit ("" , "") guard must NOT produce a ":@" authority.
        self.assertEqual(
            build_rtsp_url("1.2.3.4", 554, "/live", username="", password=""),
            "rtsp://1.2.3.4:554/live",
        )

    def test_username_only_still_builds_authority(self):
        # password defaults to None -> encoded as empty, colon retained.
        self.assertEqual(
            build_rtsp_url("1.2.3.4", 554, "/", username="admin"),
            "rtsp://admin:@1.2.3.4:554/",
        )

    def test_special_characters_are_percent_encoded(self):
        # Regression guard: a password containing '@', ':' or '/' must be
        # percent-encoded, otherwise the authority parses to the wrong host.
        url = build_rtsp_url(
            "10.0.0.5", 8554, "/stream",
            username="ad@min", password="p:s/w@rd",
        )
        self.assertEqual(
            url,
            "rtsp://ad%40min:p%3As%2Fw%40rd@10.0.0.5:8554/stream",
        )
        # The literal, un-encoded credential must never leak into the URL.
        self.assertNotIn("p:s/w@rd", url)

    def test_default_port_and_path(self):
        self.assertEqual(build_rtsp_url("host"), "rtsp://host:554/")


class AddRtspAuthTests(unittest.TestCase):
    def test_injects_encoded_credentials(self):
        self.assertEqual(
            add_rtsp_auth("rtsp://1.2.3.4:554/live", "admin", "p@ss"),
            "rtsp://admin:p%40ss@1.2.3.4:554/live",
        )

    def test_preserves_query_string(self):
        self.assertEqual(
            add_rtsp_auth("rtsp://1.2.3.4:554/live?ch=1&sub=0", "u", "p"),
            "rtsp://u:p@1.2.3.4:554/live?ch=1&sub=0",
        )

    def test_defaults_port_to_554_when_absent(self):
        self.assertEqual(
            add_rtsp_auth("rtsp://1.2.3.4/cam", "u", "p"),
            "rtsp://u:p@1.2.3.4:554/cam",
        )


class ParseRtspUrlTests(unittest.TestCase):
    def test_round_trips_components(self):
        parsed = parse_rtsp_url("rtsp://user:pass@1.2.3.4:554/live?ch=1")
        self.assertEqual(parsed["host"], "1.2.3.4")
        self.assertEqual(parsed["port"], 554)
        self.assertEqual(parsed["path"], "/live?ch=1")
        self.assertEqual(parsed["username"], "user")
        self.assertEqual(parsed["password"], "pass")

    def test_missing_credentials_are_none(self):
        parsed = parse_rtsp_url("rtsp://1.2.3.4:554/live")
        self.assertIsNone(parsed["username"])
        self.assertIsNone(parsed["password"])

    def test_empty_path_defaults_to_slash(self):
        self.assertEqual(parse_rtsp_url("rtsp://1.2.3.4:554")["path"], "/")


class ParseContentLengthTests(unittest.TestCase):
    def test_valid_value(self):
        self.assertEqual(_parse_content_length({"content-length": "123"}), 123)

    def test_missing_header_returns_zero(self):
        self.assertEqual(_parse_content_length({}), 0)

    def test_non_numeric_returns_zero(self):
        self.assertEqual(_parse_content_length({"content-length": "not-a-number"}), 0)

    def test_negative_returns_zero(self):
        self.assertEqual(_parse_content_length({"content-length": "-5"}), 0)

    def test_oversized_value_is_capped(self):
        self.assertEqual(
            _parse_content_length({"content-length": "9999999999"}),
            MAX_RTSP_BODY_BYTES,
        )

    def test_whitespace_is_stripped(self):
        self.assertEqual(_parse_content_length({"content-length": "  42  "}), 42)


if __name__ == "__main__":
    unittest.main()

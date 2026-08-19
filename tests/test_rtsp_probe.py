"""
Tests for ``pwneye.core.network.rtsp.probe_rtsp_url`` wire behaviour.

Credentials belong in the Authorization header, never in the request-URI. These
tests capture what ``probe_rtsp_url`` actually puts on the wire (the request-URI
passed to ``_send_rtsp_request`` and the URI fed into the Digest hash) and assert
no ``user:pass@`` ever appears there. The socket layer is mocked, so the suite
stays hermetic.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import unittest
from unittest import mock

from pwneye.core.network import rtsp
from pwneye.core.network.rtsp import RtspResponse


def _response(status_code: int, headers=None) -> RtspResponse:
    return RtspResponse(
        status_code=status_code,
        reason="OK" if status_code == 200 else "Unauthorized",
        headers=headers or {},
        body="",
    )


class RequestUriHasNoCredentialsTests(unittest.TestCase):
    CRED_URL = "rtsp://admin:s3cr3t@192.0.2.10:554/live?channel=1"

    def test_unauthenticated_probe_strips_userinfo(self):
        sent = {}

        def fake_send(*, host, port, method, url, timeout, headers, stop_event=None):
            sent["url"] = url
            return _response(200)

        with mock.patch.object(rtsp, "_send_rtsp_request", side_effect=fake_send):
            result = rtsp.probe_rtsp_url(self.CRED_URL)

        self.assertTrue(result.stream_available)
        self.assertEqual(sent["url"], "rtsp://192.0.2.10:554/live?channel=1")
        self.assertNotIn("@", sent["url"])
        self.assertNotIn("s3cr3t", sent["url"])

    def test_digest_uri_and_retry_strip_userinfo(self):
        # First response challenges with Digest; the retry must reuse a clean
        # request-URI, and the Digest header's uri="..." must match it.
        calls = []
        challenge = {
            "www-authenticate": 'Digest realm="cam", nonce="abc123"',
        }

        def fake_send(*, host, port, method, url, timeout, headers, stop_event=None):
            calls.append({"url": url, "headers": dict(headers)})
            if len(calls) == 1:
                return _response(401, challenge)
            return _response(200)

        with mock.patch.object(rtsp, "_send_rtsp_request", side_effect=fake_send):
            result = rtsp.probe_rtsp_url(self.CRED_URL)

        self.assertTrue(result.credentials_valid)
        self.assertEqual(len(calls), 2)

        # Both request-URIs are credential-free.
        for call in calls:
            self.assertNotIn("@", call["url"])
            self.assertNotIn("s3cr3t", call["url"])

        # The Digest header hashes over the same clean request-URI.
        authorization = calls[1]["headers"]["Authorization"]
        self.assertIn('uri="rtsp://192.0.2.10:554/live?channel=1"', authorization)
        self.assertNotIn("s3cr3t", authorization)


if __name__ == "__main__":
    unittest.main()

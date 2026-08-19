"""
Pure-function tests for the credential/authorization builders in
``pwneye.core.network.rtsp``: Basic auth, WWW-Authenticate parsing, and the
RFC 2617 Digest response.

Digest is the interesting one: HA1/HA2 ordering, the qop branch, and the
"unsupported algorithm / bad qop / missing field -> None" rejection paths are
exactly where a subtle transposition silently breaks every authenticated probe.
The qop branch mixes in a random cnonce, so that one case patches ``os.urandom``
to make the output deterministic.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import hashlib
import unittest
from unittest import mock

from pwneye.core.network import rtsp
from pwneye.core.network.rtsp import (
    _build_basic_authorization,
    _build_digest_authorization,
    _hash_md5,
    _parse_www_authenticate,
)


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


class BasicAuthTests(unittest.TestCase):
    def test_golden_vector(self):
        # Locks the wire format: "Basic " + base64("user:pass").
        self.assertEqual(
            _build_basic_authorization("admin", "12345"),
            "Basic YWRtaW46MTIzNDU=",
        )

    def test_colon_separator_and_order(self):
        # username first, single colon separator, password second.
        self.assertEqual(
            _build_basic_authorization("u", "p"),
            "Basic " + __import__("base64").b64encode(b"u:p").decode(),
        )

    def test_hash_md5_helper(self):
        self.assertEqual(_hash_md5("abc"), _md5("abc"))


class ParseWwwAuthenticateTests(unittest.TestCase):
    def test_digest_scheme_and_quoted_params(self):
        scheme, params = _parse_www_authenticate(
            'Digest realm="IPCamera", nonce="abc123", qop="auth"'
        )
        self.assertEqual(scheme, "digest")
        self.assertEqual(params["realm"], "IPCamera")
        self.assertEqual(params["nonce"], "abc123")
        self.assertEqual(params["qop"], "auth")

    def test_bare_unquoted_value(self):
        scheme, params = _parse_www_authenticate("Digest realm=\"x\", algorithm=MD5")
        self.assertEqual(scheme, "digest")
        self.assertEqual(params["algorithm"], "MD5")

    def test_basic_scheme(self):
        scheme, params = _parse_www_authenticate('Basic realm="cam"')
        self.assertEqual(scheme, "basic")
        self.assertEqual(params["realm"], "cam")

    def test_empty_header(self):
        self.assertEqual(_parse_www_authenticate(""), (None, {}))


class DigestAuthTests(unittest.TestCase):
    PARAMS = {"realm": "IPCamera", "nonce": "abc123"}
    USER, PW = "admin", "1234"
    METHOD, URI = "DESCRIBE", "rtsp://1.2.3.4:554/"

    def _expected_ha1_ha2(self):
        ha1 = _md5(f"{self.USER}:{self.PARAMS['realm']}:{self.PW}")
        ha2 = _md5(f"{self.METHOD}:{self.URI}")
        return ha1, ha2

    def test_non_qop_response_is_deterministic(self):
        header = _build_digest_authorization(
            self.USER, self.PW, self.METHOD, self.URI, dict(self.PARAMS)
        )
        self.assertIsNotNone(header)
        self.assertTrue(header.startswith("Digest "))
        self.assertIn('username="admin"', header)
        self.assertIn('realm="IPCamera"', header)
        self.assertIn('uri="rtsp://1.2.3.4:554/"', header)
        # Golden response value computed independently from the RFC formula.
        self.assertIn('response="facffdbc0f4dc43bcb75975cfd82535b"', header)
        # No qop -> no nc/cnonce fields.
        self.assertNotIn("nc=", header)
        self.assertNotIn("cnonce=", header)

    def test_qop_auth_response_with_patched_cnonce(self):
        params = dict(self.PARAMS)
        params["qop"] = "auth"
        # cnonce = md5(os.urandom(16))[:16]; pin urandom so md5 -> known prefix.
        fixed = b"\x00" * 16
        expected_cnonce = hashlib.md5(fixed).hexdigest()[:16]
        with mock.patch.object(rtsp.os, "urandom", return_value=fixed):
            header = _build_digest_authorization(
                self.USER, self.PW, self.METHOD, self.URI, params
            )
        self.assertIsNotNone(header)
        self.assertIn('qop="auth"', header)
        self.assertIn("nc=00000001", header)
        self.assertIn(f'cnonce="{expected_cnonce}"', header)

        ha1, ha2 = self._expected_ha1_ha2()
        expected = _md5(f"{ha1}:{self.PARAMS['nonce']}:00000001:{expected_cnonce}:auth:{ha2}")
        self.assertIn(f'response="{expected}"', header)

    def test_opaque_is_echoed_back(self):
        params = dict(self.PARAMS)
        params["opaque"] = "deadbeef"
        header = _build_digest_authorization(
            self.USER, self.PW, self.METHOD, self.URI, params
        )
        self.assertIn('opaque="deadbeef"', header)

    def test_missing_realm_or_nonce_returns_none(self):
        self.assertIsNone(
            _build_digest_authorization("u", "p", "DESCRIBE", "/", {"nonce": "x"})
        )
        self.assertIsNone(
            _build_digest_authorization("u", "p", "DESCRIBE", "/", {"realm": "x"})
        )

    def test_non_md5_algorithm_is_rejected(self):
        params = dict(self.PARAMS)
        params["algorithm"] = "SHA-256"
        self.assertIsNone(
            _build_digest_authorization("u", "p", "DESCRIBE", "/", params)
        )

    def test_unsupported_qop_token_is_rejected(self):
        params = dict(self.PARAMS)
        params["qop"] = "auth-int"
        self.assertIsNone(
            _build_digest_authorization("u", "p", "DESCRIBE", "/", params)
        )

    def test_comma_separated_qop_prefers_auth(self):
        params = dict(self.PARAMS)
        params["qop"] = "auth,auth-int"
        header = _build_digest_authorization(
            self.USER, self.PW, self.METHOD, self.URI, params
        )
        self.assertIsNotNone(header)
        self.assertIn('qop="auth"', header)


if __name__ == "__main__":
    unittest.main()

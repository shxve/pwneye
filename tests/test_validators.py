"""
Pure-function tests for the CLI argument validators in
``pwneye.core.utils.validators``.

These run at argparse time and are the first gate on user input: a port, a
timeout, a thread count, or a target host. The numeric validators reject
non-positive / out-of-range values (a zero timeout previously made every probe
fail instantly, per CHANGELOG 1.4.0), and the host validator is deliberately
lenient at the CLI layer — real resolution happens at connect time.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import unittest

from pwneye.core.utils import validators as v


class ValidateIpOrDomainTests(unittest.TestCase):
    def test_accepts_ipv4(self):
        self.assertEqual(v.validate_ip_or_domain("1.2.3.4"), "1.2.3.4")

    def test_accepts_fqdn_and_short_hostname(self):
        self.assertEqual(v.validate_ip_or_domain("sub.example.com"), "sub.example.com")
        self.assertEqual(v.validate_ip_or_domain("localhost"), "localhost")

    def test_trailing_dot_is_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_ip_or_domain("example.com.")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_ip_or_domain("")

    def test_leading_hyphen_is_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_ip_or_domain("-bad.com")

    def test_whitespace_and_underscore_are_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_ip_or_domain("a b")
        with self.assertRaises(ValueError):
            v.validate_ip_or_domain("under_score.com")

    def test_numeric_out_of_range_is_accepted_at_cli_layer(self):
        # Documented leniency: "999.1.1.1" is hostname-shaped, so the CLI
        # validator lets it through; connect-time resolution is the real gate.
        self.assertEqual(v.validate_ip_or_domain("999.1.1.1"), "999.1.1.1")


class ValidatePortTests(unittest.TestCase):
    def test_valid_bounds(self):
        self.assertEqual(v.validate_port("1"), 1)
        self.assertEqual(v.validate_port("65535"), 65535)

    def test_returns_int(self):
        self.assertIsInstance(v.validate_port("8080"), int)

    def test_zero_and_over_max_are_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_port("0")
        with self.assertRaises(ValueError):
            v.validate_port("65536")

    def test_negative_and_non_numeric_are_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_port("-1")
        with self.assertRaises(ValueError):
            v.validate_port("abc")


class ValidateTimeoutTests(unittest.TestCase):
    def test_positive_value(self):
        self.assertEqual(v.validate_timeout("5"), 5)

    def test_zero_is_rejected(self):
        # Regression guard: a zero timeout made every probe fail instantly.
        with self.assertRaises(ValueError):
            v.validate_timeout("0")

    def test_negative_and_non_numeric_are_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_timeout("-3")
        with self.assertRaises(ValueError):
            v.validate_timeout("abc")


class ValidateThreadsTests(unittest.TestCase):
    def test_minimum_is_one(self):
        self.assertEqual(v.validate_threads("1"), 1)
        with self.assertRaises(ValueError):
            v.validate_threads("0")

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_threads("abc")


class ValidateMaxChannelsTests(unittest.TestCase):
    def test_minimum_is_one(self):
        self.assertEqual(v.validate_max_channels("1"), 1)
        with self.assertRaises(ValueError):
            v.validate_max_channels("0")

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            v.validate_max_channels("abc")


if __name__ == "__main__":
    unittest.main()

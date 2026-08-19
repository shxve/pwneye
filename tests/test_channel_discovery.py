"""
Regression tests for RTSP channel enumeration in ``pwneye.core.engine``.

The focus is termination: ``_discover_rtsp_channels`` must always stop on its
own when a target has no further channels. A previous version's continuation
loop only ever incremented each family cursor and never cleared it, so without
``--max-channels`` (or a manual CTRL-C) it probed ever-increasing channel
numbers forever. The probe here is mocked, so the whole suite stays hermetic
(no sockets); a hard probe-count ceiling turns any re-introduced infinite loop
into a fast, loud failure instead of a hang.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import argparse
import unittest
from unittest import mock

from pwneye.core import engine
from pwneye.core.types import RtspAttempt, RtspProbeResult


# Generous ceiling: a correct run over a single-channel target makes only a few
# dozen probes; a broken (unbounded) run trips this quickly instead of hanging.
_PROBE_CEILING = 1000


class _SilentTUI:
    """Minimal TUI stub: swallow output, satisfy the live-status calls."""

    def info(self, *args, **kwargs) -> None: ...
    def info2(self, *args, **kwargs) -> None: ...
    def success(self, *args, **kwargs) -> None: ...
    def warning(self, *args, **kwargs) -> None: ...
    def start_live(self, *args, **kwargs) -> None: ...
    def update_live(self, *args, **kwargs) -> None: ...
    def stop_live(self, *args, **kwargs) -> None: ...


def _failing_probe_result(url: str) -> RtspProbeResult:
    return RtspProbeResult(
        url=url,
        status_code=404,
        reason="Not Found",
        auth_scheme=None,
        credentials_valid=True,
        path_valid=False,
        stream_available=False,
    )


def _base_attempt() -> RtspAttempt:
    path = "/live?channel=1"
    return RtspAttempt(
        host="192.0.2.10",
        port=554,
        path=path,
        username="",
        password="",
        protocol="tcp",
        url=f"rtsp://192.0.2.10:554{path}",
    )


class DiscoverRtspChannelsTerminationTests(unittest.TestCase):
    def test_all_channels_missing_still_terminates(self):
        args = argparse.Namespace(timeout=1, max_channels=None)
        probe_calls = {"count": 0}

        def fake_probe(attempt, *, timeout):
            probe_calls["count"] += 1
            if probe_calls["count"] > _PROBE_CEILING:
                raise AssertionError(
                    "channel enumeration did not terminate "
                    f"(>{_PROBE_CEILING} probes)"
                )
            return _failing_probe_result(attempt.url)

        with mock.patch.object(engine, "_probe_rtsp_attempt", side_effect=fake_probe):
            entries, interrupted = engine._discover_rtsp_channels(
                _base_attempt(), args, _SilentTUI()
            )

        # Only the seed channel survives when every probe misses.
        self.assertEqual([entry.channel for entry in entries], [1])
        self.assertFalse(interrupted)
        self.assertLessEqual(probe_calls["count"], _PROBE_CEILING)

    def test_max_channels_cap_is_respected(self):
        # Every probe succeeds; the cap must bound how many channels are kept.
        args = argparse.Namespace(timeout=1, max_channels=3)

        def fake_probe(attempt, *, timeout):
            return RtspProbeResult(
                url=attempt.url,
                status_code=200,
                reason="OK",
                auth_scheme=None,
                credentials_valid=True,
                path_valid=True,
                stream_available=True,
            )

        with mock.patch.object(engine, "_probe_rtsp_attempt", side_effect=fake_probe):
            entries, _ = engine._discover_rtsp_channels(
                _base_attempt(), args, _SilentTUI()
            )

        self.assertEqual(len(entries), 3)


if __name__ == "__main__":
    unittest.main()

"""
Pure-function tests for the ffmpeg / ffplay command builders.

These assemble the argv lists handed to ffmpeg (capture + finalize) and ffplay
(live preview). The builders are pure, but they carry two properties worth
pinning:

  1. Argv safety. The RTSP URL — which embeds percent-encoded credentials and
     '@' / ':' / '%' — must be a single list element, never a shell string, so
     it can't be word-split or injected. Every spawn site uses an argv list
     (no shell=True), and these tests lock the URL to exactly one element.

  2. Anti-drift. The exact same two ffmpeg builders exist twice: the public
     ``media.build_ffmpeg_*`` (used by the GUI viewer via QProcess) and the
     private ``engine._build_ffmpeg_*`` (used by the CLI subprocess path). A
     flag/encoding fix applied to one copy but not the other would silently
     desync CLI and GUI recordings. ``FfmpegBuilderParityTests`` fails loudly
     if the two copies ever diverge.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import unittest
from pathlib import Path

from pwneye.core.storage.media import (
    build_ffmpeg_capture_cmd,
    build_ffmpeg_finalize_cmd,
)
from pwneye.core.engine import (
    _build_ffmpeg_capture_cmd,
    _build_ffmpeg_finalize_cmd,
    _build_ffplay_cmd,
)
from pwneye.core.types import RtspAttempt

# A URL carrying percent-encoded credentials with the characters that would
# break a shell string if this were ever concatenated instead of argv-listed.
CRED_URL = "rtsp://ad%40min:p%3As%2Fw%40rd@10.0.0.5:8554/live?ch=1"


def _attempt(url=CRED_URL, protocol="tcp"):
    return RtspAttempt(
        host="10.0.0.5", port=8554, path="/live",
        username="ad@min", password="p:s/w@rd",
        protocol=protocol, url=url,
    )


def _pair_follows(cmd, flag, value):
    """True if `flag` appears immediately before `value` in the argv list."""
    for i, item in enumerate(cmd):
        if item == flag and i + 1 < len(cmd) and cmd[i + 1] == value:
            return True
    return False


class CaptureCmdTests(unittest.TestCase):
    def test_is_a_plain_string_argv_list(self):
        cmd = build_ffmpeg_capture_cmd(_attempt(), Path("/out/clip.capture.mkv"))
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(x, str) for x in cmd))

    def test_program_is_ffmpeg(self):
        # Element 0 is "ffmpeg"; the GUI caller slices it off ([1:]) for QProcess.
        cmd = build_ffmpeg_capture_cmd(_attempt(), Path("/out/clip.capture.mkv"))
        self.assertEqual(cmd[0], "ffmpeg")

    def test_transport_reflects_protocol(self):
        self.assertTrue(_pair_follows(
            build_ffmpeg_capture_cmd(_attempt(protocol="tcp"), Path("/o.mkv")),
            "-rtsp_transport", "tcp",
        ))
        self.assertTrue(_pair_follows(
            build_ffmpeg_capture_cmd(_attempt(protocol="udp"), Path("/o.mkv")),
            "-rtsp_transport", "udp",
        ))

    def test_input_url_is_a_single_argv_element(self):
        cmd = build_ffmpeg_capture_cmd(_attempt(), Path("/o.mkv"))
        # Exactly one element equal to the whole URL — never split or joined.
        self.assertEqual(cmd.count(CRED_URL), 1)
        self.assertEqual(cmd[cmd.index("-i") + 1], CRED_URL)

    def test_uses_tolerant_matroska_container_and_overwrite(self):
        cmd = build_ffmpeg_capture_cmd(_attempt(), Path("/out/clip.capture.mkv"))
        self.assertTrue(_pair_follows(cmd, "-f", "matroska"))
        self.assertIn("-y", cmd)

    def test_output_path_is_last_element(self):
        temp = Path("/out/clip.capture.mkv")
        cmd = build_ffmpeg_capture_cmd(_attempt(), temp)
        self.assertEqual(cmd[-1], str(temp))


class FinalizeCmdTests(unittest.TestCase):
    TEMP = Path("/out/clip.capture.mkv")
    OUT = Path("/out/clip.mp4")

    def test_default_mode_is_copy(self):
        cmd = build_ffmpeg_finalize_cmd(self.TEMP, self.OUT)
        self.assertTrue(_pair_follows(cmd, "-c", "copy"))
        self.assertIn("+faststart", cmd)
        self.assertNotIn("libx264", cmd)

    def test_copy_mode_input_and_output(self):
        cmd = build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode="copy")
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.TEMP))
        self.assertEqual(cmd[-1], str(self.OUT))

    def test_transcode_mode_uses_h264_and_aac(self):
        cmd = build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode="transcode")
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertTrue(_pair_follows(cmd, "-c:v", "libx264"))
        self.assertIn("+faststart", cmd)

    def test_video_only_transcode_drops_audio(self):
        cmd = build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode="video_only_transcode")
        self.assertIn("-an", cmd)
        self.assertIn("libx264", cmd)
        self.assertNotIn("aac", cmd)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode="bogus")


class FfplayCmdTests(unittest.TestCase):
    def test_program_and_transport(self):
        cmd = _build_ffplay_cmd(_attempt(protocol="udp"))
        self.assertEqual(cmd[0], "ffplay")
        self.assertTrue(_pair_follows(cmd, "-rtsp_transport", "udp"))

    def test_input_url_is_single_element_and_last(self):
        cmd = _build_ffplay_cmd(_attempt())
        self.assertEqual(cmd.count(CRED_URL), 1)
        self.assertEqual(cmd[cmd.index("-i") + 1], CRED_URL)

    def test_low_latency_flags_present(self):
        cmd = _build_ffplay_cmd(_attempt())
        self.assertTrue(_pair_follows(cmd, "-fflags", "nobuffer"))
        self.assertTrue(_pair_follows(cmd, "-flags", "low_delay"))


class FfmpegBuilderParityTests(unittest.TestCase):
    """The CLI (engine) and GUI (media) copies of the ffmpeg builders must be
    identical. If they diverge, one recording path silently gets a fix the
    other doesn't. Guard both copies here until the duplication is removed."""

    TEMP = Path("/out/clip.capture.mkv")
    OUT = Path("/out/clip.mp4")

    def test_capture_cmd_copies_match(self):
        attempt = _attempt()
        self.assertEqual(
            _build_ffmpeg_capture_cmd(attempt, self.TEMP),
            build_ffmpeg_capture_cmd(attempt, self.TEMP),
        )

    def test_finalize_cmd_copies_match_for_every_mode(self):
        for mode in ("copy", "transcode", "video_only_transcode"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    _build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode=mode),
                    build_ffmpeg_finalize_cmd(self.TEMP, self.OUT, mode=mode),
                )


if __name__ == "__main__":
    unittest.main()

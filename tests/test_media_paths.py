"""
Pure-function tests for media output-path resolution in
``pwneye.core.storage.media``.

These decide where recordings and snapshots land. The two properties that
matter: a target string must be sanitized before it becomes a directory name
(so a path-traversal-shaped target can't escape the output tree), and an
existing file must never be silently overwritten (dedup appends a numeric
suffix and reports the conflict). Tests run against a temp base dir; the
recording/snapshot wrappers patch the config dirs so nothing touches
``~/.pwneye``.

Run (from the pwneye repo root):
    venv/bin/python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pwneye.core.storage import media


class SanitizeTargetTests(unittest.TestCase):
    def test_plain_ip_is_unchanged(self):
        self.assertEqual(media.sanitize_target_for_path("1.2.3.4"), "1.2.3.4")

    def test_port_colon_becomes_underscore(self):
        self.assertEqual(media.sanitize_target_for_path("1.2.3.4:8080"), "1.2.3.4_8080")

    def test_path_traversal_is_neutralized(self):
        # Slashes and leading dots must not survive into a directory name.
        result = media.sanitize_target_for_path("../evil")
        self.assertEqual(result, "evil")
        self.assertNotIn("/", result)

    def test_spaces_collapse_and_strip(self):
        self.assertEqual(media.sanitize_target_for_path("  a b  "), "a_b")

    def test_all_special_falls_back(self):
        self.assertEqual(media.sanitize_target_for_path("..."), "unknown_target")
        self.assertEqual(media.sanitize_target_for_path("///"), "unknown_target")


class BuildTempRecordingPathTests(unittest.TestCase):
    def test_swaps_suffix_to_capture_mkv(self):
        self.assertEqual(
            media.build_temp_recording_path(Path("/out/clip.mp4")),
            Path("/out/clip.capture.mkv"),
        )


class ResolveMediaOutputPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, filename):
        return media.resolve_media_output_path_with_notice(
            filename, base_dir=self.base, target="1.2.3.4", suffix=".mp4",
        )

    def test_none_filename_uses_timestamp_under_target_dir(self):
        path, conflict = self._resolve(None)
        self.assertIsNone(conflict)
        self.assertEqual(path.parent, self.base / "1.2.3.4")
        self.assertTrue(path.name.endswith(".mp4"))

    def test_relative_name_gains_suffix_and_target_dir(self):
        path, conflict = self._resolve("clip")
        self.assertEqual(path, self.base / "1.2.3.4" / "clip.mp4")
        self.assertIsNone(conflict)

    def test_existing_suffix_is_preserved(self):
        path, _ = self._resolve("clip.mp4")
        self.assertEqual(path.name, "clip.mp4")

    def test_absolute_path_bypasses_target_dir(self):
        abs_target = self.base / "elsewhere" / "out"
        path, conflict = media.resolve_media_output_path_with_notice(
            str(abs_target), base_dir=self.base, target="1.2.3.4", suffix=".mp4",
        )
        self.assertEqual(path, abs_target.with_suffix(".mp4"))
        self.assertIsNone(conflict)

    def test_user_home_is_expanded(self):
        path, _ = media.resolve_media_output_path_with_notice(
            "~/clip", base_dir=self.base, target="t", suffix=".mp4",
        )
        self.assertFalse(str(path).startswith("~"))

    def test_conflict_gets_numeric_suffix_and_is_reported(self):
        target_dir = self.base / "1.2.3.4"
        target_dir.mkdir(parents=True)
        (target_dir / "clip.mp4").write_text("existing", encoding="utf-8")

        path, conflict = self._resolve("clip.mp4")
        self.assertEqual(path, target_dir / "clip1.mp4")
        self.assertEqual(conflict, target_dir / "clip.mp4")

    def test_second_conflict_increments_again(self):
        target_dir = self.base / "1.2.3.4"
        target_dir.mkdir(parents=True)
        (target_dir / "clip.mp4").write_text("a", encoding="utf-8")
        (target_dir / "clip1.mp4").write_text("b", encoding="utf-8")

        path, _ = self._resolve("clip.mp4")
        self.assertEqual(path, target_dir / "clip2.mp4")


class RecordingAndSnapshotWrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(media, "RECORDINGS_DIR", base / "recordings"),
            mock.patch.object(media, "SNAPSHOTS_DIR", base / "snapshots"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_recording_uses_mp4_under_target_dir(self):
        path = media.resolve_recording_path("clip", "1.2.3.4")
        self.assertEqual(path.name, "clip.mp4")
        self.assertEqual(path.parent, media.RECORDINGS_DIR / "1.2.3.4")

    def test_snapshot_uses_jpg_under_target_dir(self):
        path = media.resolve_snapshot_path("shot", "1.2.3.4")
        self.assertEqual(path.name, "shot.jpg")
        self.assertEqual(path.parent, media.SNAPSHOTS_DIR / "1.2.3.4")

    def test_recording_default_name_is_timestamped_mp4(self):
        path = media.resolve_recording_path(None, "1.2.3.4")
        self.assertTrue(path.name.endswith(".mp4"))
        self.assertEqual(path.parent, media.RECORDINGS_DIR / "1.2.3.4")


if __name__ == "__main__":
    unittest.main()

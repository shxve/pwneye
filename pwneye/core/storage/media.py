from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pwneye.config import RECORDINGS_DIR, SNAPSHOTS_DIR
from pwneye.core.types import RtspAttempt


def sanitize_target_for_path(target: str) -> str:
    """
    Sanitize a target string so it can be safely used as a directory name.
    """
    sanitized = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in target.strip()
    )
    return sanitized.strip("._") or "unknown_target"


def _deduplicate_output_path(path: Path) -> Path:
    """
    Return a non-conflicting path by appending an incrementing numeric suffix.
    """
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    candidate = path
    index = 1

    while candidate.exists():
        candidate = path.with_name(f"{stem}{index}{suffix}")
        index += 1

    return candidate


def resolve_media_output_path_with_notice(
    filename: str | None,
    *,
    base_dir: Path,
    target: str,
    suffix: str,
) -> tuple[Path, Path | None]:
    """
    Resolve a media output path and report the original conflicting path, if any.
    """
    target_dir = base_dir / sanitize_target_for_path(target)

    if not filename:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return target_dir / f"{timestamp}{suffix}", None

    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = target_dir / path

    if path.suffix == "":
        path = path.with_suffix(suffix)

    resolved = _deduplicate_output_path(path)
    conflict = path if resolved != path else None
    return resolved, conflict


def resolve_media_output_path(
    filename: str | None,
    *,
    base_dir: Path,
    target: str,
    suffix: str,
) -> Path:
    """
    Resolve a media output path using the tool runtime directories.
    """
    resolved, _ = resolve_media_output_path_with_notice(
        filename,
        base_dir=base_dir,
        target=target,
        suffix=suffix,
    )
    return resolved


def resolve_recording_path(filename: str | None, target: str) -> Path:
    """
    Resolve the recording output path.
    """
    return resolve_media_output_path(
        filename,
        base_dir=RECORDINGS_DIR,
        target=target,
        suffix=".mp4",
    )


def resolve_recording_path_with_notice(filename: str | None, target: str) -> tuple[Path, Path | None]:
    """
    Resolve the recording output path and report any conflicting original path.
    """
    return resolve_media_output_path_with_notice(
        filename,
        base_dir=RECORDINGS_DIR,
        target=target,
        suffix=".mp4",
    )


def resolve_snapshot_path(filename: str | None, target: str) -> Path:
    """
    Resolve the snapshot output path.
    """
    return resolve_media_output_path(
        filename,
        base_dir=SNAPSHOTS_DIR,
        target=target,
        suffix=".jpg",
    )


def resolve_snapshot_path_with_notice(filename: str | None, target: str) -> tuple[Path, Path | None]:
    """
    Resolve the snapshot output path and report any conflicting original path.
    """
    return resolve_media_output_path_with_notice(
        filename,
        base_dir=SNAPSHOTS_DIR,
        target=target,
        suffix=".jpg",
    )


def build_temp_recording_path(output_path: Path) -> Path:
    """
    Build the temporary recording path used during capture.
    """
    return output_path.with_suffix(".capture.mkv")


def build_ffmpeg_capture_cmd(
    attempt: RtspAttempt,
    temp_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command used to capture an RTSP stream to a tolerant container.
    """
    return [
        "ffmpeg",
        "-nostats",
        "-loglevel", "error",
        "-rtsp_transport", attempt.protocol,
        "-analyzeduration", "10M",
        "-probesize", "10M",
        "-y",
        "-i", attempt.url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        "-f", "matroska",
        str(temp_path),
    ]


def build_ffmpeg_finalize_cmd(
    temp_path: Path,
    output_path: Path,
    mode: str = "copy",
) -> list[str]:
    """
    Build the ffmpeg command used to finalize the temporary recording into MP4.
    """
    cmd = [
        "ffmpeg",
        "-nostats",
        "-loglevel", "error",
        "-y",
        "-i", str(temp_path),
    ]

    if mode == "copy":
        cmd.extend([
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
        ])
    elif mode == "transcode":
        cmd.extend([
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-movflags", "+faststart",
        ])
    elif mode == "video_only_transcode":
        cmd.extend([
            "-map", "0:v:0",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ])
    else:
        raise ValueError(f"Unsupported ffmpeg finalize mode: {mode}")

    cmd.append(str(output_path))
    return cmd

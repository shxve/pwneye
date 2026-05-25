from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from pathlib import Path

from pwneye.core.types import RtspAttempt


def _read_process_error(log_path: Path) -> str | None:
    """
    Return the last meaningful line captured from a viewer bootstrap log.
    """
    if not log_path.exists():
        return None

    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return None

    if not lines:
        return None

    return lines[-1]


def open_preview(
    attempts: list[RtspAttempt],
) -> tuple[bool, str | None]:
    """
    Launch the dedicated RTSP viewer in a detached process.
    """
    if not attempts:
        return False, "no RTSP streams were provided"

    payload_path = Path(
        tempfile.mkstemp(prefix="pwneye-viewer-", suffix=".json")[1]
    )
    stderr_path = Path(
        tempfile.mkstemp(prefix="pwneye-viewer-", suffix=".log")[1]
    )

    payload = [
        {
            "host": attempt.host,
            "port": attempt.port,
            "path": attempt.path,
            "username": attempt.username,
            "password": attempt.password,
            "protocol": attempt.protocol,
            "url": attempt.url,
        }
        for attempt in attempts
    ]
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    env = os.environ.copy()
    env.setdefault("QT_LOGGING_RULES", "*.ffmpeg.*=false;*.multimedia.*=false")

    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pwneye.core.viewer.app",
                str(payload_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            start_new_session=True,
            env=env,
        )
    finally:
        stderr_handle.close()

    time.sleep(0.8)
    exit_code = process.poll()
    if exit_code is None:
        stderr_path.unlink(missing_ok=True)
        return True, None

    payload_path.unlink(missing_ok=True)
    detail = _read_process_error(stderr_path)
    stderr_path.unlink(missing_ok=True)
    return False, detail or "the viewer exited unexpectedly"


def open_multi_preview(
    attempts: list[RtspAttempt],
) -> tuple[bool, str | None]:
    """
    Backward-compatible wrapper for the dedicated RTSP viewer launcher.
    """
    return open_preview(attempts)

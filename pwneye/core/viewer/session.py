from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from pathlib import Path

from pwneye.core.types import RtspAttempt, ViewerLaunchOptions, ViewerOnvifContext


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


def _build_payload(
    attempts: list[RtspAttempt],
    onvif_context: ViewerOnvifContext | None,
    launch_options: ViewerLaunchOptions | None,
) -> dict:
    return {
        "attempts": [
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
        ],
        "onvif": (
            {
                "host": onvif_context.host,
                "port": onvif_context.port,
                "username": onvif_context.username,
                "password": onvif_context.password,
                "ptz_supported": onvif_context.ptz_supported,
            }
            if onvif_context is not None
            else None
        ),
        "options": {
            "allow_recording": True if launch_options is None else launch_options.allow_recording,
        },
    }


def _launch_viewer_process(
    attempts: list[RtspAttempt],
    *,
    onvif_context: ViewerOnvifContext | None = None,
    launch_options: ViewerLaunchOptions | None = None,
    detached: bool,
) -> tuple[subprocess.Popen | None, Path | None, str | None]:
    if not attempts:
        return None, None, "no RTSP streams were provided"

    payload_path = Path(
        tempfile.mkstemp(prefix="pwneye-viewer-", suffix=".json")[1]
    )
    stderr_path = Path(
        tempfile.mkstemp(prefix="pwneye-viewer-", suffix=".log")[1]
    )

    payload_path.write_text(
        json.dumps(_build_payload(attempts, onvif_context, launch_options)),
        encoding="utf-8",
    )

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
            start_new_session=detached,
            env=env,
        )
    finally:
        stderr_handle.close()

    time.sleep(0.8)
    exit_code = process.poll()
    if exit_code is None:
        return process, stderr_path, None

    payload_path.unlink(missing_ok=True)
    detail = _read_process_error(stderr_path)
    stderr_path.unlink(missing_ok=True)
    return None, None, detail or "the viewer exited unexpectedly"


def open_preview(
    attempts: list[RtspAttempt],
    onvif_context: ViewerOnvifContext | None = None,
    launch_options: ViewerLaunchOptions | None = None,
) -> tuple[bool, str | None]:
    """
    Launch the dedicated RTSP viewer in a detached process.
    """
    process, stderr_path, detail = _launch_viewer_process(
        attempts,
        onvif_context=onvif_context,
        launch_options=launch_options,
        detached=True,
    )
    if process is None:
        return False, detail
    if stderr_path is not None:
        stderr_path.unlink(missing_ok=True)
    return True, None


def open_preview_managed(
    attempts: list[RtspAttempt],
    onvif_context: ViewerOnvifContext | None = None,
    launch_options: ViewerLaunchOptions | None = None,
) -> tuple[subprocess.Popen | None, Path | None, str | None]:
    """
    Launch the dedicated RTSP viewer in a managed process that the caller waits on.
    """
    return _launch_viewer_process(
        attempts,
        onvif_context=onvif_context,
        launch_options=launch_options,
        detached=False,
    )


def open_multi_preview(
    attempts: list[RtspAttempt],
    onvif_context: ViewerOnvifContext | None = None,
    launch_options: ViewerLaunchOptions | None = None,
) -> tuple[bool, str | None]:
    """
    Backward-compatible wrapper for the dedicated RTSP viewer launcher.
    """
    return open_preview(
        attempts,
        onvif_context=onvif_context,
        launch_options=launch_options,
    )

from __future__ import annotations

import json
import os
import sys

from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from pwneye.config import BASE_DIR
from pwneye.core.types import RtspAttempt, ViewerLaunchOptions, ViewerOnvifContext
from pwneye.core.viewer.client import MultiChannelViewer

APP_ICON_PATH = BASE_DIR / "data" / "app_icon.png"


def _load_payload(
    payload_path: Path,
) -> tuple[list[RtspAttempt], ViewerOnvifContext | None, ViewerLaunchOptions]:
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    attempts = [
        RtspAttempt(
            host=item["host"],
            port=int(item["port"]),
            path=item["path"],
            username=item["username"],
            password=item["password"],
            protocol=item["protocol"],
            url=item["url"],
        )
        for item in raw["attempts"]
    ]
    onvif_raw = raw.get("onvif")
    onvif_context = None
    if onvif_raw:
        onvif_context = ViewerOnvifContext(
            host=onvif_raw["host"],
            port=int(onvif_raw["port"]),
            username=onvif_raw["username"],
            password=onvif_raw["password"],
            ptz_supported=bool(onvif_raw.get("ptz_supported")),
        )
    options_raw = raw.get("options") or {}
    launch_options = ViewerLaunchOptions(
        allow_recording=bool(options_raw.get("allow_recording", True)),
    )
    return attempts, onvif_context, launch_options


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        return 1

    payload_path = Path(argv[0])
    try:
        attempts, onvif_context, launch_options = _load_payload(payload_path)
    finally:
        payload_path.unlink(missing_ok=True)

    os.environ.setdefault("QT_LOGGING_RULES", "*.ffmpeg.*=false;*.multimedia.*=false")

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(True)
    app.lastWindowClosed.connect(app.quit)
    app_icon = QIcon(str(APP_ICON_PATH)) if APP_ICON_PATH.exists() else None
    if app_icon is not None:
        app.setWindowIcon(app_icon)

    viewer = MultiChannelViewer(
        attempts,
        onvif_context=onvif_context,
        launch_options=launch_options,
    )
    if app_icon is not None:
        viewer.setWindowIcon(app_icon)
    viewer.show()
    viewer.start()

    if owns_app:
        exit_code = app.exec()
        os._exit(exit_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

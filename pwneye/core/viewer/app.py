from __future__ import annotations

import json
import os
import sys

from pathlib import Path

from PyQt6.QtWidgets import QApplication

from pwneye.core.types import RtspAttempt
from pwneye.core.viewer.client import MultiChannelViewer


def _load_attempts(payload_path: Path) -> list[RtspAttempt]:
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    return [
        RtspAttempt(
            host=item["host"],
            port=int(item["port"]),
            path=item["path"],
            username=item["username"],
            password=item["password"],
            protocol=item["protocol"],
            url=item["url"],
        )
        for item in raw
    ]


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        return 1

    payload_path = Path(argv[0])
    try:
        attempts = _load_attempts(payload_path)
    finally:
        payload_path.unlink(missing_ok=True)

    os.environ.setdefault("QT_LOGGING_RULES", "*.ffmpeg.*=false;*.multimedia.*=false")

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(True)
    app.lastWindowClosed.connect(app.quit)

    viewer = MultiChannelViewer(attempts)
    viewer.show()
    viewer.start()

    if owns_app:
        return app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

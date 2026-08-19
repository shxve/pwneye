from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QEvent, QPoint, QProcess, QPropertyAnimation, QParallelAnimationGroup, QTimer, QUrl, pyqtSignal, Qt
from PyQt6.QtGui import QCursor, QFont, QKeySequence, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from pwneye.config import VERSION, CODENAME
from pwneye.core.network import onvif as onvifnet
from pwneye.core.storage.media import (
    build_ffmpeg_capture_cmd,
    build_ffmpeg_finalize_cmd,
    build_temp_recording_path,
    resolve_recording_path,
    resolve_snapshot_path,
)
from pwneye.core.types import RtspAttempt, ViewerLaunchOptions, ViewerOnvifContext
from pwneye.core.viewer.layout import MosaicLayout, build_mosaic_layout

os.environ.setdefault(
    "QT_FFMPEG_PROTOCOL_WHITELIST",
    "file,crypto,rtsp,rtp,tcp,udp",
)


def _reserve_udp_port() -> int:
    """
    Reserve a free local UDP port for a relayed preview stream.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_relay_output_url(port: int) -> str:
    """
    Build the local UDP URL used by Qt to consume a relayed stream.
    """
    return f"udp://127.0.0.1:{port}?overrun_nonfatal=1&fifo_size=5000000"


def _build_ffmpeg_relay_cmd(
    attempt: RtspAttempt,
    port: int,
    *,
    compatibility_mode: bool = False,
) -> list[str]:
    """
    Build the ffmpeg command that relays one RTSP stream to localhost.
    """
    cmd = [
        "ffmpeg",
        "-nostats",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        attempt.protocol,
        "-analyzeduration",
        "10M",
        "-probesize",
        "10M",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-i",
        attempt.url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]

    if compatibility_mode:
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
            ]
        )
    else:
        cmd.extend(
            [
                "-c:v",
                "copy",
                "-c:a",
                "copy",
            ]
        )

    cmd.extend(
        [
            "-f",
            "mpegts",
            f"udp://127.0.0.1:{port}?pkt_size=1316",
        ]
    )
    return cmd


def _read_process_error(log_path: Path) -> str | None:
    """
    Return the last meaningful line captured from a relay stderr log.
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


def _build_ffmpeg_snapshot_cmd(
    attempt: RtspAttempt,
    output_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command used to capture a single frame from an RTSP stream.
    """
    return [
        "ffmpeg",
        "-nostats",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        attempt.protocol,
        "-timeout",
        "10000000",
        "-i",
        attempt.url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output_path),
    ]


@dataclass(frozen=True)
class ViewerStream:
    title: str
    source_attempt: RtspAttempt
    relay_url: str
    relay_port: int


class RelayStreamSession:
    def __init__(
        self,
        attempt: RtspAttempt,
        *,
        compatibility_mode: bool = False,
    ) -> None:
        self.port = _reserve_udp_port()
        self.url = _build_relay_output_url(self.port)
        self.stderr_path = Path(
            tempfile.mkstemp(prefix="pwneye-viewer-relay-", suffix=".log")[1]
        )
        self.process: subprocess.Popen | None = None
        self.cmd = _build_ffmpeg_relay_cmd(
            attempt,
            self.port,
            compatibility_mode=compatibility_mode,
        )

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
            )
        except OSError as exc:
            # ffmpeg missing or not executable: record the reason so the tile can
            # surface it via last_error() instead of crashing the viewer process.
            self.process = None
            try:
                stderr_handle.write(f"Unable to start ffmpeg: {exc}\n")
            except OSError:
                pass
        finally:
            stderr_handle.close()

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.process = None
            return

        try:
            self.process.kill()
            self.process.wait()
        finally:
            self.process = None

    def last_error(self) -> str | None:
        return _read_process_error(self.stderr_path)

    def cleanup(self) -> None:
        self.stop()
        self.stderr_path.unlink(missing_ok=True)


class ClickableVideoWidget(QVideoWidget):
    clicked = pyqtSignal()
    wheel_zoom = pyqtSignal(int)
    pinch_zoom = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._press_pos: QPoint | None = None
        self._drag_moved = False

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.globalPosition().toPoint()
            self._press_pos = pos
            self._drag_moved = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._drag_moved and self._press_pos is not None:
                release_pos = event.globalPosition().toPoint()
                if (release_pos - self._press_pos).manhattanLength() <= 6:
                    self.clicked.emit()
            self._press_pos = None
            self._drag_moved = False
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        if delta:
            self.wheel_zoom.emit(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.NativeGesture:
            gesture_type = getattr(event, "gestureType", lambda: None)()
            if gesture_type == Qt.NativeGestureType.ZoomNativeGesture:
                self.pinch_zoom.emit(float(getattr(event, "value", lambda: 0.0)()))
                event.accept()
                return True
        return super().event(event)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.clicked.emit()
        super().mousePressEvent(event)


class FocusVideoHost(QWidget):
    resized = pyqtSignal()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        self.resized.emit()
        super().resizeEvent(event)


class FocusInteractionOverlay(QWidget):
    wheel_zoom = pyqtSignal(int)
    pinch_zoom = pyqtSignal(float)
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        self.setMouseTracking(True)
        self._press_pos: QPoint | None = None

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            if self._press_pos is not None:
                release_pos = event.globalPosition().toPoint()
                if (release_pos - self._press_pos).manhattanLength() <= 6:
                    self.clicked.emit()
            self._press_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        if delta:
            self.wheel_zoom.emit(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.NativeGesture:
            gesture_type = getattr(event, "gestureType", lambda: None)()
            if gesture_type == Qt.NativeGestureType.ZoomNativeGesture:
                self.pinch_zoom.emit(float(getattr(event, "value", lambda: 0.0)()))
                event.accept()
                return True
        return super().event(event)


class ExitConfirmDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(320)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(
            """
            QDialog {
                background-color: #061015;
                color: #d8fff6;
                border: 1px solid #1a4c55;
                border-radius: 3px;
            }
            QLabel {
                color: #d8fff6;
            }
            QPushButton {
                background-color: #0d2027;
                color: #d8fff6;
                border: 1px solid #1a4c55;
                border-radius: 2px;
                padding: 6px 12px;
                font-weight: 600;
                min-width: 88px;
            }
            QPushButton:hover {
                background-color: #12313a;
                border-color: #28d7b5;
            }
            QPushButton#dangerButton {
                background-color: #2a1212;
                color: #ffb3b3;
                border: 1px solid #7a2d2d;
            }
            QPushButton#dangerButton:hover {
                background-color: #3a1717;
                border-color: #d14d4d;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 14, 16, 14)
        body_layout.setSpacing(12)

        title = QLabel("Do you really want to close the client?", self)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        title.setFont(title_font)
        title.setWordWrap(True)

        subtitle = QLabel("All live previews in the current window will be closed", self)
        subtitle.setStyleSheet("color: #7cb8ae;")
        subtitle.setWordWrap(True)

        button_row = QHBoxLayout()
        button_row.addStretch(1)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        self.exit_button = QPushButton("Exit", self)
        self.exit_button.setObjectName("dangerButton")
        self.exit_button.clicked.connect(self.accept)
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)

        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.exit_button)
        button_row.addStretch(1)

        body_layout.addWidget(title)
        body_layout.addWidget(subtitle)
        body_layout.addLayout(button_row)

        layout.addWidget(body)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.escape_shortcut.activated.connect(self.reject)
        self.cancel_button.setFocus(Qt.FocusReason.ActiveWindowFocusReason)


class ToastNotification(QFrame):
    dismissed = pyqtSignal(object)

    def __init__(
        self,
        message: str,
        *,
        title: str = "Screenshot saved",
        error: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setObjectName("viewerToast")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if error:
            self.setStyleSheet(
                """
                QFrame#viewerToast {
                    background-color: rgba(42, 18, 18, 240);
                    border: 1px solid #7a2d2d;
                    border-radius: 3px;
                }
                QLabel {
                    color: #ffb3b3;
                }
                """
            )
        else:
            self.setStyleSheet(
                """
                QFrame#viewerToast {
                    background-color: rgba(8, 17, 21, 240);
                    border: 1px solid #1a4c55;
                    border-radius: 3px;
                }
                QLabel {
                    color: #d8fff6;
                }
                """
            )

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self.opacity_effect)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self.title_label = QLabel(title, self)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(10)
        self.title_label.setFont(title_font)

        self.body_label = QLabel(message, self)
        self.body_label.setWordWrap(True)
        self.body_label.setMaximumWidth(520)

        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        self._apply_variant(error=error, pending=False)
        self.adjustSize()

        self.enter_opacity = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.enter_opacity.setDuration(180)
        self.enter_opacity.setStartValue(0.0)
        self.enter_opacity.setEndValue(1.0)
        self.enter_opacity.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.enter_position = QPropertyAnimation(self, b"pos", self)
        self.enter_position.setDuration(180)
        self.enter_position.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.enter_group = QParallelAnimationGroup(self)
        self.enter_group.addAnimation(self.enter_opacity)
        self.enter_group.addAnimation(self.enter_position)

        self.exit_opacity = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.exit_opacity.setDuration(180)
        self.exit_opacity.setStartValue(1.0)
        self.exit_opacity.setEndValue(0.0)
        self.exit_opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        self.exit_opacity.finished.connect(self._finalize_dismiss)

        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.setInterval(3600)
        self.dismiss_timer.timeout.connect(self.dismiss)

        self._dismissed = False

    def _apply_variant(self, *, error: bool, pending: bool) -> None:
        if error:
            self.setStyleSheet(
                """
                QFrame#viewerToast {
                    background-color: rgba(42, 18, 18, 240);
                    border: 1px solid #7a2d2d;
                    border-radius: 3px;
                }
                QLabel {
                    color: #ffb3b3;
                }
                """
            )
            self.body_label.setStyleSheet("color: #e8a3a3;")
            return

        if pending:
            self.setStyleSheet(
                """
                QFrame#viewerToast {
                    background-color: rgba(8, 17, 21, 240);
                    border: 1px solid #5e4a2c;
                    border-radius: 3px;
                }
                QLabel {
                    color: #d8fff6;
                }
                """
            )
            self.body_label.setStyleSheet("color: #c7b182;")
            return

        self.setStyleSheet(
            """
            QFrame#viewerToast {
                background-color: rgba(8, 17, 21, 240);
                border: 1px solid #1a4c55;
                border-radius: 3px;
            }
            QLabel {
                color: #d8fff6;
            }
            """
        )
        self.body_label.setStyleSheet("color: #7cb8ae;")

    def show_at(self, target: QPoint) -> None:
        self.adjustSize()
        start = QPoint(target.x(), target.y() + 16)
        self.move(start)
        self.show()
        self.raise_()
        self.enter_position.setStartValue(start)
        self.enter_position.setEndValue(target)
        self.enter_group.start()
        self.dismiss_timer.start()

    def move_to(self, target: QPoint) -> None:
        animation = QPropertyAnimation(self, b"pos", self)
        animation.setDuration(160)
        animation.setStartValue(self.pos())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start()
        self._move_animation = animation

    def dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss_timer.stop()
        self.exit_opacity.start()

    def show_pending(self) -> None:
        self._dismissed = False
        self.dismiss_timer.stop()
        self._apply_variant(error=False, pending=True)
        self.adjustSize()

    def mark_success(self, message: str, *, title: str = "Screenshot saved") -> None:
        self.title_label.setText(title)
        self.body_label.setText(message)
        self._apply_variant(error=False, pending=False)
        self.adjustSize()
        self.dismiss_timer.start()

    def mark_error(self, message: str, *, title: str = "Screenshot failed") -> None:
        self.title_label.setText(title)
        self.body_label.setText(message)
        self._apply_variant(error=True, pending=False)
        self.adjustSize()
        self.dismiss_timer.start()

    def _finalize_dismiss(self) -> None:
        self.hide()
        self.dismissed.emit(self)
        self.deleteLater()


class StreamTile(QFrame):
    clicked = pyqtSignal(int)
    badges_changed = pyqtSignal(int)
    status_changed = pyqtSignal(int, str)

    def __init__(
        self,
        index: int,
        stream: ViewerStream,
        relay: RelayStreamSession,
        *,
        start_with_audio: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.index = index
        self.stream = stream
        self.relay = relay
        self.start_with_audio = start_with_audio
        self.click_action = "open"
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { background-color: #081115; border: 1px solid #14333b; border-radius: 2px; }"
        )

        self.video_host = QWidget(self)
        self.video_host_layout = QVBoxLayout(self.video_host)
        self.video_host_layout.setContentsMargins(0, 0, 0, 0)
        self.video_host_layout.setSpacing(0)

        self.video_widget = ClickableVideoWidget(self.video_host)
        self.video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.video_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.video_widget.clicked.connect(self._emit_clicked)
        self.video_host_layout.addWidget(self.video_widget)

        title = QLabel(stream.title, self)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        title.setFont(title_font)
        title.setStyleSheet("color: #d8fff6;")

        self.live_badge = QLabel("OFFLINE", self)
        self.live_badge.setStyleSheet(
            "background-color: #2a1212; color: #ff8f8f; border: 1px solid #5a2424; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )

        self.audio_badge = QLabel("AUDIO", self)
        self.audio_badge.setStyleSheet(
            "background-color: #101820; color: #7cb8ae; border: 1px solid #1a4c55; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )

        title_row = QHBoxLayout()
        title_row.setContentsMargins(10, 8, 10, 8)
        title_row.setSpacing(8)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.audio_badge)
        title_row.addWidget(self.live_badge)

        self.status_label = QLabel("", self)
        self.status_label.setStyleSheet("color: #7cb8ae; padding: 0 10px 8px 10px;")
        self.status_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(title_row)
        layout.addWidget(self.video_host, stretch=1)
        layout.addWidget(self.status_label)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(1.0 if self.start_with_audio else 0.0)
        self.should_run = False
        self.has_connected_once = False

        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.has_gone_live = False
        self.player_boot_attempts = 0
        self.max_player_boot_attempts = 8
        self.player.errorOccurred.connect(self._handle_error)
        self.player.mediaStatusChanged.connect(self._handle_status_change)
        self.player.playbackStateChanged.connect(self._handle_playback_state_change)
        self.player.hasAudioChanged.connect(self._handle_has_audio_changed)
        self.player.tracksChanged.connect(self._refresh_audio_badge)
        self.player.activeTracksChanged.connect(self._refresh_audio_badge)
        self.player.setSource(QUrl(stream.relay_url))

        self.relay_watchdog = QTimer(self)
        self.relay_watchdog.setInterval(1200)
        self.relay_watchdog.timeout.connect(self._poll_relay_state)

        self.player_boot_timer = QTimer(self)
        self.player_boot_timer.setSingleShot(True)
        self.player_boot_timer.timeout.connect(self._play_source)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.setInterval(1000)
        self.reconnect_timer.timeout.connect(self._restart_after_failure)

        self._update_live_badge(False)
        self._refresh_audio_badge()

    def start(self) -> None:
        self.should_run = True
        self.has_gone_live = False
        self.player_boot_attempts = 0
        self._set_status("Connecting..")
        self.reconnect_timer.stop()
        self.relay.start()
        self.player_boot_timer.start(700)
        self.relay_watchdog.start()

    def stop(self) -> None:
        self.should_run = False
        self.reconnect_timer.stop()
        self.player_boot_timer.stop()
        self.relay_watchdog.stop()
        self.player.stop()
        self.relay.stop()

    def _play_source(self) -> None:
        if self.has_gone_live or self.player_boot_attempts >= self.max_player_boot_attempts:
            return

        self.player_boot_attempts += 1
        self._set_status(
            "Trying to reconnect.." if self.has_connected_once else "Connecting.."
        )
        self.player.stop()
        self.player.setSource(QUrl(self.stream.relay_url))
        self.player.play()

    def _schedule_player_retry(self, delay_ms: int = 500) -> None:
        if self.has_gone_live:
            return

        if self.relay.process is None or self.relay.process.poll() is not None:
            return

        if self.player_boot_attempts >= self.max_player_boot_attempts:
            return

        if not self.player_boot_timer.isActive():
            self.player_boot_timer.start(delay_ms)

    def _emit_clicked(self) -> None:
        if self.click_action == "open":
            self.clicked.emit(self.index)

    def _handle_error(self, _error, error_string: str) -> None:
        self._update_live_badge(False)
        if error_string and not self.has_gone_live:
            self._set_status(error_string)
            self._schedule_player_retry()

    def _handle_status_change(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.BufferedMedia:
            self.has_gone_live = True
            self.has_connected_once = True
            self._update_live_badge(True)
            self._refresh_audio_badge()
            self._clear_status()
        elif status == QMediaPlayer.MediaStatus.LoadingMedia and not self.has_gone_live:
            self._set_status(
                "Trying to reconnect.." if self.has_connected_once else "Connecting.."
            )
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._update_live_badge(False)
            relay_error = self.relay.last_error()
            self._set_status(relay_error or "Unable to open stream")
            self._schedule_player_retry()

    def _handle_playback_state_change(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.has_gone_live = True
            self._update_live_badge(True)
            self._refresh_audio_badge()
            self._clear_status()

    def _handle_has_audio_changed(self, available: bool) -> None:
        self._update_audio_badge(available)

    def _refresh_audio_badge(self) -> None:
        self._update_audio_badge(
            bool(self.player.hasAudio() or self.player.audioTracks())
        )

    def _poll_relay_state(self) -> None:
        if self.relay.process is None:
            return

        exit_code = self.relay.process.poll()
        if exit_code is None:
            return

        relay_error = self.relay.last_error()
        if not self.has_gone_live:
            self._update_live_badge(False)
            self._set_status(relay_error or "Unable to open stream")
        self.relay_watchdog.stop()
        if self.should_run and not self.reconnect_timer.isActive():
            self.reconnect_timer.start()

    def _restart_after_failure(self) -> None:
        if not self.should_run:
            return

        self.player.stop()
        self.relay.stop()
        self.has_gone_live = False
        self.player_boot_attempts = 0
        self._set_status("Trying to reconnect..")
        self.relay.start()
        self.player_boot_timer.start(700)
        self.relay_watchdog.start()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.show()
        self.status_changed.emit(self.index, text)

    def _clear_status(self) -> None:
        self.status_label.clear()
        self.status_label.hide()
        self.status_changed.emit(self.index, "")

    def set_click_action(self, action: str) -> None:
        self.click_action = action

    def _update_live_badge(self, online: bool) -> None:
        if online:
            self.live_badge.setText("LIVE")
            self.live_badge.setStyleSheet(
                "background-color: #0f2219; color: #7fffc4; border: 1px solid #1e5a44; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )
            self.badges_changed.emit(self.index)
            return

        self.live_badge.setText("OFFLINE")
        self.live_badge.setStyleSheet(
            "background-color: #2a1212; color: #ff8f8f; border: 1px solid #5a2424; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )
        self.badges_changed.emit(self.index)

    def _update_audio_badge(self, available: bool) -> None:
        if available:
            self.audio_badge.setText("🔊 AUDIO SUPPORTED")
            self.audio_badge.setStyleSheet(
                "background-color: #0d1a2d; color: #8fc6ff; border: 1px solid #295a9a; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )
            self.badges_changed.emit(self.index)
            return

        self.audio_badge.setText("🔇 AUDIO UNSUPPORTED")
        self.audio_badge.setStyleSheet(
            "background-color: #16191d; color: #9aa3ad; border: 1px solid #3a4048; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )
        self.badges_changed.emit(self.index)

    def badge_snapshot(self) -> dict[str, str]:
        return {
            "live_text": self.live_badge.text(),
            "live_style": self.live_badge.styleSheet(),
            "audio_text": self.audio_badge.text(),
            "audio_style": self.audio_badge.styleSheet(),
        }


class MultiChannelViewer(QWidget):
    HEADER_CONNECTING_FRAMES = ("·  ", "·· ", "···")

    def __init__(
        self,
        attempts: list[RtspAttempt],
        *,
        onvif_context: ViewerOnvifContext | None = None,
        launch_options: ViewerLaunchOptions | None = None,
    ) -> None:
        super().__init__()
        self.target_host = attempts[0].host if attempts else "unknown"
        self.single_stream_mode = len(attempts) == 1
        self.onvif_context = onvif_context
        self.ptz_controller = (
            onvifnet.PtzController(onvif_context)
            if onvif_context is not None and onvif_context.ptz_supported
            else None
        )
        self.ptz_supported = self.ptz_controller is not None
        self.active_ptz_keys: set[int] = set()
        self.focus_ptz_position: tuple[float | None, float | None] | None = None
        self.focus_ptz_display_position: tuple[float | None, float | None] | None = None
        self.focus_ptz_last_sample_at: float | None = None
        self.relays = [
            RelayStreamSession(
                attempt,
                compatibility_mode=self.single_stream_mode,
            )
            for attempt in attempts
        ]
        self.streams = [
            ViewerStream(
                title=f"Channel {index}",
                source_attempt=attempt,
                relay_url=relay.url,
                relay_port=relay.port,
            )
            for index, (attempt, relay) in enumerate(zip(attempts, self.relays), start=1)
        ]
        self.layout_spec = build_mosaic_layout(len(self.streams))
        self.tiles: list[StreamTile] = []

        self.focus_stream_index: int | None = None
        self.focus_click_bound_tile: StreamTile | None = None
        self.focus_zoom = 1.0
        self.focus_min_zoom = 1.0
        self.focus_max_zoom = 4.0
        self.focus_offset_x = 0
        self.focus_offset_y = 0
        self.close_in_progress = False
        self.toast_notifications: list[ToastNotification] = []
        self.snapshot_processes: list[tuple[QProcess, Path, Path, ToastNotification]] = []
        self.launch_options = launch_options or ViewerLaunchOptions()
        self.recording_process: QProcess | None = None
        self.recording_finalize_process: QProcess | None = None
        self.recording_output_path: Path | None = None
        self.recording_temp_path: Path | None = None
        self.recording_stderr_path: Path | None = None
        self.recording_finalize_stderr_path: Path | None = None
        self.recording_stream_index: int | None = None
        self.recording_toast: ToastNotification | None = None
        self.recording_finalize_mode_index = 0

        window_label = "live viewer" if self.single_stream_mode else "multi-channel viewer"
        self.setWindowTitle(f"pwneye {window_label} - {self.target_host}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(
            self.layout_spec.canvas_width,
            self.layout_spec.canvas_height + 96,
        )
        self.setStyleSheet(
            """
            QWidget {
                background-color: #061015;
                color: #d8fff6;
            }
            QLabel {
                color: #d8fff6;
            }
            QPushButton {
                background-color: #0d2027;
                color: #d8fff6;
                border: 1px solid #1a4c55;
                border-radius: 2px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #12313a;
                border-color: #28d7b5;
            }
            """
        )

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.header_bar = QWidget(self)
        self.header_bar.setObjectName("viewerHeaderBar")
        self.header_bar.setStyleSheet(
            """
            QWidget#viewerHeaderBar {
                background-color: #081115;
                border-bottom: 1px solid #14333b;
            }
            """
        )
        self.header_layout = QHBoxLayout(self.header_bar)
        self.header_layout.setContentsMargins(12, 6, 12, 6)
        self.header_layout.setSpacing(12)

        self.header_version_label = QLabel(
            f"pwneye {VERSION}_{CODENAME}",
            self.header_bar,
        )
        self.header_version_label.setStyleSheet(
            "color: #7cb8ae; font-size: 11px; font-family: Menlo, Monaco, 'Courier New', monospace;"
        )

        self.header_status_icon = QLabel("", self.header_bar)
        self.header_status_icon.setStyleSheet(
            "color: #b89f61; font-size: 11px; font-weight: 700; min-width: 20px;"
        )
        self.header_status_icon.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self.header_right_label = QLabel(f"Connecting to {self.target_host}", self.header_bar)
        self.header_right_label.setStyleSheet("font-size: 11px; font-weight: 700;")
        self.header_connected = False
        self.header_spinner_index = 0
        self.header_spinner_timer = QTimer(self)
        self.header_spinner_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.header_spinner_timer.setInterval(220)
        self.header_spinner_timer.timeout.connect(self._advance_header_connection_spinner)
        self.header_spinner_timer.start()

        self.header_exit_button = ClickableLabel("Exit", self.header_bar)
        self.header_exit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_exit_button.setStyleSheet(
            """
            QLabel {
                color: #c96a5f;
                font-weight: 600;
                padding: 0px;
            }
            QLabel:hover {
                color: #e07b6e;
            }
            """
        )
        self.header_exit_button.clicked.connect(self._request_close_client)

        self.header_layout.addWidget(self.header_version_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(
            self.header_status_icon,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        self.header_layout.addWidget(self.header_right_label, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.header_layout.addWidget(
            self.header_exit_button,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        self.root_layout.addWidget(self.header_bar)

        self.stack = QStackedLayout()
        self.grid_page = self._build_grid_page(self.layout_spec)
        self.focus_page = self._build_focus_page()
        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.focus_page)

        self.stack_container = QWidget(self)
        self.stack_container.setLayout(self.stack)
        self.root_layout.addWidget(self.stack_container, stretch=1)

        self.footer_bar = QWidget(self)
        self.footer_bar.setObjectName("viewerFooterBar")
        self.footer_bar.setStyleSheet(
            """
            QWidget#viewerFooterBar {
                background-color: #081115;
                border-top: 1px solid #14333b;
            }
            """
        )
        self.footer_layout = QHBoxLayout(self.footer_bar)
        self.footer_layout.setContentsMargins(12, 6, 12, 6)
        self.footer_layout.setSpacing(12)

        self.footer_left_label = QLabel(self.footer_bar)
        self.footer_left_label.setStyleSheet("color: #7cb8ae; font-size: 10px;")
        self.footer_left_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.footer_right_label = QLabel(self.footer_bar)
        self.footer_right_label.setStyleSheet("color: #7cb8ae; font-size: 10px;")
        self.footer_right_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.footer_layout.addWidget(self.footer_left_label, stretch=1)
        self.footer_layout.addWidget(self.footer_right_label, stretch=1)
        self.root_layout.addWidget(self.footer_bar)

        self._refresh_header_connection_state()
        self._update_footer()
        self.exit_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.exit_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.exit_shortcut.activated.connect(self._handle_escape)
        self.focus_next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        self.focus_next_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.focus_next_shortcut.activated.connect(self._focus_next_stream)
        self.focus_next_shortcut.setEnabled(False)
        self.focus_previous_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        self.focus_previous_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.focus_previous_shortcut.activated.connect(self._focus_previous_stream)
        self.focus_previous_shortcut.setEnabled(False)
        self.zoom_in_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Plus), self)
        self.zoom_in_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.zoom_in_shortcut.activated.connect(
            lambda: self._adjust_focus_zoom(0.2, anchor=self._current_focus_anchor())
        )
        self.zoom_in_shortcut.setEnabled(False)
        self.zoom_out_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Minus), self)
        self.zoom_out_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.zoom_out_shortcut.activated.connect(
            lambda: self._adjust_focus_zoom(-0.2, anchor=self._current_focus_anchor())
        )
        self.zoom_out_shortcut.setEnabled(False)
        self.zoom_reset_shortcut = QShortcut(QKeySequence("0"), self)
        self.zoom_reset_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.zoom_reset_shortcut.activated.connect(self._reset_focus_zoom)
        self.zoom_reset_shortcut.setEnabled(False)
        self.ptz_status_timer = QTimer(self)
        self.ptz_status_timer.setInterval(90)
        self.ptz_status_timer.timeout.connect(self._poll_ptz_status)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def start(self) -> None:
        for tile in self.tiles:
            tile.start()
        if self.single_stream_mode and self.tiles:
            self._enter_single_stream_focus()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.close_in_progress = True
        self.ptz_status_timer.stop()
        if self.recording_process is not None and self.recording_process.state() != QProcess.ProcessState.NotRunning:
            self.recording_process.kill()
            self.recording_process.waitForFinished(200)
        if self.recording_finalize_process is not None and self.recording_finalize_process.state() != QProcess.ProcessState.NotRunning:
            self.recording_finalize_process.kill()
            self.recording_finalize_process.waitForFinished(200)
        if self.recording_stderr_path is not None:
            self.recording_stderr_path.unlink(missing_ok=True)
        if self.recording_finalize_stderr_path is not None:
            self.recording_finalize_stderr_path.unlink(missing_ok=True)
        if self.recording_temp_path is not None:
            self.recording_temp_path.unlink(missing_ok=True)
        for process, _output_path, stderr_path, toast in list(self.snapshot_processes):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(200)
            stderr_path.unlink(missing_ok=True)
            toast.close()
        self.snapshot_processes.clear()
        for toast in list(self.toast_notifications):
            toast.close()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.releaseKeyboard()
        self._stop_ptz_motion(wait=False)
        self._stop_grid()
        for relay in self.relays:
            relay.cleanup()
        event.accept()
        os._exit(0)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._layout_toasts(animate=False)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.WindowDeactivate:
            if not self.close_in_progress:
                self._stop_ptz_motion(wait=False)
            return super().eventFilter(obj, event)

        if not self.isVisible() or not self.isActiveWindow():
            return super().eventFilter(obj, event)

        event_type = event.type()
        if event_type not in {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}:
            return super().eventFilter(obj, event)

        key = event.key()
        if event.isAutoRepeat():
            return True

        if event_type == QEvent.Type.KeyRelease and self._handle_ptz_key_release(key):
            return True

        if event_type != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)

        if self._handle_ptz_key_press(key):
            return True

        if key == Qt.Key.Key_Escape:
            self._handle_escape()
            return True

        if self.focus_stream_index is None:
            return super().eventFilter(obj, event)

        if key == Qt.Key.Key_Right and not self.single_stream_mode:
            self._focus_next_stream()
            return True

        if key == Qt.Key.Key_Left and not self.single_stream_mode:
            self._focus_previous_stream()
            return True

        if key in {Qt.Key.Key_Plus, Qt.Key.Key_Equal}:
            self._adjust_focus_zoom(0.2, anchor=self._current_focus_anchor())
            return True

        if key in {Qt.Key.Key_Minus, Qt.Key.Key_Underscore}:
            self._adjust_focus_zoom(-0.2, anchor=self._current_focus_anchor())
            return True

        if key == Qt.Key.Key_0:
            self._reset_focus_zoom()
            return True

        return super().eventFilter(obj, event)

    def _build_grid_page(self, layout_spec: MosaicLayout) -> QWidget:
        page = QWidget(self)
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)

        for index, (stream, relay) in enumerate(zip(self.streams, self.relays)):
            tile = StreamTile(
                index,
                stream,
                relay,
                start_with_audio=self.single_stream_mode,
                parent=page,
            )
            tile.clicked.connect(self._open_focus_view)
            tile.badges_changed.connect(self._refresh_focus_badges_for_tile)
            tile.status_changed.connect(self._refresh_focus_status_for_tile)
            self.tiles.append(tile)
            row, column = divmod(index, layout_spec.columns)
            layout.addWidget(tile, row, column)

        for column in range(layout_spec.columns):
            layout.setColumnStretch(column, 1)

        for row in range(layout_spec.rows):
            layout.setRowStretch(row, 1)

        return page

    def _build_focus_page(self) -> QWidget:
        page = QWidget(self)
        self.focus_layout = QVBoxLayout(page)
        self.focus_layout.setContentsMargins(12, 12, 12, 12)
        self.focus_layout.setSpacing(12)

        top_bar = QHBoxLayout()
        self.back_button = QPushButton("← Back", page)
        self.back_button.clicked.connect(self._close_focus_view)
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.focus_title = QLabel("Focused channel", page)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(15)
        self.focus_title.setFont(title_font)
        self.focus_title.setStyleSheet("color: #d8fff6;")

        self.focus_snapshot_button = QPushButton("📸 Snapshot", page)
        self.focus_snapshot_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.focus_snapshot_button.setStyleSheet(
            "background-color: #081115; color: #d8fff6; border: 1px solid #14333b; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 0px;"
        )
        self.focus_snapshot_button.clicked.connect(self._save_gui_snapshot)

        self.focus_record_button = QPushButton("● RECORD", page)
        self.focus_record_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.focus_record_button.setStyleSheet(
            "background-color: #081115; color: #d8fff6; border: 1px solid #14333b; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 0px;"
        )
        self.focus_record_button.clicked.connect(self._toggle_recording)
        self.focus_record_button.setVisible(self.launch_options.allow_recording)

        top_bar.addWidget(self.back_button)
        top_bar.addWidget(self.focus_title)
        top_bar.addWidget(self.focus_snapshot_button)
        top_bar.addWidget(self.focus_record_button)
        top_bar.addStretch(1)
        self.focus_movement_badge = QLabel("MOVEMENT UNSUPPORTED", page)
        self.focus_movement_badge.setStyleSheet(
            "background-color: #1a1711; color: #c2aa77; border: 1px solid #58452a; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )
        self.focus_audio_badge = QLabel("AUDIO UNSUPPORTED", page)
        self.focus_audio_badge.setStyleSheet(
            "background-color: #101820; color: #7cb8ae; border: 1px solid #1a4c55; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )
        self.focus_live_badge = QLabel("OFFLINE", page)
        self.focus_live_badge.setStyleSheet(
            "background-color: #2a1212; color: #ff8f8f; border: 1px solid #5a2424; padding: 2px 6px; font-size: 10px; font-weight: 700;"
        )
        self.focus_snapshot_button.setFixedHeight(self.focus_live_badge.sizeHint().height())
        self.focus_record_button.setFixedHeight(self.focus_live_badge.sizeHint().height())
        top_bar.addWidget(self.focus_movement_badge)
        top_bar.addWidget(self.focus_audio_badge)
        top_bar.addWidget(self.focus_live_badge)

        self.focus_layout.addLayout(top_bar)
        self._reset_focus_video_host(page)
        self.focus_back_shortcut = QShortcut(QKeySequence("Escape"), page)
        self.focus_back_shortcut.activated.connect(self._close_focus_view)
        self.focus_back_shortcut.setEnabled(False)
        return page

    def _reset_focus_video_host(self, parent: QWidget) -> None:
        """
        Recreate the focus host so the selected live widget can be moved into it.
        """
        existing_viewport = getattr(self, "focus_viewport", None)
        if existing_viewport is not None:
            self.focus_layout.removeWidget(existing_viewport)
            existing_viewport.deleteLater()

        self.focus_viewport = FocusVideoHost(parent)
        self.focus_viewport.setStyleSheet("background-color: #000000;")
        self.focus_viewport.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.focus_viewport.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.focus_viewport.resized.connect(self._apply_focus_transform)
        self.focus_video_host = FocusVideoHost(self.focus_viewport)
        self.focus_video_host.setStyleSheet("background-color: #000000;")
        self.focus_video_host.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.focus_overlay = FocusInteractionOverlay(self.focus_viewport)
        self.focus_overlay.wheel_zoom.connect(self._handle_focus_wheel)
        self.focus_overlay.pinch_zoom.connect(self._handle_focus_pinch)
        self.focus_overlay.clicked.connect(self._handle_focus_overlay_click)
        self.focus_layout.addWidget(self.focus_viewport, stretch=1)

    def _stop_grid(self) -> None:
        for tile in self.tiles:
            tile.stop()

    def _start_grid(self) -> None:
        for tile in self.tiles:
            tile.start()

    def _update_footer(self) -> None:
        if self.single_stream_mode:
            self.footer_left_label.setText("Focused view: use the mouse wheel or + / - to zoom")
            if self.ptz_supported:
                self.footer_right_label.setText("Use WASD to move the camera, 0 to reset zoom, or Esc to close the client")
            else:
                self.footer_right_label.setText("Press 0 to reset zoom or Esc to close the client")
            return

        if self.focus_stream_index is None:
            self.footer_left_label.setText("Click a stream to enter focus view")
            if self.ptz_supported:
                self.footer_right_label.setText("Enter focus view to move the camera with WASD or press Esc to close the client")
            else:
                self.footer_right_label.setText("Press Esc to close the client")
            return

        self.footer_left_label.setText(
            "Focused view: click the stream, press Esc, or use ← Back to return to the preview screen"
        )
        if self.ptz_supported:
            self.footer_right_label.setText("Use ← and → to switch channels, wheel or + / - to zoom, WASD to move, and 0 to reset")
        else:
            self.footer_right_label.setText("Use ← and → to switch channels, wheel or + / - to zoom, and 0 to reset")

    def _set_focus_navigation_enabled(self, enabled: bool) -> None:
        self.focus_back_shortcut.setEnabled(False)
        self.focus_next_shortcut.setEnabled(enabled and not self.single_stream_mode)
        self.focus_previous_shortcut.setEnabled(enabled and not self.single_stream_mode)
        self.zoom_in_shortcut.setEnabled(enabled)
        self.zoom_out_shortcut.setEnabled(enabled)
        self.zoom_reset_shortcut.setEnabled(enabled)

    def _handle_escape(self) -> None:
        if self.single_stream_mode:
            self._request_close_client()
            return

        if self.focus_stream_index is not None:
            self._close_focus_view()
            return

        self._request_close_client()

    def _request_close_client(self) -> None:
        dialog = ExitConfirmDialog(self)
        self.exit_shortcut.setEnabled(False)
        dialog.activateWindow()
        dialog.raise_()
        try:
            should_close = bool(dialog.exec())
        finally:
            self.exit_shortcut.setEnabled(True)

        if should_close:
            self.close()

    def _toggle_recording(self) -> None:
        if not self.launch_options.allow_recording or self.focus_stream_index is None:
            return

        if self.recording_process is None and self.recording_finalize_process is None:
            self._start_recording()
            return

        if self.recording_process is not None:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self.focus_stream_index is None:
            return

        attempt = self.streams[self.focus_stream_index].source_attempt
        output_path = resolve_recording_path(None, self.target_host)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = build_temp_recording_path(output_path)
        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-viewer-record-", suffix=".log")[1])

        pending_toast = self._push_toast(
            f"Recording stream to {output_path.resolve()}",
            title="Recording stream",
            pending=True,
        )

        process = QProcess(self)
        process.setProgram("ffmpeg")
        process.setArguments(build_ffmpeg_capture_cmd(attempt, temp_path)[1:])
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(str(stderr_path))
        process.finished.connect(
            lambda _code, _status, proc=process: self._handle_recording_capture_finished(proc)
        )
        process.errorOccurred.connect(
            lambda _error, proc=process: self._handle_recording_capture_finished(proc)
        )

        self.recording_process = process
        self.recording_output_path = output_path
        self.recording_temp_path = temp_path
        self.recording_stderr_path = stderr_path
        self.recording_stream_index = self.focus_stream_index
        self.recording_toast = pending_toast
        self._refresh_recording_ui()
        process.start()

    def _stop_recording(self) -> None:
        if self.recording_process is None:
            return

        toast = self.recording_toast
        if toast is not None:
            toast.title_label.setText("Finalizing recording")
            toast.body_label.setText("Stopping the capture and finalizing the MP4 file")
            toast.show_pending()
            self._layout_toasts(animate=False)

        if self.recording_process.state() == QProcess.ProcessState.NotRunning:
            self._handle_recording_capture_finished(self.recording_process)
            return

        try:
            self.recording_process.write(b"q\n")
            self.recording_process.waitForBytesWritten(200)
        except RuntimeError:
            pass

        QTimer.singleShot(5000, self._force_stop_recording_if_needed)

    def _force_stop_recording_if_needed(self) -> None:
        if self.recording_process is not None and self.recording_process.state() != QProcess.ProcessState.NotRunning:
            self.recording_process.kill()

    def _handle_recording_capture_finished(self, process: QProcess) -> None:
        if process is not self.recording_process:
            return

        process.deleteLater()
        self.recording_process = None

        output_path = self.recording_output_path
        temp_path = self.recording_temp_path
        stderr_path = self.recording_stderr_path
        if output_path is None or temp_path is None or stderr_path is None:
            self._reset_recording_state()
            return

        if not temp_path.exists() or temp_path.stat().st_size == 0:
            detail = _read_process_error(stderr_path) or "Unable to record the stream"
            if self.recording_toast is not None:
                self.recording_toast.mark_error(detail, title="Recording failed")
                self._layout_toasts(animate=False)
            stderr_path.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)
            self._reset_recording_state()
            return

        finalize_stderr = Path(tempfile.mkstemp(prefix="pwneye-viewer-finalize-", suffix=".log")[1])
        finalize = QProcess(self)
        finalize.setProgram("ffmpeg")
        self.recording_finalize_mode_index = 0
        finalize.setArguments(
            build_ffmpeg_finalize_cmd(temp_path, output_path, mode="copy")[1:]
        )
        finalize.setStandardOutputFile(QProcess.nullDevice())
        finalize.setStandardErrorFile(str(finalize_stderr))
        finalize.finished.connect(
            lambda _code, _status, proc=finalize: self._handle_recording_finalize_finished(proc)
        )
        finalize.errorOccurred.connect(
            lambda _error, proc=finalize: self._handle_recording_finalize_finished(proc)
        )
        self.recording_finalize_process = finalize
        self.recording_finalize_stderr_path = finalize_stderr
        finalize.start()

    def _handle_recording_finalize_finished(self, process: QProcess) -> None:
        if process is not self.recording_finalize_process:
            return

        process.deleteLater()
        output_path = self.recording_output_path
        temp_path = self.recording_temp_path
        stderr_path = self.recording_stderr_path
        finalize_stderr = self.recording_finalize_stderr_path
        toast = self.recording_toast
        modes = ["copy", "transcode", "video_only_transcode"]

        success = (
            process.exitStatus() == QProcess.ExitStatus.NormalExit
            and process.exitCode() == 0
            and output_path is not None
            and output_path.exists()
            and output_path.stat().st_size > 0
        )

        if toast is not None:
            if success and output_path is not None:
                toast.mark_success(
                    f"Recording saved in {output_path.resolve()}",
                    title="Recording saved",
                )
                self._layout_toasts(animate=False)
            elif (
                output_path is not None
                and temp_path is not None
                and finalize_stderr is not None
                and self.recording_finalize_mode_index < len(modes) - 1
            ):
                self.recording_finalize_mode_index += 1
                next_mode = modes[self.recording_finalize_mode_index]
                toast.title_label.setText("Finalizing recording")
                if next_mode == "transcode":
                    toast.body_label.setText("Completing the recording in compatibility mode")
                else:
                    toast.body_label.setText("Completing the recording in video-only compatibility mode")
                toast.show_pending()
                self._layout_toasts(animate=False)

                finalize_stderr.unlink(missing_ok=True)
                next_stderr = Path(tempfile.mkstemp(prefix="pwneye-viewer-finalize-", suffix=".log")[1])
                finalize = QProcess(self)
                finalize.setProgram("ffmpeg")
                finalize.setArguments(
                    build_ffmpeg_finalize_cmd(temp_path, output_path, mode=next_mode)[1:]
                )
                finalize.setStandardOutputFile(QProcess.nullDevice())
                finalize.setStandardErrorFile(str(next_stderr))
                finalize.finished.connect(
                    lambda _code, _status, proc=finalize: self._handle_recording_finalize_finished(proc)
                )
                finalize.errorOccurred.connect(
                    lambda _error, proc=finalize: self._handle_recording_finalize_finished(proc)
                )
                self.recording_finalize_process = finalize
                self.recording_finalize_stderr_path = next_stderr
                finalize.start()
                return
            else:
                detail = (
                    _read_process_error(finalize_stderr) if finalize_stderr is not None else None
                ) or "Unable to finalize the recording"
                toast.mark_error(detail, title="Recording failed")
                self._layout_toasts(animate=False)

        if stderr_path is not None:
            stderr_path.unlink(missing_ok=True)
        if finalize_stderr is not None:
            finalize_stderr.unlink(missing_ok=True)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        self.recording_finalize_process = None
        self._reset_recording_state()

    def _reset_recording_state(self) -> None:
        self.recording_output_path = None
        self.recording_temp_path = None
        self.recording_stderr_path = None
        self.recording_finalize_stderr_path = None
        self.recording_stream_index = None
        self.recording_toast = None
        self.recording_finalize_mode_index = 0
        self._refresh_recording_ui()

    def _refresh_recording_ui(self) -> None:
        active = self.recording_process is not None or self.recording_finalize_process is not None
        if self.launch_options.allow_recording:
            if active:
                self.focus_record_button.setText("■ STOP RECORDING")
                self.focus_record_button.setStyleSheet(
                    "background-color: #2a1212; color: #ffb3b3; border: 1px solid #7a2d2d; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 0px;"
                )
            else:
                self.focus_record_button.setText("● RECORD")
                self.focus_record_button.setStyleSheet(
                    "background-color: #081115; color: #d8fff6; border: 1px solid #14333b; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 0px;"
                )
        self.focus_record_button.setVisible(self.launch_options.allow_recording)

    def _save_gui_snapshot(self) -> None:
        if self.focus_stream_index is None:
            return

        attempt = self.streams[self.focus_stream_index].source_attempt
        output_path = resolve_snapshot_path(None, self.target_host)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-viewer-snapshot-", suffix=".log")[1])
        pending_toast = self._push_toast(
            f"Saving screenshot in {output_path.resolve()}",
            title="Saving screenshot",
            pending=True,
        )

        process = QProcess(self)
        process.setProgram("ffmpeg")
        process.setArguments(_build_ffmpeg_snapshot_cmd(attempt, output_path)[1:])
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(str(stderr_path))
        process.finished.connect(
            lambda _code, _status, proc=process, path=output_path, err=stderr_path, toast=pending_toast: self._handle_snapshot_finished(
                proc,
                path,
                err,
                toast,
            )
        )
        process.errorOccurred.connect(
            lambda _error, proc=process, path=output_path, err=stderr_path, toast=pending_toast: self._handle_snapshot_finished(
                proc,
                path,
                err,
                toast,
            )
        )
        self.snapshot_processes.append((process, output_path, stderr_path, pending_toast))
        process.start()

    def _push_toast(
        self,
        message: str,
        *,
        title: str = "Screenshot saved",
        error: bool = False,
        pending: bool = False,
    ) -> ToastNotification:
        toast = ToastNotification(message, title=title, error=error)
        if pending:
            toast.show_pending()
        toast.dismissed.connect(self._remove_toast)
        self.toast_notifications.append(toast)
        self._layout_toasts(animate=False)

        target = self._toast_target_position(toast)
        toast.show_at(target)
        if pending:
            toast.dismiss_timer.stop()
        return toast

    def _handle_snapshot_finished(
        self,
        process: QProcess,
        output_path: Path,
        stderr_path: Path,
        toast: ToastNotification,
    ) -> None:
        process_info = next(
            (entry for entry in self.snapshot_processes if entry[0] is process),
            None,
        )
        if process_info is None:
            # finished and errorOccurred can both fire for a single process; the
            # first invocation already handled it and scheduled its deletion.
            return
        self.snapshot_processes.remove(process_info)

        try:
            if (
                process.exitStatus() == QProcess.ExitStatus.NormalExit
                and process.exitCode() == 0
                and output_path.exists()
                and output_path.stat().st_size > 0
            ):
                toast.mark_success(
                    f"Snapshot saved in {output_path.resolve()}",
                    title="Snapshot saved",
                )
                self._layout_toasts(animate=False)
                return

            error_detail = _read_process_error(stderr_path) or "Unable to capture the current frame"
            toast.mark_error(error_detail, title="Snapshot failed")
            self._layout_toasts(animate=False)
        finally:
            stderr_path.unlink(missing_ok=True)
            process.deleteLater()

    def _remove_toast(self, toast: ToastNotification) -> None:
        if toast in self.toast_notifications:
            self.toast_notifications.remove(toast)
            self._layout_toasts()

    def _layout_toasts(self, *, animate: bool = True) -> None:
        for toast in list(self.toast_notifications):
            target = self._toast_target_position(toast)
            if animate and toast.isVisible():
                toast.move_to(target)
            elif not toast.isVisible():
                toast.move(target)
            else:
                toast.move(target)

    def _toast_target_position(self, target_toast: ToastNotification) -> QPoint:
        right_margin = 18
        bottom_margin = self.footer_bar.height() + 18
        stack_gap = 10
        bottom_right = self.mapToGlobal(self.rect().bottomRight())
        top_left = self.mapToGlobal(self.rect().topLeft())
        width = bottom_right.x() - top_left.x()
        height = bottom_right.y() - top_left.y()
        y = height - bottom_margin

        for toast in reversed(self.toast_notifications):
            toast.adjustSize()
            toast_y = y - toast.height()
            if toast is target_toast:
                return QPoint(
                    top_left.x() + width - right_margin - toast.width(),
                    top_left.y() + toast_y,
                )
            y = toast_y - stack_gap

        return QPoint(
            top_left.x() + width - right_margin - target_toast.width(),
            top_left.y() + y,
        )

    def _open_focus_view(self, index: int) -> None:
        if self.single_stream_mode or self.focus_stream_index is not None:
            return

        self.focus_stream_index = index
        stream = self.streams[index]
        selected_tile = self.tiles[index]

        self.focus_title.setText(stream.title)
        self._sync_focus_badges(selected_tile)
        selected_tile.audio_output.setVolume(1.0)
        self.back_button.show()
        self._attach_tile_to_focus(selected_tile, allow_click_close=True)
        self._set_focus_navigation_enabled(True)
        self._update_footer()
        self.stack.setCurrentWidget(self.focus_page)
        self.grabKeyboard()
        self._start_ptz_status_tracking()
        self._refresh_recording_ui()

    def _close_focus_view(self) -> None:
        if self.recording_process is not None or self.recording_finalize_process is not None:
            self._push_toast(
                "Stop recording before leaving the current stream",
                title="Recording in progress",
                error=True,
            )
            return

        if self.focus_stream_index is None:
            self.stack.setCurrentWidget(self.grid_page)
            return

        self._stop_ptz_status_tracking()
        self._stop_ptz_motion()
        selected_tile = self.tiles[self.focus_stream_index]
        selected_tile.audio_output.setVolume(0.0)
        self._restore_tile_from_focus(selected_tile)

        for tile in self.tiles:
            tile.updateGeometry()

        self._set_focus_navigation_enabled(False)
        self.focus_stream_index = None
        self._update_footer()
        self.stack.setCurrentWidget(self.grid_page)
        self.releaseKeyboard()
        self._refresh_recording_ui()

    def _focus_next_stream(self) -> None:
        if self.single_stream_mode or self.focus_stream_index is None or not self.tiles:
            return

        next_index = (self.focus_stream_index + 1) % len(self.tiles)
        self._switch_focus_stream(next_index)

    def _focus_previous_stream(self) -> None:
        if self.single_stream_mode or self.focus_stream_index is None or not self.tiles:
            return

        previous_index = (self.focus_stream_index - 1) % len(self.tiles)
        self._switch_focus_stream(previous_index)

    def _switch_focus_stream(self, index: int) -> None:
        if self.focus_stream_index is None or index == self.focus_stream_index:
            return
        if self.recording_process is not None or self.recording_finalize_process is not None:
            self._push_toast(
                "Stop recording before switching to another channel",
                title="Recording in progress",
                error=True,
            )
            return

        self._stop_ptz_motion()
        current_tile = self.tiles[self.focus_stream_index]
        next_tile = self.tiles[index]

        current_tile.audio_output.setVolume(0.0)
        self._restore_tile_from_focus(current_tile)

        self.focus_stream_index = index
        self.focus_title.setText(self.streams[index].title)
        self._sync_focus_badges(next_tile)
        next_tile.audio_output.setVolume(1.0)
        self._attach_tile_to_focus(next_tile, allow_click_close=True)
        self._poll_ptz_status()
        self._refresh_recording_ui()

        current_tile.updateGeometry()
        next_tile.updateGeometry()

    def _sync_focus_badges(self, tile: StreamTile) -> None:
        snapshot = tile.badge_snapshot()
        self.focus_live_badge.setText(snapshot["live_text"])
        self.focus_live_badge.setStyleSheet(snapshot["live_style"])
        self.focus_audio_badge.setText(snapshot["audio_text"])
        self.focus_audio_badge.setStyleSheet(snapshot["audio_style"])
        self._refresh_focus_movement_badge()

    def _start_ptz_status_tracking(self) -> None:
        if not self.ptz_supported:
            return

        self._poll_ptz_status()
        if not self.ptz_status_timer.isActive():
            self.ptz_status_timer.start()

    def _stop_ptz_status_tracking(self) -> None:
        self.ptz_status_timer.stop()
        self.focus_ptz_position = None
        self.focus_ptz_display_position = None
        self.focus_ptz_last_sample_at = None
        self._refresh_focus_movement_badge()

    def _poll_ptz_status(self) -> None:
        if not self.ptz_supported or self.focus_stream_index is None or self.ptz_controller is None:
            return

        now = time.monotonic()
        dt = 0.0 if self.focus_ptz_last_sample_at is None else max(0.0, now - self.focus_ptz_last_sample_at)
        self.focus_ptz_last_sample_at = now

        previous_actual = self.focus_ptz_position
        current_actual = self.ptz_controller.current_position()
        self.focus_ptz_position = current_actual

        if current_actual is not None and current_actual[0] is not None and current_actual[1] is not None:
            if self.focus_ptz_display_position is None:
                self.focus_ptz_display_position = current_actual
            elif previous_actual != current_actual:
                self.focus_ptz_display_position = current_actual
            elif self.active_ptz_keys:
                self.focus_ptz_display_position = self._estimate_ptz_display_position(
                    self.focus_ptz_display_position,
                    dt,
                )
            else:
                # Keep the last displayed position until the device reports a new one.
                pass
        elif self.focus_ptz_display_position is not None and self.active_ptz_keys:
            self.focus_ptz_display_position = self._estimate_ptz_display_position(
                self.focus_ptz_display_position,
                dt,
            )

        self._refresh_focus_movement_badge()

    def _estimate_ptz_display_position(
        self,
        position: tuple[float | None, float | None] | None,
        dt: float,
    ) -> tuple[float | None, float | None] | None:
        if position is None:
            return None

        x, y = position
        if x is None or y is None:
            return position

        pan, tilt = self._current_ptz_vector()
        if pan == 0.0 and tilt == 0.0:
            return position

        step_scale = 0.42
        next_x = max(-1.0, min(1.0, x + (pan * step_scale * dt)))
        next_y = max(-1.0, min(1.0, y + (tilt * step_scale * dt)))
        return next_x, next_y

    def _refresh_focus_movement_badge(self) -> None:
        if not self.ptz_supported:
            self.focus_movement_badge.setText("MOVEMENT UNSUPPORTED")
            self.focus_movement_badge.setStyleSheet(
                "background-color: #16191d; color: #9aa3ad; border: 1px solid #3a4048; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )
            return

        display = self.focus_ptz_display_position or self.focus_ptz_position
        if display is None:
            self.focus_movement_badge.setText("X --  |  Y --")
            self.focus_movement_badge.setStyleSheet(
                "background-color: #17140e; color: #c7b182; border: 1px solid #5e4a2c; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )
            return

        x, y = display
        x_text = "--" if x is None else f"{x:.3f}"
        y_text = "--" if y is None else f"{y:.3f}"
        moving = bool(self.active_ptz_keys)
        self.focus_movement_badge.setText(f"X {x_text}  |  Y {y_text}")
        if moving:
            self.focus_movement_badge.setStyleSheet(
                "background-color: #231a0f; color: #f0c978; border: 1px solid #9d6d1d; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )
        else:
            self.focus_movement_badge.setStyleSheet(
                "background-color: #17140e; color: #c7b182; border: 1px solid #5e4a2c; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            )

    def _enter_single_stream_focus(self) -> None:
        if not self.single_stream_mode or not self.tiles:
            return

        self.focus_stream_index = 0
        selected_tile = self.tiles[0]
        self.focus_title.setText(self.streams[0].title)
        self._sync_focus_badges(selected_tile)
        self.back_button.hide()
        self._attach_tile_to_focus(selected_tile, allow_click_close=False)
        self._set_focus_navigation_enabled(True)
        self.stack.setCurrentWidget(self.focus_page)
        self._update_footer()
        self.grabKeyboard()
        self._start_ptz_status_tracking()
        self._refresh_recording_ui()

    def _attach_tile_to_focus(self, tile: StreamTile, *, allow_click_close: bool) -> None:
        self.focus_zoom = 1.0
        self.focus_offset_x = 0
        self.focus_offset_y = 0
        tile.set_click_action("close" if allow_click_close else "open")
        tile.video_host_layout.removeWidget(tile.video_widget)
        tile.video_widget.setParent(self.focus_viewport)
        tile.video_widget.show()

        if allow_click_close:
            tile.video_widget.clicked.connect(self._close_focus_view)
            self.focus_click_bound_tile = tile
        else:
            self.focus_click_bound_tile = None

        self.focus_overlay.setGeometry(self.focus_viewport.rect())
        self.focus_overlay.raise_()
        self.focus_overlay.show()
        self.focus_viewport.setCursor(Qt.CursorShape.ArrowCursor)
        self._apply_focus_transform(recenter=True)

    def _restore_tile_from_focus(self, tile: StreamTile) -> None:
        if self.focus_click_bound_tile is not None:
            try:
                self.focus_click_bound_tile.video_widget.clicked.disconnect(self._close_focus_view)
            except TypeError:
                pass
            self.focus_click_bound_tile = None

        tile.set_click_action("open")
        tile.video_widget.setParent(tile.video_host)
        tile.video_host_layout.addWidget(tile.video_widget)
        tile.video_host_layout.invalidate()
        tile.updateGeometry()
        self.focus_overlay.hide()
        self.focus_zoom = 1.0
        self.focus_offset_x = 0
        self.focus_offset_y = 0
        self.focus_viewport.setCursor(Qt.CursorShape.ArrowCursor)

    def _handle_focus_wheel(self, delta: int) -> None:
        step = 0.15 if delta > 0 else -0.15
        self._adjust_focus_zoom(step, anchor=self._current_focus_anchor())

    def _handle_focus_pinch(self, value: float) -> None:
        if value == 0:
            return
        self._adjust_focus_zoom(value, anchor=self._current_focus_anchor())

    def _handle_focus_overlay_click(self) -> None:
        if self.single_stream_mode or self.focus_stream_index is None:
            return
        self._close_focus_view()

    def _adjust_focus_zoom(self, step: float, *, anchor: QPoint | None = None) -> None:
        if self.focus_stream_index is None:
            return

        old_zoom = self.focus_zoom
        next_zoom = max(self.focus_min_zoom, min(self.focus_max_zoom, self.focus_zoom + step))
        if abs(next_zoom - self.focus_zoom) < 0.001:
            return

        self.focus_zoom = next_zoom
        self._apply_focus_transform(anchor=anchor, previous_zoom=old_zoom)

    def _reset_focus_zoom(self) -> None:
        if self.focus_stream_index is None:
            return

        self.focus_zoom = 1.0
        self._apply_focus_transform()

    def _current_focus_anchor(self) -> QPoint | None:
        if self.focus_stream_index is None:
            return None

        anchor = self.focus_viewport.mapFromGlobal(QCursor.pos())
        if 0 <= anchor.x() <= self.focus_viewport.width() and 0 <= anchor.y() <= self.focus_viewport.height():
            return anchor
        return None

    def _apply_focus_transform(
        self,
        *,
        anchor: QPoint | None = None,
        previous_zoom: float | None = None,
        recenter: bool = False,
    ) -> None:
        if self.focus_stream_index is None:
            return

        selected_tile = self.tiles[self.focus_stream_index]
        widget = selected_tile.video_widget
        viewport_width = max(1, self.focus_viewport.width())
        viewport_height = max(1, self.focus_viewport.height())
        old_zoom = previous_zoom or self.focus_zoom
        old_width = max(1, int(viewport_width * old_zoom))
        old_height = max(1, int(viewport_height * old_zoom))
        old_left = self.focus_offset_x
        old_top = self.focus_offset_y
        zoomed_width = int(viewport_width * self.focus_zoom)
        zoomed_height = int(viewport_height * self.focus_zoom)
        widget.resize(zoomed_width, zoomed_height)

        if anchor is not None:
            ratio_x = (anchor.x() - old_left) / old_width
            ratio_y = (anchor.y() - old_top) / old_height
            self.focus_offset_x = int(anchor.x() - ratio_x * zoomed_width)
            self.focus_offset_y = int(anchor.y() - ratio_y * zoomed_height)
        elif recenter:
            self.focus_offset_x = (viewport_width - zoomed_width) // 2
            self.focus_offset_y = (viewport_height - zoomed_height) // 2

        min_x = min(0, viewport_width - zoomed_width)
        max_x = 0
        min_y = min(0, viewport_height - zoomed_height)
        max_y = 0
        self.focus_offset_x = max(min_x, min(max_x, self.focus_offset_x))
        self.focus_offset_y = max(min_y, min(max_y, self.focus_offset_y))
        widget.move(self.focus_offset_x, self.focus_offset_y)
        self.focus_viewport.setCursor(Qt.CursorShape.ArrowCursor)
        if getattr(self, "focus_overlay", None) is not None:
            self.focus_overlay.setGeometry(self.focus_viewport.rect())

    def _refresh_focus_badges_for_tile(self, index: int) -> None:
        self._refresh_header_connection_state()
        if self.focus_stream_index != index:
            return
        self._sync_focus_badges(self.tiles[index])

    def _refresh_focus_status_for_tile(self, index: int, text: str) -> None:
        self._refresh_header_connection_state()

    def _advance_header_connection_spinner(self) -> None:
        if self.header_connected:
            return

        self.header_spinner_index = (
            self.header_spinner_index + 1
        ) % len(self.HEADER_CONNECTING_FRAMES)
        self._render_header_connection_state()

    def _render_header_connection_state(self) -> None:
        if self.header_connected:
            self.header_status_icon.clear()
            self.header_right_label.setStyleSheet(
                "color: #8bc7aa; font-size: 11px; font-weight: 700;"
            )
            self.header_right_label.setText(f"Connected to {self.target_host}")
            return

        self.header_status_icon.setText(
            self.HEADER_CONNECTING_FRAMES[self.header_spinner_index]
        )
        self.header_status_icon.setStyleSheet(
            "color: #b89f61; font-size: 11px; font-weight: 700; min-width: 20px;"
        )
        self.header_right_label.setStyleSheet(
            "color: #c6b06f; font-size: 11px; font-weight: 700;"
        )
        self.header_right_label.setText(f"Connecting to {self.target_host}")

    def _refresh_header_connection_state(self) -> None:
        self.header_connected = any(tile.live_badge.text() == "LIVE" for tile in self.tiles)
        if self.header_connected:
            self.header_spinner_timer.stop()
        elif not self.header_spinner_timer.isActive():
            self.header_spinner_timer.start()
        self._render_header_connection_state()

    def _handle_ptz_key_press(self, key: int) -> bool:
        if (
            not self.ptz_supported
            or self.focus_stream_index is None
            or key not in {Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D}
        ):
            return False

        self.active_ptz_keys.add(key)
        self._apply_ptz_motion()
        return True

    def _handle_ptz_key_release(self, key: int) -> bool:
        if (
            not self.ptz_supported
            or key not in {Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D}
        ):
            return False

        self.active_ptz_keys.discard(key)
        self._apply_ptz_motion()
        return True

    def _current_ptz_vector(self) -> tuple[float, float]:
        pan = 0.0
        tilt = 0.0

        if Qt.Key.Key_A in self.active_ptz_keys:
            pan -= 0.6
        if Qt.Key.Key_D in self.active_ptz_keys:
            pan += 0.6
        if Qt.Key.Key_W in self.active_ptz_keys:
            tilt += 0.6
        if Qt.Key.Key_S in self.active_ptz_keys:
            tilt -= 0.6

        return pan, tilt

    def _apply_ptz_motion(self) -> None:
        pan, tilt = self._current_ptz_vector()
        self._refresh_focus_movement_badge()

        if self.ptz_controller is None:
            return

        if pan == 0.0 and tilt == 0.0:
            self.ptz_controller.stop()
            return

        self.ptz_controller.move(pan=pan, tilt=tilt)

    def _stop_ptz_motion(self, *, wait: bool = True) -> None:
        self.active_ptz_keys.clear()
        self._refresh_focus_movement_badge()
        if self.ptz_controller is not None:
            if wait:
                self.ptz_controller.stop()
            else:
                self.ptz_controller.stop_async()

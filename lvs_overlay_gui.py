#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  Qt6 Overlay GUI
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  This module is the VIEWER-INVARIANT HUD.  It depends only on
#  lvs_backend.LVSDataManager and a ViewerAdapter — no FastStone code here.
#
#  Bug fixes in this version
#  -------------------------
#    * Reset-to-top-centre now hugs the top of the screen (margin = 5 px)
#      instead of leaving 15 % blank.  (TOP_OFFSET 70 → TOP_MARGIN 5)
#    * Burst-flash no longer warps the layout.  The previous version used
#      QGraphicsOpacityEffect on the main container which forces Qt to
#      composite via QGraphicsView, causing the inner widgets to "scale".
#      The new flash paints a solid bright-green QFrame over the container
#      for ~80 ms then deletes it — layout is never touched.
#    * Border pre-pulse still expands 1 → 5 px before settling to 2 px.
#    * Tray "Reset to top-centre" now also clamps the X position when the
#      saved position is from a wider previous monitor configuration.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import sys
import json
from typing import Optional, Dict, List, Any, Tuple, Callable

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFrame, QSizePolicy, QSystemTrayIcon, QMenu, QFileDialog, QMessageBox,
    QGraphicsOpacityEffect,
)
from PyQt6.QtCore import (
    Qt, QTimer, QObject, pyqtSignal, QPoint,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QCursor, QIcon, QPixmap, QAction,
)

from lvs_backend import (
    LVSDataManager, ViewerAdapter, AHKManager, AHKPipeListener,
    SELECT_COUNT, SELECT_NAMES, strip_preview_hash, get_base_filename,
    reveal_in_explorer, load_hud_pos, save_hud_pos,
    __version__, __product_name__, __codename__, __license__, __author__,
)
from hesitancy_parser import HesitancyParser


# -----------------------------------------------------------------------------
# Visual constants
# -----------------------------------------------------------------------------
WEIGHTS = {
    "score_lighting":    0.5,
    "score_overall":     0.8,
    "score_quality":     1.0,
    "score_composition": 1.0,
    "score_color":       1.0,
    "score_dof":         1.0,
    "score_content":     1.0,
}

RANK_COLORS_RGB = {
    "lowest": (255, 69, 58),
    "low":    (255, 140, 0),
    "mid":    (255, 215, 0),
    "high":   (50, 205, 50),
    "best":   (0, 255, 127),
}

SCORE_LABELS = {
    "score_lighting":    "Light",
    "score_overall":     "Overall",
    "score_quality":     "Qual",
    "score_composition": "Comp",
    "score_color":       "Color",
    "score_dof":         "DoF",
    "score_content":     "Cont",
}

HUD_WIDTH    = 440
OPACITY      = 0.87
TOP_MARGIN   = 5         # v1.0.3: was 70 — too much dead space at the top


# -----------------------------------------------------------------------------
# Cross-thread signal buses
# -----------------------------------------------------------------------------
class WatcherSignals(QObject):
    """Bridge: ViewerAdapter watcher thread → Qt main thread."""
    update_request = pyqtSignal(str, bool)
    hide_request   = pyqtSignal()


class IPCSignals(QObject):
    """Bridge: AHKPipeListener thread → Qt main thread."""
    copy_event = pyqtSignal(dict)


# -----------------------------------------------------------------------------
# Rating circle
# -----------------------------------------------------------------------------
class RatingIndicator(QWidget):
    """28×28 ring badge for select-folder status. Grey = inert, colour = clickable."""

    clicked = pyqtSignal(int)

    def __init__(self, number: int):
        super().__init__()
        self.setFixedSize(28, 28)
        self.number = number
        self.color = QColor("#383838")
        self.text_color = QColor("#666")
        self._is_active = False
        self._update_cursor()

    def _update_cursor(self):
        self.setCursor(QCursor(
            Qt.CursorShape.PointingHandCursor if self._is_active
            else Qt.CursorShape.ArrowCursor
        ))

    def set_state(self, color: QColor, text_color: QColor, active: bool):
        self.color = color
        self.text_color = text_color
        self._is_active = active
        self._update_cursor()
        self.update()

    def mousePressEvent(self, event):
        if not self._is_active:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.number)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self.color); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 24, 24)
        p.setPen(self.text_color)
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.number))


# -----------------------------------------------------------------------------
# JPEG / RAW pill toggle
# -----------------------------------------------------------------------------
class ToggleWidget(QWidget):
    toggled = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = 'jpeg'
        self.setFixedSize(110, 22)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Toggle: clicked dots open JPEG vs RAW")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.state = 'raw' if self.state == 'jpeg' else 'jpeg'
            self.toggled.emit(self.state)
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height(); r = H / 2
        p.setBrush(QColor("#2a2a2a")); p.setPen(QColor("#555"))
        p.drawRoundedRect(0, 0, W, H, r, r)
        hw = W // 2
        if self.state == 'jpeg':
            hx, color = 0, QColor("#4caf50")
        else:
            hx, color = hw, QColor("#2196F3")
        p.setBrush(color); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(hx + 1, 1, hw - 2, H - 2, r - 1, r - 1)
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        for text, x, active in [("JPEG", 0, self.state == 'jpeg'),
                                ("RAW",  hw, self.state == 'raw')]:
            p.setPen(QColor("#fff") if active else QColor("#777"))
            p.drawText(x, 0, hw, H, Qt.AlignmentFlag.AlignCenter, text)


# -----------------------------------------------------------------------------
# Tray Icon
# -----------------------------------------------------------------------------
class LVSTrayIcon(QSystemTrayIcon):
    def __init__(self, overlay: 'LVSOverlay', dm: 'LVSDataManager',
                 ahk: Optional[AHKManager],
                 on_open_tasker: Optional[Callable] = None,
                 parent=None):
        # Parent to the overlay so the tray icon shares the overlay's lifetime
        # and is NOT garbage-collected the moment the constructing function
        # returns.  An unparented QSystemTrayIcon held only by a local variable
        # was the reason the tray icon never appeared after the launcher
        # returned into app.exec().
        super().__init__(parent if parent is not None else overlay)
        self.overlay = overlay
        self.dm = dm
        self.ahk = ahk
        self.on_open_tasker = on_open_tasker
        self._paused = False
        self.setIcon(self._make_icon())
        self.setToolTip(f"{__product_name__} v{__version__}  —  Running")
        self._build_menu()

        self.show()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[Tray] System tray not available yet — retrying in 2 s ...")
            QTimer.singleShot(2000, self._retry_show)

    def _retry_show(self):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.show()
            print("[Tray] System tray now available — icon shown.")
        else:
            # One more try in 5 s; if the host still isn't ready the icon
            # appears by itself once the shell finishes booting.
            QTimer.singleShot(5000, lambda: self.show() if QSystemTrayIcon.isSystemTrayAvailable() else None)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._build_menu)
        self._refresh_timer.start(4000)

    def _make_icon(self) -> QIcon:
        pix = QPixmap(22, 22); pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#263238")); p.setPen(QColor("#4caf50"))
        p.drawEllipse(1, 1, 20, 20)
        p.setBrush(QColor("#1565C0")); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(5, 5, 12, 12)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawEllipse(7, 7, 5, 5)
        p.end()
        return QIcon(pix)

    def _build_menu(self):
        menu = QMenu()
        count = self.dm.get_file_count()
        head = QAction(f"\U0001f4f7  {count:,} images in database", menu)
        head.setEnabled(False); menu.addAction(head)

        per = self.dm.get_select_folder_counts()
        any_row = False
        for i, n in enumerate(per, 1):
            if n <= 0: continue
            row = QAction(f"   \u2605\u00d7{i}: {n:,}", menu)
            row.setEnabled(False); menu.addAction(row)
            any_row = True
        if not any_row:
            row = QAction("   (no images sorted yet)", menu)
            row.setEnabled(False); menu.addAction(row)

        raws = self.dm.get_active_raws_root()
        if raws:
            short = raws if len(raws) <= 48 else "..." + raws[-45:]
            raws_row = QAction(f"\U0001f4c1 Raws: {short}", menu)
        else:
            raws_row = QAction("\U0001f4c1 Raws: (not set)", menu)
        raws_row.setEnabled(False); menu.addAction(raws_row)

        menu.addSeparator()

        if self.on_open_tasker:
            act_tasker = QAction("\U0001f527  Open Tasker", menu)
            act_tasker.triggered.connect(self.on_open_tasker)
            menu.addAction(act_tasker)

        act_show = QAction("\U0001f441  Show overlay now", menu)
        act_show.triggered.connect(self.overlay.force_show)
        menu.addAction(act_show)

        self.act_pause = QAction(
            "\u25b6  Resume overlay" if self._paused else "\u23f8  Pause overlay", menu)
        self.act_pause.triggered.connect(self._toggle_pause)
        menu.addAction(self.act_pause)

        act_reset = QAction("\u2b06  Reset to top-centre", menu)
        act_reset.triggered.connect(self.overlay.reset_position)
        menu.addAction(act_reset)

        menu.addSeparator()
        act_choose_raws = QAction("\U0001f4c2  Choose raws root...", menu)
        act_choose_raws.triggered.connect(self.overlay.prompt_for_raws_root)
        menu.addAction(act_choose_raws)
        if self.dm._raws_root_override:
            act_forget = QAction("\u232b  Forget raws location", menu)
            act_forget.triggered.connect(
                lambda: self.dm.set_raws_root_override(None))
            menu.addAction(act_forget)

        if self.ahk:
            act_reload = QAction("\u21bb  Reload AHK macro", menu)
            act_reload.triggered.connect(self.ahk.reload)
            menu.addAction(act_reload)

        menu.addSeparator()
        act_quit = QAction("\u2715  Quit LVS", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        try: self.activated.disconnect()
        except Exception: pass
        self.activated.connect(
            lambda reason: self.contextMenu().popup(QCursor.pos())
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )

    def _toggle_pause(self):
        self._paused = not self._paused
        self.overlay.paused = self._paused
        self.setToolTip(
            f"{__product_name__} v{__version__}  —  "
            + ("PAUSED" if self._paused else "Running"))
        self._build_menu()

    def _quit(self):
        if self.ahk: self.ahk.stop()
        self.hide()
        QApplication.quit()


# -----------------------------------------------------------------------------
# Main HUD overlay
# -----------------------------------------------------------------------------
class LVSOverlay(QWidget):
    """Frameless always-on-top HUD.  Viewer-agnostic."""

    def __init__(self, dm: LVSDataManager, signals: WatcherSignals):
        super().__init__()
        self.dm = dm
        self.signals = signals
        # Ephemeral caption-clean + phrase-score helper (display only; never
        # writes back).  Reads optional <workspace>/hesitancy.txt once.
        self.hesitancy = HesitancyParser(getattr(dm, "base_path", None))
        self.current_filename: Optional[str] = None
        self.paused = False
        self._drag_pos: Optional[QPoint] = None
        self.open_mode = 'jpeg'
        self._force_visible = False

        # Border state
        self._border_rgb   = (50, 205, 50)
        self._border_w     = 2
        self._border_alpha = 0.85

        # Toast
        self._toast_timer = QTimer(); self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.lbl_toast.setText(""))
        self._toast_effect = None   # lazy QGraphicsOpacityEffect for fade toasts
        self._toast_fade = None     # QPropertyAnimation

        # Flash overlay handle (created lazily in _flash_paint, destroyed after)
        self._flash_widget: Optional[QFrame] = None

        # Window flags
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(HUD_WIDTH)

        # Container
        self.main_container = QFrame(self)
        self.main_container.setObjectName("LVSContainer")
        self.main_container.setFixedWidth(HUD_WIDTH)
        self._apply_border_qss()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.main_container)

        layout = QVBoxLayout(self.main_container)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        # Header
        header = QHBoxLayout(); header.setSpacing(8)
        self.lbl_title = QLabel(
            f"LVS \u00b7 Selection Assist  v{__version__}")
        self.lbl_title.setStyleSheet(
            "color:#888;font-family:'Segoe UI';font-size:9px;font-weight:bold;"
            "letter-spacing:1px;")
        header.addWidget(self.lbl_title)
        header.addStretch()
        self.lbl_batch = QLabel("")
        self.lbl_batch.setStyleSheet(
            "color:#bbb;font-family:'Segoe UI';font-size:9px;font-weight:bold;")
        header.addWidget(self.lbl_batch)
        layout.addLayout(header)

        # Caption (rich text: Florence "tells" are rendered bold/red/larger)
        self.lbl_caption = QLabel("")
        self.lbl_caption.setWordWrap(True)
        self.lbl_caption.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_caption.setMaximumHeight(54)
        self.lbl_caption.setStyleSheet(
            "color:#DDD;font-family:'Segoe UI';font-size:12px;font-style:italic;")
        layout.addWidget(self.lbl_caption)

        # Score grid
        self.score_grid = QGridLayout()
        self.score_grid.setContentsMargins(0, 4, 0, 4)
        self.score_grid.setHorizontalSpacing(16)
        self.score_grid.setVerticalSpacing(4)
        self._score_val_labels: Dict[str, QLabel] = {}
        for idx, key in enumerate(SCORE_LABELS.keys()):
            row, col = divmod(idx, 4)
            lbl_name = QLabel(SCORE_LABELS[key])
            lbl_name.setStyleSheet(
                "color:#777;font-family:'Segoe UI';font-size:8px;font-weight:bold;"
                "letter-spacing:0.5px;")
            # Rich text so we can prepend a red "down" arrow / append a green
            # "up" arrow when a phrase modifier nudged this channel.
            lbl_val = QLabel("\u2014")
            lbl_val.setTextFormat(Qt.TextFormat.RichText)
            lbl_val.setStyleSheet(
                "color:#EEE;font-family:'Consolas','Cascadia Mono',monospace;"
                "font-size:11px;font-weight:600;")
            self.score_grid.addWidget(lbl_name, row * 2,     col,
                                      Qt.AlignmentFlag.AlignLeft)
            self.score_grid.addWidget(lbl_val,  row * 2 + 1, col,
                                      Qt.AlignmentFlag.AlignLeft)
            self._score_val_labels[key] = lbl_val
        layout.addLayout(self.score_grid)

        # Bottom row
        bot = QHBoxLayout(); bot.setSpacing(6)
        self.rating_widgets: List[RatingIndicator] = []
        for i in range(1, SELECT_COUNT + 1):
            rw = RatingIndicator(i)
            rw.clicked.connect(self._open_from_select)
            self.rating_widgets.append(rw)
            bot.addWidget(rw)
        bot.addStretch()
        tog_lbl = QLabel("Open:")
        tog_lbl.setStyleSheet(
            "color:#777;font-family:'Segoe UI';font-size:9px;font-weight:bold;")
        bot.addWidget(tog_lbl)
        self.toggle = ToggleWidget()
        self.toggle.toggled.connect(self._on_toggle)
        bot.addWidget(self.toggle)
        layout.addLayout(bot)

        # Toast
        self.lbl_toast = QLabel("")
        self.lbl_toast.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_toast.setStyleSheet(
            "color:#4caf50;font-family:'Segoe UI';font-size:10px;font-weight:bold;")
        self.lbl_toast.setMaximumHeight(14)
        layout.addWidget(self.lbl_toast)

        # Hook signals
        self.signals.update_request.connect(self.update_data)
        self.signals.hide_request.connect(self._on_hide_request)

        self.hide()
        self._restore_position()

    # ---------------------------------------------------- Border QSS
    def _apply_border_qss(self):
        r, g, b = self._border_rgb
        a = self._border_alpha
        w = self._border_w
        qss = (
            "QFrame#LVSContainer {"
            f" background-color: rgba(18,18,18,{OPACITY});"
            " border-radius: 12px;"
            f" border: {w}px solid rgba({r},{g},{b},{a});"
            "} "
            "QFrame#LVSContainer QWidget { background: transparent; border: none; } "
            "QFrame#LVSContainer QLabel  { background: transparent; border: none; }"
        )
        self.main_container.setStyleSheet(qss)

    # ---------------------------------------------------- Position
    def _restore_position(self):
        x, y = load_hud_pos()
        if x is not None and y is not None:
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.left(),  min(screen.right()  - HUD_WIDTH, x))
            y = max(screen.top(),   min(screen.bottom() - 60, y))
            self.move(x, y)
        else:
            self.reset_position()

    def reset_position(self):
        """v1.0.3: hug the TOP edge — was leaving ~15% blank screen space."""
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.left() + (screen.width() - HUD_WIDTH) // 2
        y = screen.top() + TOP_MARGIN
        self.move(x, y)
        save_hud_pos(self.x(), self.y())

    def force_show(self):
        self._force_visible = True
        if not self.current_filename:
            self.lbl_caption.setText("No image currently open in the viewer.")
            self.lbl_batch.setText("")
        self.show(); self.raise_(); self.activateWindow()

    def _on_hide_request(self):
        if self._force_visible:
            return
        self.hide()

    # ---------------------------------------------------- Drag
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            save_hud_pos(self.x(), self.y())

    # ---------------------------------------------------- Toggle / raws
    def _on_toggle(self, mode: str):
        self.open_mode = mode

    def prompt_for_raws_root(self) -> Optional[str]:
        start = (self.dm._raws_root_override
                 or self.dm.default_raws
                 or self.dm.base_path)
        chosen = QFileDialog.getExistingDirectory(
            self,
            "LVS — Select the RAW source root for this photoshoot "
            "(remembered until LVS exits)",
            start,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if not chosen:
            return None
        self.dm.set_raws_root_override(chosen)
        self.show_toast(f"Raws root set ({os.path.basename(chosen) or chosen})",
                        "#2196F3")
        return chosen

    # ---------------------------------------------------- Toast
    def show_toast(self, text: str, color: str = "#4caf50", ms: int = 2200):
        # Cancel any in-flight fade so a normal toast is fully opaque.
        if getattr(self, "_toast_fade", None) is not None:
            self._toast_fade.stop()
        if getattr(self, "_toast_effect", None) is not None:
            self._toast_effect.setOpacity(1.0)
        self.lbl_toast.setStyleSheet(
            f"color:{color};font-family:'Segoe UI';font-size:10px;font-weight:bold;")
        self.lbl_toast.setText(text)
        self._toast_timer.start(ms)

    def show_fade_toast(self, text: str, color: str = "#4caf50",
                        hold_ms: int = 0, fade_ms: int = 3000):
        """
        Show `text` then fade it out over `fade_ms` (default 3s).  Used for the
        RAW "Opening …" message so it lingers and gently disappears.
        """
        # Stop the plain-toast clear timer so it doesn't wipe us mid-fade.
        self._toast_timer.stop()
        if getattr(self, "_toast_effect", None) is None:
            self._toast_effect = QGraphicsOpacityEffect(self.lbl_toast)
            self.lbl_toast.setGraphicsEffect(self._toast_effect)
        if getattr(self, "_toast_fade", None) is not None:
            self._toast_fade.stop()
        self.lbl_toast.setStyleSheet(
            f"color:{color};font-family:'Segoe UI';font-size:10px;font-weight:bold;")
        self.lbl_toast.setText(text)
        self._toast_effect.setOpacity(1.0)
        anim = QPropertyAnimation(self._toast_effect, b"opacity", self)
        anim.setDuration(fade_ms)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)

        def _done():
            self.lbl_toast.setText("")
            self._toast_effect.setOpacity(1.0)  # reset for next plain toast
        anim.finished.connect(_done)
        # optional hold before the fade begins
        if hold_ms > 0:
            QTimer.singleShot(hold_ms, anim.start)
        else:
            anim.start()
        self._toast_fade = anim  # keep a reference

    # ---------------------------------------------------- Open cascade
    def _open_from_select(self, idx: int):
        if not self.current_filename:
            return
        try:
            if self.open_mode == 'raw':
                self._open_raw()
            else:
                self._open_jpeg_cascade(idx)
        except Exception as ex:
            print(f"[Open] {ex}")
            self.show_toast("Open error — see console", "#ff5252")

    def _open_jpeg_cascade(self, idx: int):
        fname = self.current_filename
        stripped = strip_preview_hash(fname)
        base_no_ext, _ = os.path.splitext(stripped)

        # T1 — edits/output
        t1 = self.dm.find_in_folder(self.dm.edits_output, [
            f"{base_no_ext}_edited.jpg",
            f"{base_no_ext}_edited.jpeg",
            f"{base_no_ext}.jpg",
            f"{base_no_ext}.jpeg",
        ])
        # JPEG path opens SILENTLY (no confirm toast) — the file just opens.
        if t1:
            os.startfile(t1)
            print(f"[Cascade T1] OPEN: {t1}"); return

        # Clean camera stem (no extension, no hash) for stem-matching against
        # files that were copied into selectN/ with an ingest hash appended,
        # e.g. 'DSC03806' must match 'DSC03806_4b2f758b29004ffe.jpg'.
        clean_stem = get_base_filename(fname)

        # T2 — select pool → reveal in Explorer (stem match, no DB roundtrip)
        t2 = (self.dm.find_in_folder_by_stem(self.dm.select_pool, clean_stem)
              or self.dm.find_in_folder_by_base(self.dm.select_pool, base_no_ext))
        if t2 and reveal_in_explorer(t2):
            print(f"[Cascade T2] REVEAL: {t2}"); return

        # T3 — selectN → open (stem match; the on-disk name carries the hash)
        t3 = (self.dm.find_in_folder_by_stem(
                  self.dm.select_folders[idx - 1], clean_stem)
              or self.dm.find_in_folder_by_base(
                  self.dm.select_folders[idx - 1], base_no_ext))
        if t3:
            os.startfile(t3)
            print(f"[Cascade T3] OPEN: {t3}"); return

        # T4 — DB-driven preview resolver
        t4 = self.dm.resolve_preview_path(fname)
        if t4:
            os.startfile(t4)
            print(f"[Cascade T4] OPEN: {t4}"); return

        # Only a failure surfaces to the user (clean stem, no hash).
        self.show_toast(f"Couldn't find {clean_stem}", "#ff5252")
        print(f"[Cascade] EXHAUSTED for {fname}")

    def _open_raw(self):
        fname = self.current_filename
        raw_name = self.dm.get_raw_filename(fname)
        clean_stem = get_base_filename(fname)
        if not raw_name:
            self.show_toast("No RAW linked to this photo", "#ff5252")
            return
        raw_clean = get_base_filename(raw_name)

        if self.dm.needs_raws_prompt():
            if not self.prompt_for_raws_root():
                return

        expected_hash = self.dm.get_source_hash_for_preview(fname)
        raw_path = self.dm.find_raw_file(raw_name, expected_hash=expected_hash)
        if not (raw_path and os.path.exists(raw_path)):
            self.show_toast(f"RAW not found: {raw_clean}", "#ff5252")
            print(f"[RAW] not found in {self.dm.get_active_raws_root()}: {raw_name}")
            return

        # "Opening …" (not "Opened") — a RAW launches a slow external editor, so
        # the message lingers and fades over ~3s while it loads.
        self.show_fade_toast(f"Opening {clean_stem} \u2026", "#2196F3",
                             fade_ms=3000)
        os.startfile(raw_path)
        print(f"[RAW] OPENING: {raw_path}")

    # ---------------------------------------------------- BURST CELEBRATION
    #
    # v1.0.3 fix:
    #   Previous version used QGraphicsOpacityEffect on main_container, which
    #   forced Qt to composite the container as a graphics-view item — this
    #   distorted the inner layout ("scaled and returned to normal").  The
    #   new approach paints a semi-transparent bright-green QFrame OVER the
    #   container (does not touch layout), then deletes it after ~80 ms.
    #
    #   The border still does its 1→3→5→2 px pre-pulse so the user reads
    #   "this is the best in burst" before the flash kicks in.
    def trigger_burst_celebration(self):
        """Pre-pulse border 1→5px then 80ms paint-only flash. ~120ms total."""
        rgb = RANK_COLORS_RGB["best"]

        def _set_border(w, a):
            self._border_w = w
            self._border_alpha = a
            self._border_rgb = rgb
            self._apply_border_qss()

        # Border expansion (synchronous, ~50 ms total)
        QTimer.singleShot(0,  lambda: _set_border(3, 0.95))
        QTimer.singleShot(20, lambda: _set_border(5, 1.00))

        # Paint-only flash starts at 30 ms, lives for 80 ms
        QTimer.singleShot(30, self._flash_paint_on)
        QTimer.singleShot(110, self._flash_paint_off)

        # Settle the border down to 2 px
        QTimer.singleShot(120, lambda: _set_border(2, 0.85))

    def _flash_paint_on(self):
        if self._flash_widget is not None:
            return
        flash = QFrame(self.main_container)
        flash.setObjectName("LVSFlash")
        flash.setStyleSheet(
            "QFrame#LVSFlash {"
            " background-color: rgba(0, 255, 127, 130);"
            " border-radius: 12px;"
            " border: 2px solid rgba(0, 255, 127, 220);"
            "}"
        )
        # Cover the container exactly; do NOT use a layout so no widget
        # is ever asked to resize.
        flash.setGeometry(0, 0,
                          self.main_container.width(),
                          self.main_container.height())
        flash.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        flash.show()
        flash.raise_()
        self._flash_widget = flash

    def _flash_paint_off(self):
        if self._flash_widget is not None:
            self._flash_widget.hide()
            self._flash_widget.deleteLater()
            self._flash_widget = None

    # ---------------------------------------------------- AHK IPC bridge
    def on_ahk_event(self, msg: dict):
        # The overlay does NOT register/announce copies (no "N copied of M"
        # toast). The only feedback is the small, transient rating-dot refresh
        # so the user sees which bucket the current frame now lives in.
        ev = msg.get("event")
        if ev in ("copied", "copy_failed"):
            if self.current_filename:
                self._refresh_rating_dots(self.current_filename)
        elif ev == "hotkey_fired":
            pass

    # ---------------------------------------------------- Caption rendering
    @staticmethod
    def _html_escape(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))

    # Total characters of caption we try to show around an anchored flag.
    CAP_BUDGET = 150
    CAP_LEAD   = 40   # max chars to keep BEFORE the flag

    def _render_caption_html(self, caption: str,
                             directions: Dict[str, str]) -> str:
        """
        Build the caption as rich text, ANCHORED on the most important flag.

        The phrase tell (e.g. BLURRY / HIGH QUALITY) matters more than the full
        caption, so when a flag sits mid-caption we keep a little context before
        it, render the flag BOLD + larger (red for a penalty, green for a boost),
        and then continue with as much trailing context as fits the budget —
        e.g.  "...with the focus on the BLURRY rider and the ramp behind..."

        `directions` maps phrase -> 'up'|'down' (green|red). If no flags are
        present we just show the start of the caption.
        """
        if not caption:
            return "n/a"
        budget = self.CAP_BUDGET

        # Find the earliest flag occurrence to anchor on.
        anchor: Optional[Tuple[int, int, str]] = None  # (start, end, phrase)
        low = caption.lower()
        for phrase in sorted(directions.keys(), key=len, reverse=True):
            i = low.find(phrase.lower())
            if i >= 0 and (anchor is None or i < anchor[0]):
                anchor = (i, i + len(phrase), phrase)

        if anchor is None:
            # No flag: show the head of the caption.
            head = caption[:budget]
            tail_ell = "" if len(caption) <= budget else "\u2026"
            return f"\u201c{self._html_escape(head)}{tail_ell}\u201d"

        a_start, a_end, a_phrase = anchor
        a_dir = directions.get(a_phrase, "down")

        # Keep up to CAP_LEAD chars before the flag (snap to a word boundary).
        lead_start = max(0, a_start - self.CAP_LEAD)
        if lead_start > 0:
            sp = caption.find(" ", lead_start, a_start)
            if sp != -1:
                lead_start = sp + 1
        before = caption[lead_start:a_start]
        flag_text = caption[a_start:a_end]

        # Fill the remaining budget with trailing context.
        remaining = max(0, budget - len(before) - len(flag_text))
        after = caption[a_end:a_end + remaining]
        # snap the tail back to a word boundary if we cut mid-word
        if a_end + remaining < len(caption):
            sp = after.rfind(" ")
            if sp > 0:
                after = after[:sp]
            after_ell = "\u2026"
        else:
            after_ell = ""
        before_ell = "\u2026" if lead_start > 0 else ""

        colour = "#00ff7f" if a_dir == "up" else "#ff4538"
        flag_html = (f"<span style='color:{colour};font-weight:bold;"
                     f"font-size:14px;'>{self._html_escape(flag_text)}</span>")

        body = (before_ell + self._html_escape(before) + flag_html
                + self._html_escape(after) + after_ell)
        return f"\u201c{body}\u201d"

    # ---------------------------------------------------- Main update
    def update_data(self, filename: str, is_fullscreen: bool):
        if self.paused:
            return
        if not is_fullscreen:
            if not self._force_visible:
                self.hide()
            return
        if filename == self.current_filename and self.isVisible():
            self._refresh_rating_dots(filename)
            return
        self.current_filename = filename

        data = self.dm.get_image_data(filename)
        if not data:
            if not self._force_visible:
                self.hide()
            return

        scores = {}
        for k in WEIGHTS.keys():
            v = data.get(k, 0)
            scores[k] = v if (v is not None and v > 0) else None

        burst_id = data.get('burst_id')
        overall  = scores.get('score_overall', 0) or 0
        batch    = self.dm.get_batch_rank(burst_id, overall)
        rgb      = RANK_COLORS_RGB.get(batch['rank'], RANK_COLORS_RGB['mid'])

        self._border_rgb = rgb
        self._border_w = 2
        self._border_alpha = 0.85
        self._apply_border_qss()

        raw_cap = data.get('caption') or ''
        # Phrase-based score nudges + arrows (display-only; from raw caption).
        scores, arrows, _matched = self.hesitancy.apply_score_modifiers(
            raw_cap, scores)
        # Conservative caption clean for readability.
        clean_cap = self.hesitancy.clean_caption(raw_cap)
        # Flag colours (green=boost, red=penalty); anchor the caption on the
        # most important flag rather than always showing the head.
        directions = self.hesitancy.phrase_directions(clean_cap)
        self.lbl_caption.setText(
            self._render_caption_html(clean_cap, directions))

        for key, lbl in self._score_val_labels.items():
            v = scores.get(key)
            if v is None:
                lbl.setText("n/a")
                continue
            num = f"{v:.2f}"
            arr = arrows.get(key)
            if arr == "up":
                # green arrow on the RIGHT (boost)
                lbl.setText(f"{num} <span style='color:#00ff7f;'>&#9650;</span>")
            elif arr == "down":
                # red arrow on the LEFT (penalty)
                lbl.setText(f"<span style='color:#ff4538;'>&#9660;</span> {num}")
            else:
                lbl.setText(num)

        star = "  \u2605" if batch['is_best'] else ""
        self.lbl_batch.setText(f"Burst {batch['position']}{star}")

        self._refresh_rating_dots(filename)

        self.main_container.adjustSize(); self.adjustSize()
        self.show()
        if batch['is_best']:
            self.trigger_burst_celebration()

    def _refresh_rating_dots(self, filename: str):
        found_in = self.dm.get_folder_ratings(filename)
        if not found_in:
            for rw in self.rating_widgets:
                rw.set_state(QColor("#383838"), QColor("#666"), active=False)
        elif len(found_in) > 1:
            for i, rw in enumerate(self.rating_widgets, 1):
                if i in found_in:
                    rw.set_state(QColor("#ff9800"), QColor("#FFF"), active=True)
                else:
                    rw.set_state(QColor("#222"), QColor("#444"), active=False)
        else:
            rating = found_in[0]
            for i, rw in enumerate(self.rating_widgets, 1):
                if i == rating:
                    rw.set_state(QColor("#4caf50"), QColor("#FFF"), active=True)
                elif i < rating:
                    rw.set_state(QColor("#2e7031"),
                                 QColor("#9ad79d"), active=False)
                else:
                    rw.set_state(QColor("#383838"), QColor("#666"), active=False)

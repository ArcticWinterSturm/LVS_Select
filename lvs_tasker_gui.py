#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  Qt6 Tasker GUI
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  v1.0.4 — full rewrite from Tkinter → Qt6
#  -----------------------------------------
#  Rationale: the dual-mainloop (Qt overlay + Tk tasker) caused side-by-side
#  windows to mangle each other's Win32 message pumps and IPC handles in
#  testing.  Qt-only across both surfaces means a single QApplication owns
#  every window, eliminating that whole class of bugs and unlocking direct
#  cross-window signal/slot wiring.
#
#  Layout (mirrors the screenshot reference on the right of the user's compose):
#
#   ┌─────────────────────────────────────────────────────────────────┐
#   │ Paths  (click ↻ to re-detect)                                   │
#   │   Workspace    [ C:\editing                          ] [...]    │
#   │   Previews     [ C:\editing\previews                 ] [...]    │
#   │   RAWs         [ D:\DCIM            (external/local) ] [...]    │
#   │   Database     [ C:\editing\ingest.db           [✓]  ] [...]    │  ← "inject" tick
#   │   [          Auto-detect from Workspace                    ]    │
#   ├─────────────────────────────────────────────────────────────────┤
#   │ Star buckets  (1★ 2★ 3★ 4★ 5★)                                  │
#   │   [  20  ] [  20 ] [  50 ] [ 133 ] [ 270 ]                      │
#   │   Total sorted pictures: 493                                    │
#   ├─────────────────────────────────────────────────────────────────┤
#   │ Mode A · Normal Execute (move JPEGs from selectN folders)       │
#   │   [▶ Run Normal Execute ]   <descriptor>                        │
#   ├─────────────────────────────────────────────────────────────────┤
#   │ Mode B · Paste dir / ls / Get-ChildItem output                  │
#   │   [ textarea                                              ]     │
#   │   [✓ Test Parse]  [▶ Execute Paste (COPY previews → select/)]   │
#   │                                                  [✕ Reset]      │
#   ├─────────────────────────────────────────────────────────────────┤
#   │ RAW copy-back                                                   │
#   │   [ source path                              ] [...] [ Copy ]   │
#   │                                              [Placeholder]      │
#   │   [Verify external location] ← appears only when src is valid   │
#   ├─────────────────────────────────────────────────────────────────┤
#   │ Log  (read-only, monospace)                                     │
#   └─────────────────────────────────────────────────────────────────┘
#
#  Colour-coding for path fields:
#     OK   = exists & has content    (green tint)
#     WARN = exists but empty         (yellow tint)
#     BAD  = does not exist           (red tint)
#
#  Database "inject" tick:
#     When checked, every Apply-paths action also re-runs the FastStone
#     configurator (registry + FSSettings.db patch) with the currently
#     shown paths, so changing Workspace from the GUI propagates to the
#     hotkeys without needing a re-launch.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import re
import time
import traceback
import subprocess
from pathlib import Path
from typing import Optional, List, Callable

from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QPlainTextEdit, QCheckBox, QFrame,
    QFileDialog, QMessageBox, QSizePolicy, QToolButton, QGroupBox, QDialog,
    QSlider, QStackedWidget, QGraphicsOpacityEffect
)
from PyQt6.QtCore import (
    Qt, QTimer, QObject, pyqtSignal, QThread,
    QPropertyAnimation, QEasingCurve, QEvent
)
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor

from lvs_backend import (
    LVSDataManager, ViewerAdapter, raws_copy_back,
    SELECT_NAMES, SELECT_COUNT, PICTURE_EXTS, RAW_EXTS,
    load_tasker_paths, save_tasker_paths,
    __version__, __product_name__, __codename__, __author__, __license__,
)


# ─────────────────────────────────────────────────────────────────────────────
# Catppuccin Mocha palette (matches the original lvs_tasker_gui colour scheme
# the user said "old GUI is better with its colour coded yes found / no not")
# ─────────────────────────────────────────────────────────────────────────────
BG       = "#000000"   # Black background
BG2      = "#121212"
BG3      = "#1a1a1a"
BG4      = "#2d2d2d"
FG       = "#cdd6f4"
FG_DIM   = "#6c7086"
GREEN    = "#a6e3a1"
RED      = "#f38ba8"
YELLOW   = "#f9e2af"
BLUE     = "#89b4fa"
ORANGE   = "#fab387"
PURPLE   = "#cba6f7"

# Field state tints (mirrors the green/yellow/red field backgrounds in old GUI)
BG_OK    = "#2a3a2a"   # path resolves & has content
BG_WARN  = "#3a3520"   # path resolves but empty
BG_BAD   = "#3a2a2a"   # path doesn't exist


# ─────────────────────────────────────────────────────────────────────────────
# Master stylesheet
# ─────────────────────────────────────────────────────────────────────────────
def _stylesheet() -> str:
    return f"""
    QMainWindow, QWidget {{
        background-color: {BG};
        color: {FG};
        font-family: 'Segoe UI', sans-serif;
        font-size: 10pt;
    }}
    QGroupBox {{
        font-weight: bold;
        color: {BLUE};
        border: 1px solid {BG3};
        border-radius: 6px;
        margin-top: 14px;
        padding-top: 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 6px;
    }}
    QPushButton {{
        background-color: {BG3};
        color: {FG};
        border: none;
        padding: 6px 14px;
        border-radius: 4px;
        font-weight: bold;
    }}
    QPushButton:hover  {{ background-color: {BG4}; }}
    QPushButton:pressed {{ background-color: {BG2}; }}
    QPushButton:disabled {{
        background-color: {BG2};
        color: {FG_DIM};
    }}
    QPushButton#PrimaryBtn {{ color: {GREEN}; }}
    QPushButton#WarnBtn    {{ color: {YELLOW}; }}
    QPushButton#DangerBtn  {{ color: {RED}; }}
    QPushButton#PlaceBtn   {{ color: {FG_DIM}; }}
    QPushButton#GhostBtn   {{ color: {BLUE}; }}
    QLineEdit, QPlainTextEdit {{
        background-color: {BG3};
        color: {FG};
        border: 1px solid {BG2};
        border-radius: 4px;
        padding: 4px 8px;
        font-family: 'Consolas', 'Cascadia Mono', monospace;
        font-size: 10pt;
        selection-background-color: {BLUE};
        selection-color: {BG};
    }}
    QLineEdit#FieldOK   {{ background-color: {BG_OK};   border: 1px solid #2e7031; }}
    QLineEdit#FieldWarn {{ background-color: {BG_WARN}; border: 1px solid #6b5a1a; }}
    QLineEdit#FieldBad  {{ background-color: {BG_BAD};  border: 1px solid #6b2e2e; }}
    QToolButton {{
        background: transparent;
        color: {FG};
        border: none;
        padding: 4px 6px;
        font-weight: bold;
    }}
    QToolButton:hover {{ background-color: {BG3}; border-radius: 3px; }}
    QCheckBox {{ color: {FG}; spacing: 6px; }}
    QCheckBox::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {BG4};
        border-radius: 3px;
        background: {BG3};
    }}
    QCheckBox::indicator:checked {{
        background: {GREEN};
        border: 1px solid #2e7031;
    }}
    /* Inline RAWs copy toggle — identical to other buttons, checked gets highlight */
    QPushButton#RawCopyToggle {{
        background-color: {BG3};
        color: {FG};
        border: none;
        padding: 6px 10px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 9pt;
    }}
    QPushButton#RawCopyToggle:hover  {{ background-color: {BG4}; }}
    QPushButton#RawCopyToggle:checked {{
        background-color: {BG4};
    }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# Path field row — Label · LineEdit (colour-coded) · optional checkbox · "..."
# ─────────────────────────────────────────────────────────────────────────────
class PathRow(QWidget):
    """
    A reusable horizontal row for displaying / editing a single path:
    Label · colour-coded LineEdit · [optional inline toggle] · "..." browse.
    Emits `changed` whenever the path text edits (or the toggle flips).
    """

    changed = pyqtSignal()

    def __init__(
        self, label: str,
        toggle_text: str = "",
        toggle_tip: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        h = QHBoxLayout(self); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)

        self.lbl = QLabel(label + ":")
        # Fixed, generous label column so every label reads on one line AND
        # every value field below starts at the same x.
        self.lbl.setFixedWidth(110)
        self.lbl.setStyleSheet(f"color: {FG_DIM}; font-weight: bold; font-size: 9pt;")
        h.addWidget(self.lbl)

        self.edit = QLineEdit()
        # Wide minimum + expanding policy so long paths are readable.
        self.edit.setMinimumWidth(300)
        self.edit.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Fixed)
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self._on_edit)
        h.addWidget(self.edit, 1)

        # Optional inline toggle (used by the RAWs row for "copy into ./raws
        # during cull").  Hidden until enabled via set_toggle_visible().
        # Uses a QPushButton (not QCheckBox) so it renders consistently with
        # every other button in the GUI — no platform-native tick-box oddities.
        self.toggle: Optional[QPushButton] = None
        self._toggle_on: bool = True
        if toggle_text:
            self.toggle = QPushButton(toggle_text)
            self.toggle.setCheckable(True)
            self.toggle.setChecked(True)            # ON by default
            self.toggle.setToolTip(toggle_tip)
            self.toggle.setVisible(False)
            self.toggle.setObjectName("RawCopyToggle")
            self.toggle.setFixedWidth(130)
            self.toggle.clicked.connect(lambda _s: self.changed.emit())
            h.addWidget(self.toggle)

        self.btn = QPushButton("...")
        self.btn.setFixedWidth(36)
        h.addWidget(self.btn)

    def set_toggle_visible(self, vis: bool):
        if self.toggle is not None:
            self.toggle.setVisible(vis)

    def toggle_on(self) -> bool:
        return bool(self.toggle and self.toggle.isVisible() and self.toggle.isChecked())

    def _on_edit(self, _txt: str):
        self.changed.emit()

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, s: str):
        self.edit.setText(s)

    def set_state(self, state: str):
        """state: 'ok' | 'warn' | 'bad' | 'neutral'"""
        # Re-polishing every 2s (on the refresh timer) recomputed the field
        # geometry and made the path edits visibly shrink/jitter.  Only re-style
        # when the state ACTUALLY changes.
        if getattr(self, "_state", None) == state:
            return
        self._state = state
        name = {"ok": "FieldOK", "warn": "FieldWarn",
                "bad": "FieldBad", "neutral": ""}.get(state, "")
        self.edit.setObjectName(name)
        self.edit.style().unpolish(self.edit)
        self.edit.style().polish(self.edit)


# ─────────────────────────────────────────────────────────────────────────────
# Star bucket display
# ─────────────────────────────────────────────────────────────────────────────
class ViewerBox(QFrame):
    """
    A single selectable viewer box (e.g. "FastStone" / "Digikam").

    Inert pickers for now — they only toggle the visual selection so the user
    can see which viewer the session is bound to.  The digiKam auto-culling
    integration that wires box 2 to a real adapter is coming soon.
    """
    clicked = pyqtSignal(str)   # emits the viewer id when picked

    def __init__(self, viewer_id: str, label: str, available: bool = True):
        super().__init__()
        self.viewer_id = viewer_id
        self.available = available
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)
        v = QVBoxLayout(self); v.setContentsMargins(10, 6, 10, 6)
        self._lbl = QLabel(label)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._lbl)
        self._restyle()

    def set_selected(self, sel: bool):
        self._selected = sel
        self._restyle()

    def _restyle(self):
        if self._selected:
            self.setStyleSheet(
                f"QFrame {{ background-color: {BG_OK}; border: 1px solid #2e7031;"
                f" border-radius: 6px; }}")
            self._lbl.setStyleSheet(
                f"color: {GREEN}; font-weight: bold; font-size: 10pt;"
                " background: transparent; border: none;")
        else:
            col = FG_DIM if not self.available else FG
            self.setStyleSheet(
                f"QFrame {{ background-color: {BG2}; border: 1px solid {BG3};"
                f" border-radius: 6px; }}")
            self._lbl.setStyleSheet(
                f"color: {col}; font-weight: bold; font-size: 10pt;"
                " background: transparent; border: none;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.viewer_id)


class StarBucket(QFrame):
    """A clickable labelled count cell — '1 ★' / count. Click opens the folder."""

    clicked = pyqtSignal(int)

    def __init__(self, idx: int):
        super().__init__()
        self.idx = idx
        self.setStyleSheet(
            f"QFrame {{ background-color: {BG2}; border-radius: 6px;"
            f" border: 1px solid {BG3}; }}"
            f"QFrame:hover {{ border: 1px solid {BLUE}; }}"
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open this folder")
        self.setMinimumHeight(64)
        v = QVBoxLayout(self); v.setContentsMargins(8, 6, 8, 6); v.setSpacing(2)
        title = QLabel(f"{idx} \u2605")     # "1 ★"
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {BLUE}; font-weight: bold; font-size: 10pt;"
            " background: transparent; border: none;")
        v.addWidget(title)
        self.val = QLabel("—")
        self.val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.val.setStyleSheet(
            f"color: {FG}; font-size: 18pt; font-weight: bold;"
            " background: transparent; border: none;")
        v.addWidget(self.val)

    def set_count(self, n: int):
        self.val.setText(f"{n:,}")
        if n == 0:
            self.val.setStyleSheet(
                f"color: {FG_DIM}; font-size: 18pt; font-weight: bold;"
                " background: transparent; border: none;")
        else:
            self.val.setStyleSheet(
                f"color: {GREEN}; font-size: 18pt; font-weight: bold;"
                " background: transparent; border: none;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.idx)


# ─────────────────────────────────────────────────────────────────────────────
# Background worker for raws_copy_back  (keeps GUI responsive)
# ─────────────────────────────────────────────────────────────────────────────
class CopyBackWorker(QObject):
    progress = pyqtSignal(str, int, int)
    done     = pyqtSignal(dict)
    failed   = pyqtSignal(str)

    def __init__(self, dm: LVSDataManager, source_root: str):
        super().__init__()
        self.dm = dm
        self.source_root = source_root

    def run(self):
        try:
            summary = raws_copy_back(
                self.dm, self.source_root,
                dry_run=False,
                on_progress=lambda fn, d, t: self.progress.emit(fn, d, t),
            )
            self.done.emit(summary)
        except Exception:
            self.failed.emit(traceback.format_exc())


# Helper to strip ANSI escape codes
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text)

# Module-level paste helpers
def _normalize_preview_stem(filename: str) -> str:
    """Strip extensions and hash suffixes to get the clean camera stem."""
    name, _ = os.path.splitext(filename)
    m = re.search(r'^(.*?)_[0-9a-fA-F]{16}$', name)
    if m:
        return m.group(1).upper()
    return name.upper()

def build_preview_index(previews_dir: str) -> dict:
    """Build a mapping of clean_stem.upper() -> full_preview_path from the previews folder."""
    idx = {}
    if not os.path.isdir(previews_dir):
        return idx
    try:
        for f in os.listdir(previews_dir):
            full = os.path.join(previews_dir, f)
            if os.path.isfile(full) and os.path.splitext(f)[1].lower() in PICTURE_EXTS:
                norm = _normalize_preview_stem(f)
                idx.setdefault(norm, []).append(full)
    except Exception:
        pass
    return idx

def deduplicate_paste_items(items: list) -> list:
    """De-duplicate paste items by stem, preferring the highest rating."""
    by_stem = {}
    for item in items:
        clean_stem = _normalize_preview_stem(item.filename)
        if clean_stem in by_stem:
            if item.rating > by_stem[clean_stem].rating:
                by_stem[clean_stem] = item
        else:
            by_stem[clean_stem] = item
    # Ensure every item has its stem updated to the clean stem
    for clean_stem, item in by_stem.items():
        item.stem = clean_stem
    return list(by_stem.values())

class AskCopyRawsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copy RAW Files Locally?")
        self.setModal(True)
        self.setFixedWidth(450)
        self.setStyleSheet(_stylesheet())
        
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(12)
        
        lbl = QLabel(
            "An external RAW directory is configured, but your local workspace/raws/ "
            "folder is empty.\n\n"
            "Would you like to copy the matching RAW files into your local workspace/raws/ "
            "directory while culling, so they are available offline?"
        )
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        
        h = QHBoxLayout()
        h.addStretch()
        
        self.btn_yes = QPushButton("Yes, Copy Locally")
        self.btn_yes.setObjectName("PrimaryBtn")
        self.btn_yes.clicked.connect(self.accept)
        h.addWidget(self.btn_yes)
        
        self.btn_no = QPushButton("No, Keep RAWs External")
        self.btn_no.setObjectName("WarnBtn")
        self.btn_no.clicked.connect(self.reject)
        h.addWidget(self.btn_no)
        
        v.addLayout(h)

class TaskerExecuteWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int)

    def __init__(self, workspace: str, raws_dir: str, db_path: str, copy_raws: bool):
        super().__init__()
        self.workspace = workspace
        self.raws_dir = raws_dir
        self.db_path = db_path
        self.copy_raws = copy_raws

    def run(self):
        class LogStream:
            def __init__(self, signal):
                self.signal = signal
            def write(self, text):
                if text:
                    self.signal.emit(text)
            def flush(self):
                pass

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = LogStream(self.log_signal)
        sys.stderr = LogStream(self.log_signal)

        try:
            from lvs_tasker import LVSTasker
            from pathlib import Path
            tasker = LVSTasker(Path(self.workspace))
            # External RAW source (D:\..., SD card, etc.) — RawIndex builds from this.
            # The local workspace/raws/ is ALWAYS the copy destination.
            if self.raws_dir:
                tasker.raws_source = Path(self.raws_dir)
            if self.db_path:
                tasker.db_path = Path(self.db_path)
            tasker.copy_raws_while_rating = self.copy_raws
            # GUI auto-deny: mismatches are logged prominently rather than
            # blocking the thread with an interactive prompt. Legitimate EXIF/rotation
            # modifications are now safely handled by the 16-hex suffix hash check.
            tasker.decision_callback = lambda fname, reasons: False
            code = tasker.execute()
            self.finished_signal.emit(code)
        except Exception as e:
            import traceback
            self.log_signal.emit(f"Error executing tasker: {e}\n{traceback.format_exc()}")
            self.finished_signal.emit(1)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

class PasteExecuteWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int)

    def __init__(self, workspace: str, previews_dir: str, raws_dir: str, db_path: str, paste_text: str, copy_raws: bool):
        super().__init__()
        self.workspace = workspace
        self.previews_dir = previews_dir
        self.raws_dir = raws_dir
        self.db_path = db_path
        self.paste_text = paste_text
        self.copy_raws = copy_raws

    def run(self):
        class LogStream:
            def __init__(self, signal):
                self.signal = signal
            def write(self, text):
                if text:
                    self.signal.emit(text)
            def flush(self):
                pass

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = LogStream(self.log_signal)
        sys.stderr = LogStream(self.log_signal)

        try:
            from lvs_tasker import (
                parse_paste_block, RawIndex, file_sha256, copy_raw_to_local,
                open_db_copy, write_task_md, populate_edit_db,
                find_exiftool, exiftool_set_rating, init_tasker_tables,
                query_ingest_metadata_by_stems, is_external_dir,
            )
            from pathlib import Path
            import shutil
            import sqlite3
            import time

            print("Starting Paste Ingest Pipeline...")
            raw_items = parse_paste_block(self.paste_text)
            if not raw_items:
                print("No valid files found in paste text.")
                self.finished_signal.emit(1)
                return

            deduped = deduplicate_paste_items(raw_items)
            print(f"Parsed {len(raw_items)} items; de-duplicated to {len(deduped)} unique stems.")

            prev_idx = build_preview_index(self.previews_dir)
            if not prev_idx:
                print(f"ERROR: No image previews found in '{self.previews_dir}'.")
                print("Did you accidentally select your RAW directory on the Previews line?")
                print("Please resolve this manually by selecting the correct Previews folder.")
                self.finished_signal.emit(1)
                return

            p_workspace = Path(self.workspace)
            p_raws = Path(self.raws_dir)
            p_db = Path(self.db_path)
            exiftool_bin = find_exiftool()
            print(f"EXIF tool: {exiftool_bin if exiftool_bin else '(not found - EXIF skipped)'}")

            raw_idx = None
            if p_raws.is_dir():
                raw_idx = RawIndex(p_raws)

            if p_db.exists():
                init_tasker_tables(p_db)

            db_hashes = {}
            if p_db.exists():
                try:
                    conn, temp_dir = open_db_copy(p_db)
                    for item in deduped:
                        row = None
                        try:
                            row = conn.execute(
                                "SELECT source_hash FROM files WHERE file_name LIKE ? LIMIT 1",
                                (f"{item.stem}.%",)
                            ).fetchone()
                        except sqlite3.OperationalError:
                            pass
                        if row and row[0]:
                            db_hashes[item.stem.upper()] = row[0]
                except Exception as exc:
                    print(f"Warning: Failed to fetch hashes from DB: {exc}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass

            select_dir = p_workspace / "select"
            select_dir.mkdir(parents=True, exist_ok=True)
            local_raws_dir = p_workspace / "raws"
            local_raws_dir.mkdir(parents=True, exist_ok=True)

            processed = []
            copied_raws_count = 0
            copied_previews_count = 0
            exif_preview_ok = 0
            exif_raw_ok = 0
            exif_local_raw_ok = 0
            ingest_rows = 0

            for idx, item in enumerate(deduped, 1):
                stem = item.stem
                rating = item.rating

                candidates = prev_idx.get(stem.upper(), [])
                if not candidates:
                    print(f"  [!][{idx}/{len(deduped)}] No preview found for stem {stem}")
                    continue

                prev_path = Path(candidates[0])
                prev_ext = prev_path.suffix

                raw_path = None
                raw_sha = None
                expected_hash = db_hashes.get(stem.upper())

                if raw_idx:
                    raw_path = raw_idx.resolve(stem, expected_hash=expected_hash)
                    if raw_path:
                        raw_sha = file_sha256(raw_path)

                local_raw_path = raw_path
                local_copy_made = False
                if self.copy_raws:
                    if raw_path and is_external_dir(raw_path, p_workspace):
                        local_raw_path = copy_raw_to_local(raw_path, local_raws_dir, expected_hash=raw_sha)
                        if local_raw_path:
                            copied_raws_count += 1
                            local_copy_made = True
                            if local_raw_path.stem != stem:
                                print(f"  [i] RAW collision resolved: copied as {local_raw_path.name}")

                dest_preview = select_dir / f"{stem}{prev_ext}"
                try:
                    shutil.copy2(str(prev_path), str(dest_preview))
                    copied_previews_count += 1
                except Exception as exc:
                    print(f"  [✗] Failed to copy preview {prev_path.name}: {exc}")
                    continue

                jpg_ok = False
                raw_original_ok = False
                raw_local_ok = False
                if exiftool_bin:
                    jpg_ok, msg = exiftool_set_rating(dest_preview, rating, exiftool_bin)
                    if jpg_ok:
                        exif_preview_ok += 1
                    else:
                        print(f"  [!] Preview EXIF failed for {dest_preview.name}: {msg}")
                    if local_copy_made and local_raw_path:
                        raw_local_ok, msg = exiftool_set_rating(local_raw_path, rating, exiftool_bin)
                        if raw_local_ok:
                            exif_local_raw_ok += 1
                        else:
                            print(f"  [!] Local RAW EXIF failed for {local_raw_path.name}: {msg}")
                    if raw_path:
                        raw_original_ok, msg = exiftool_set_rating(raw_path, rating, exiftool_bin)
                        if raw_original_ok:
                            exif_raw_ok += 1
                        else:
                            print(f"  [!] RAW EXIF failed for {raw_path.name}: {msg}")

                processed.append({
                    "rating": rating,
                    "clean_name": f"{stem}{prev_ext}",
                    "stem": stem,
                    "raw_path": local_raw_path,
                    "original_name": prev_path.name,
                    "hash_prefix": None,
                    "raw_sha256": raw_sha,
                })

                if p_db.exists():
                    try:
                        raw_for_db = local_raw_path if local_raw_path else raw_path
                        conn = sqlite3.connect(str(p_db), timeout=15)
                        try:
                            conn.execute("""
                                INSERT INTO tasker_ratings (
                                    created_at, select_path, select_stem, select_hash_prefix,
                                    select_clean_name, select_final_path,
                                    raw_path, raw_resolved, raw_ext, raw_sha256,
                                    user_rating, exif_select_ok, exif_raw_ok,
                                    db_linked, ingest_file_id, notes
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                time.strftime("%Y-%m-%dT%H:%M:%S"),
                                str(prev_path),
                                stem,
                                None,
                                dest_preview.name,
                                str(dest_preview),
                                str(raw_for_db) if raw_for_db else None,
                                1 if raw_for_db else 0,
                                raw_for_db.suffix.lower() if raw_for_db else None,
                                file_sha256(raw_for_db) if raw_for_db else None,
                                rating,
                                1 if jpg_ok else 0,
                                1 if raw_original_ok else 0,
                                1 if expected_hash else 0,
                                None,
                                f"paste_mode: {item.source_dir}; external_raw={raw_path}" if raw_path else f"paste_mode: {item.source_dir}",
                            ))
                            conn.commit()
                            ingest_rows += 1
                        finally:
                            conn.close()
                    except Exception as exc:
                        print(f"  [!] ingest.db tasker_ratings insert failed for {dest_preview.name}: {exc}")

            if not processed:
                print("No files were successfully processed.")
                self.finished_signal.emit(1)
                return

            print(f"Successfully staged {len(processed)} previews in select/.")
            if copied_raws_count > 0:
                print(f"Copied {copied_raws_count} RAW files locally.")
            print(
                f"EXIF results: {exif_preview_ok} previews, {exif_raw_ok} external RAWs, "
                f"{exif_local_raw_ok} local RAW copies. ingest.db rows: {ingest_rows}."
            )

            stems = [x["stem"] for x in processed]
            meta_cache = query_ingest_metadata_by_stems(stems, p_db)

            edit_db_path = p_workspace / "edit.db"
            written = populate_edit_db(processed, meta_cache, edit_db_path)
            print(f"Wrote {written} entries to {edit_db_path.name}.")

            md_path = p_workspace / "task.md"
            write_task_md(processed, meta_cache, md_path, p_workspace, raws_dir=local_raws_dir)
            print("Successfully wrote task.md.")
            self.finished_signal.emit(0)

        except Exception as e:
            import traceback
            self.log_signal.emit(f"Error executing paste: {e}\n{traceback.format_exc()}")
            self.finished_signal.emit(1)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


# ─────────────────────────────────────────────────────────────────────────────
# Main Tasker window
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Digikam probe worker — runs the diagnostic probe script in a subprocess and
# streams its (verbose, terminal-style) output into the Autocull log.
# ─────────────────────────────────────────────────────────────────────────────
class DigikamProbeWorker(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal(int)

    def __init__(self, workspace: str, db_path: str):
        super().__init__()
        self.workspace = workspace
        self.db_path = db_path

    def run(self):
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "digikam_probe.py")
        if not os.path.isfile(script):
            self.line.emit(f"[probe] script not found: {script}\n")
            self.done.emit(1)
            return
        try:
            proc = subprocess.Popen(
                [sys.executable, script, "--workspace", self.workspace,
                 "--db", self.db_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for ln in proc.stdout:
                self.line.emit(ln)
            proc.wait()
            self.done.emit(proc.returncode or 0)
        except Exception as e:
            self.line.emit(f"[probe] failed to launch: {e}\n")
            self.done.emit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Autocull sub-screen  (NON-transparent black overlay over the tasker window)
# ─────────────────────────────────────────────────────────────────────────────
class AutocullPanel(QWidget):
    """
    A MOCKUP of the future AI-DIRECTED cull, shown as a solid-black child
    overlay covering the whole tasker.  Two stacked pages:

      Page 0  — safety GATE: the (deliberately discouraging) warning + two
                buttons. "Back to safety" (left) closes; "Proceed with caution"
                (right) advances.
      Page 1  — the mixer: a LVS↔DigiKam blend slider (10% steps), its own
                disconnected log, and a "Run DigiKam probe" button that executes
                the real diagnostic script (it ignores the slider — it only
                probes how to talk to DigiKam on this machine).
    """

    WARNING = (
        "WARNING! Auto-Culling, For All Intents And Purposes, Is Just Science "
        "Fiction.\n\n"
        "While A Tiny Toy Model Could Give Its Opinion On Which Of The Photos "
        "It Prefers, This Judgement Is Basically Worthless, And Will Result In "
        "Wasting Millions Of Tokens And Valuable Time Dealing With Quality "
        "Edits Applied To The Wrong Photographs.\n\n"
        "AI-Assisted, Rather Than AI-Directed Culling Is Always The Correct "
        "Choice."
    )

    def __init__(self, parent, dm: LVSDataManager,
                 on_close: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.dm = dm
        self._on_close = on_close
        self._probe_thread: Optional[DigikamProbeWorker] = None

        # Solid black, opaque — this is a sub-screen, not a translucent HUD.
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            f"AutocullPanel {{ background-color: #000000; }}"
            f" QLabel {{ color: {FG}; }}")

        self.stack = QStackedWidget(self)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_gate())     # page 0
        self.stack.addWidget(self._build_mixer())    # page 1
        self.stack.setCurrentIndex(0)

    # ---- page 0: safety gate ----
    def _build_gate(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(60, 50, 60, 50)
        v.addStretch()
        title = QLabel("\U0001f480  AUTOCULL")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color:{RED}; font-size:22pt; font-weight:bold;")
        v.addWidget(title)
        v.addSpacing(18)
        msg = QLabel(self.WARNING)
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet(f"color:{FG}; font-size:12pt; line-height:150%;")
        v.addWidget(msg)
        v.addSpacing(28)
        row = QHBoxLayout()
        btn_back = QPushButton("\u2190  Back to safety")
        btn_back.setObjectName("PrimaryBtn")
        btn_back.clicked.connect(self._close)
        row.addWidget(btn_back)
        row.addStretch()
        btn_go = QPushButton("Proceed with caution  \u2192")
        btn_go.setObjectName("DangerBtn")
        btn_go.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        row.addWidget(btn_go)
        v.addLayout(row)
        v.addStretch()
        return page

    # ---- page 1: mixer + probe + log ----
    def _build_mixer(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(40, 28, 40, 28)
        v.setSpacing(12)

        hdr = QHBoxLayout()
        t = QLabel("\U0001f480  Autocull Mixer")
        t.setStyleSheet(f"color:{RED}; font-size:16pt; font-weight:bold;")
        hdr.addWidget(t); hdr.addStretch()
        btn_back = QPushButton("\u2190  Back to safety")
        btn_back.setObjectName("PrimaryBtn")
        btn_back.clicked.connect(self._close)
        hdr.addWidget(btn_back)
        v.addLayout(hdr)

        # Mixer labels
        lab = QHBoxLayout()
        left = QLabel("LVS\n(Aesthetic Scorer + Florence 2)")
        left.setStyleSheet(f"color:{GREEN}; font-weight:bold;")
        right = QLabel("DigiKam\n(MobileNet)")
        right.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.setStyleSheet(f"color:{BLUE}; font-weight:bold;")
        lab.addWidget(left); lab.addStretch(); lab.addWidget(right)
        v.addLayout(lab)

        # Slider: 10..100 in 10% steps (the DigiKam weight). Start at 10%.
        self.mix = QSlider(Qt.Orientation.Horizontal)
        self.mix.setMinimum(10); self.mix.setMaximum(100)
        self.mix.setSingleStep(10); self.mix.setPageStep(10)
        self.mix.setTickInterval(10)
        self.mix.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.mix.setValue(10)
        self.mix.valueChanged.connect(self._on_mix)
        v.addWidget(self.mix)

        self.mix_lbl = QLabel("")
        self.mix_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mix_lbl.setStyleSheet(f"color:{FG};")
        v.addWidget(self.mix_lbl)
        self._on_mix(self.mix.value())

        # Probe row
        prow = QHBoxLayout()
        self.btn_probe = QPushButton(" Run DigiKam probe (diagnostic) ")
        self.btn_probe.setObjectName("GhostBtn")
        self.btn_probe.setToolTip(
            "Runs digikam_probe.py — terminal-style diagnostic of how to talk "
            "to the local DigiKam (DB, DBus, CLI). Ignores the slider.")
        self.btn_probe.clicked.connect(self._run_probe)
        prow.addWidget(self.btn_probe)
        prow.addStretch()
        v.addLayout(prow)

        # Disconnected log (separate from the main tasker log)
        self.ac_log = QPlainTextEdit()
        self.ac_log.setReadOnly(True)
        self.ac_log.setFont(QFont("Consolas", 9))
        self.ac_log.setStyleSheet(
            f"background-color: {BG2}; color: {FG};"
            f" border: 1px solid {BG3}; border-radius: 4px;")
        v.addWidget(self.ac_log, 1)
        return page

    def _on_mix(self, dk: int):
        # Always snap to clean 10% increments — this is an auto-magic feature,
        # not a granular fader. No 54/64 oddities.
        snapped = max(10, min(100, int(round(dk / 10.0)) * 10))
        if snapped != dk:
            self.mix.blockSignals(True)
            self.mix.setValue(snapped)
            self.mix.blockSignals(False)
        self.mix_lbl.setText(
            f"Blend:  LVS {100 - snapped}%   \u00b7   DigiKam {snapped}%")

    def _aclog(self, s: str):
        self.ac_log.moveCursor(self.ac_log.textCursor().MoveOperation.End)
        self.ac_log.insertPlainText(s if s.endswith("\n") else s + "\n")
        sb = self.ac_log.verticalScrollBar(); sb.setValue(sb.maximum())

    def _run_probe(self):
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        self.btn_probe.setEnabled(False)
        self.ac_log.clear()
        self._aclog("[probe] launching digikam_probe.py ...")
        db = ""
        try:
            db = self.dm.db_path
        except Exception:
            pass
        self._probe_thread = DigikamProbeWorker(self.dm.base_path, db)
        self._probe_thread.line.connect(lambda s: self._aclog(s.rstrip("\n")))
        self._probe_thread.done.connect(self._probe_done)
        self._probe_thread.start()

    def _probe_done(self, code: int):
        self._aclog(f"[probe] finished (exit {code}).")
        self.btn_probe.setEnabled(True)
        self._probe_thread = None

    def _close(self):
        if self._probe_thread is not None and self._probe_thread.isRunning():
            self._probe_thread.quit()
            self._probe_thread.wait(1500)
        if self._on_close:
            self._on_close()


class LVSTaskerWindow(QMainWindow):
    """
    Standalone tasker window.  Lives in the SAME QApplication as the
    overlay (no Tk root, no second mainloop, no IPC mangling).
    """

    def __init__(
        self,
        dm: LVSDataManager,
        active_adapter: Optional[ViewerAdapter] = None,
        active_viewer_id: str = "faststone",
        on_close: Optional[Callable[[], None]] = None,
        on_viewer_change: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self.dm = dm
        self.active_adapter = active_adapter
        # Track which viewer is selected (needed for DigiKam cull write-back).
        self._active_viewer_id = active_viewer_id
        # Called with the new viewer id ("faststone"/"digikam") when the user
        # switches the picker.  lvs_main uses it to (dis)connect the FastStone
        # watcher so the overlay stops/starts.
        self._on_viewer_change = on_viewer_change
        self._on_close = on_close
        self._autocull_overlay: Optional[QWidget] = None
        self._copy_thread: Optional[QThread] = None
        self._exec_thread: Optional[QThread] = None
        self._paste_thread: Optional[QThread] = None

        self.setWindowTitle("LVS Tasker")
        self.setMinimumSize(820, 420)
        self.resize(940, 500)
        self.setWindowOpacity(0.90)  # High-polish 90% opacity
        self.setStyleSheet(_stylesheet())

        # Paste-block execution state: once tested+executed, block is immutable
        self._paste_executed: bool = False
        self._paths_ready: bool = False   # gates path persistence during init

        self._build()
        self._reload_paths_from_dm()
        self._restyle_paths()
        self._refresh_counts()

        # Live refresh (no clock / status bar — removed in v1.0.8)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_counts)
        self._tick.timeout.connect(self._restyle_paths)
        self._tick.start(2000)

    # ------------------------------------------------------------- build
    def _build(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ============================ Paths ==============================
        paths_box = QGroupBox()           # no header — the paths are self-evident
        paths_box.setStyleSheet("QGroupBox { margin-top: 2px; }")
        paths_layout = QVBoxLayout(paths_box)
        paths_layout.setContentsMargins(10, 8, 10, 8)
        paths_layout.setSpacing(6)

        self.row_ws    = PathRow("Workspace")
        self.row_prev  = PathRow("Previews")
        self.row_raws  = PathRow(
            "RAWs",
            toggle_text="copy to ./raws",
            toggle_tip=("When the RAWs path is a valid source, the matching RAW "
                        "originals are copied into this workspace's ./raws "
                        "during the cull. ON by default."))
        self.row_db    = PathRow("Database")
        for row, on_browse in [
            (self.row_ws,   self._browse_workspace),
            (self.row_prev, lambda: self._browse_dir(self.row_prev,
                                                    "Pick previews folder")),
            (self.row_raws, lambda: self._browse_dir(self.row_raws,
                                                    "Pick RAW source root")),
            (self.row_db,   self._browse_db),
        ]:
            row.changed.connect(self._restyle_paths)
            row.changed.connect(self._save_paths)
            row.btn.clicked.connect(on_browse)
            row.changed.connect(self._on_raws_changed)  # noop except for raws
            paths_layout.addWidget(row)

        # Single "Auto-detect from Workspace" action (the old separate
        # "re-detect" button was a duplicate refresh — removed).
        self.btn_autodetect = QPushButton(" \u21bb  Auto-detect from Workspace")
        self.btn_autodetect.setObjectName("GhostBtn")
        self.btn_autodetect.clicked.connect(self._auto_detect_from_workspace)
        paths_layout.addWidget(self.btn_autodetect)

        root.addWidget(paths_box)

        # ============================ Viewer selector ====================
        # Two switchable boxes. FastStone is box 1 (green/selected); Digikam is
        # box 2 (switchable).  Selecting Digikam DISCONNECTS FastStone + the
        # overlay (handled by lvs_main via on_viewer_change) and reveals the
        # red skull "Autocull" button to the right.
        viewer_row = QHBoxLayout(); viewer_row.setSpacing(8)
        self.viewer_boxes: List[ViewerBox] = []
        if not hasattr(self, "_active_viewer_id") or not self._active_viewer_id:
            self._active_viewer_id = "faststone"
        fs_box = ViewerBox("faststone", "FastStone", available=True)
        dk_box = ViewerBox("digikam", "Digikam", available=True)
        for vb in (fs_box, dk_box):
            vb.clicked.connect(self._on_viewer_pick)
            self.viewer_boxes.append(vb)
            viewer_row.addWidget(vb, 1)
        fs_box.set_selected(True)

        # Red skull Autocull button — only meaningful in Digikam mode.
        self.btn_autocull = QPushButton("\U0001f480  Autocull")
        self.btn_autocull.setObjectName("DangerBtn")
        # Locked size/shape so the viewer row never reflows when it toggles.
        self.btn_autocull.setFixedWidth(140)
        self.btn_autocull.setMinimumHeight(40)
        self.btn_autocull.setSizePolicy(QSizePolicy.Policy.Fixed,
                                        QSizePolicy.Policy.Fixed)
        self.btn_autocull.setToolTip(
            "Radical AI-DIRECTED cull (Digikam mode). Disabled without ingest.db.")
        self.btn_autocull.clicked.connect(self._open_autocull)
        self.btn_autocull.setVisible(False)   # hidden until Digikam is selected
        viewer_row.addWidget(self.btn_autocull)
        root.addLayout(viewer_row)

        # ============================ Star buckets =======================
        # No "Star buckets" header — the 1★..5★ cells are self-describing.
        buckets_box = QFrame()
        buckets_box.setStyleSheet("QFrame { border: none; }")
        bv = QVBoxLayout(buckets_box); bv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout(); row.setSpacing(8)
        self.buckets: List[StarBucket] = []
        for i in range(1, SELECT_COUNT + 1):
            b = StarBucket(i); b.clicked.connect(self._open_bucket_folder)
            self.buckets.append(b); row.addWidget(b, 1)
        bv.addLayout(row)
        root.addWidget(buckets_box)

        # ============================ Mode A =============================
        mode_a_box = QGroupBox(" Finalize cull ")
        ma = QHBoxLayout(mode_a_box)
        self.btn_normal = QPushButton(" \u25b6  Run ")
        self.btn_normal.setObjectName("PrimaryBtn")
        self.btn_normal.setToolTip(
            "Collect your rated picks, tag them, and write the editing manifest.")
        self.btn_normal.clicked.connect(self._run_normal_execute)
        ma.addWidget(self.btn_normal)
        self.lbl_normal_desc = QLabel("Collects your rated picks and prepares them for editing.")
        self.lbl_normal_desc.setStyleSheet(f"color: {FG_DIM}; font-size: 9pt;")
        ma.addWidget(self.lbl_normal_desc)
        ma.addStretch()
        root.addWidget(mode_a_box)

        # ============================ Mode B =============================
        mode_b_box = QGroupBox(" Paste a file listing ")
        mb = QVBoxLayout(mode_b_box)
        # The paste widget doubles as a terminal readout after execute:
        # before execute  → editable input box with placeholder
        # after test+lock → read-only, monospace, shows live execution log
        self.paste = QPlainTextEdit()
        self.paste.setPlaceholderText(
            "Paste a folder listing here (Windows dir, PowerShell Get-ChildItem, "
            "or ls). LVS reads the filenames and which star folder each was in.\n"
            "Test Parse first, then Execute.")
        # Compact 2-line box that grows to ~8 lines the moment anything is typed
        # or pasted, and shrinks back when emptied.  (While in live-terminal
        # mode after Execute, it stays expanded.)
        self._paste_h_small = self._lines_to_px(self.paste, 2)
        self._paste_h_big = self._lines_to_px(self.paste, 8)
        self.paste.setFixedHeight(self._paste_h_small)
        self.paste.textChanged.connect(self._autosize_paste)
        mb.addWidget(self.paste)
        row_b = QHBoxLayout()
        self.btn_test = QPushButton(" \u2713  Test Parse")
        self.btn_test.setObjectName("WarnBtn")
        self.btn_test.clicked.connect(self._test_parse)
        row_b.addWidget(self.btn_test)
        self.btn_exec_paste = QPushButton(
            " \u25b6  Execute Paste")
        self.btn_exec_paste.setObjectName("PrimaryBtn")
        self.btn_exec_paste.clicked.connect(self._run_paste_execute)
        self.btn_exec_paste.setEnabled(False)
        row_b.addWidget(self.btn_exec_paste)
        row_b.addStretch()
        self.btn_reset = QPushButton(" \u2715  Reset")
        self.btn_reset.setObjectName("DangerBtn")
        self.btn_reset.clicked.connect(self._reset_paste_box)
        row_b.addWidget(self.btn_reset)
        mb.addLayout(row_b)
        root.addWidget(mode_b_box)

        # RAW copy-back is no longer a separate panel — it's the inline
        # "copy to ./raws" toggle on the RAWs path row, honoured by Finalize.

        # ============================ Log ================================
        # The Log group is HIDDEN while empty and slides open (animated max-
        # height) the first time anything is logged.  No clock / status bar.
        self.log_box = QGroupBox(" Log ")
        lv = QVBoxLayout(self.log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setStyleSheet(
            f"background-color: {BG3}; color: {FG};"
            f" border: 1px solid {BG2}; border-radius: 4px;")
        lv.addWidget(self.log)
        self.log_box.setVisible(False)          # hidden until first log line
        self.log_box.setMaximumHeight(0)
        root.addWidget(self.log_box, 1)         # log takes available space when revealed
        # Minimal trailing spacer so groups stay packed at the top when the log
        # is hidden.  Factor 0 means it collapses to nothing; the log box fills
        # the rest when revealed.
        root.addStretch(0)
        self._log_anim: Optional[QPropertyAnimation] = None
        self._log_revealed = False

        # Indicator for new log messages when scrolled up
        self.indicator = QPushButton("↓", self.log)
        self.indicator.setFixedSize(30, 30)
        self.indicator.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self.indicator.setCursor(Qt.CursorShape.PointingHandCursor)
        self.indicator.setStyleSheet(f"background-color: {BG_OK}; color: {GREEN}; border: 1px solid {GREEN}; border-radius: 15px;")
        
        self.indicator_effect = QGraphicsOpacityEffect(self.indicator)
        self.indicator.setGraphicsEffect(self.indicator_effect)
        self.indicator_anim = QPropertyAnimation(self.indicator_effect, b"opacity")
        self.indicator_anim.setDuration(800)
        self.indicator_anim.setStartValue(0.3)
        self.indicator_anim.setEndValue(1.0)
        self.indicator_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.indicator_anim.setLoopCount(-1)
        
        self.indicator.clicked.connect(self._scroll_to_bottom)
        self.indicator.hide()

        self.log.installEventFilter(self)
        self.log.verticalScrollBar().valueChanged.connect(self._on_log_scroll)

    # ----------------------------------------------------- helpers
    # ----------------------------------------------------- paste autosize
    @staticmethod
    def _lines_to_px(edit: QPlainTextEdit, lines: int) -> int:
        fm = edit.fontMetrics()
        return int(fm.lineSpacing() * lines + 14)

    def _autosize_paste(self):
        """2 lines when empty, ~8 lines once there is any content."""
        # Don't fight the live-terminal mode (stays expanded after Execute).
        if getattr(self, "_paste_executed", False):
            return
        has_text = bool(self.paste.toPlainText().strip())
        target = self._paste_h_big if has_text else self._paste_h_small
        if self.paste.height() != target:
            self.paste.setFixedHeight(target)

    def _reveal_log(self):
        """Reveal the Log group with a one-time slide-open animation."""
        if self._log_revealed:
            return
        self._log_revealed = True
        self.log_box.setVisible(True)
        target = 200
        anim = QPropertyAnimation(self.log_box, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setStartValue(0)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Keep the Log a fixed, compact panel after revealing (the trailing
        # stretch absorbs slack) so it never balloons or collides with Mode B.
        anim.start()
        self._log_anim = anim  # keep a reference

    def eventFilter(self, obj, event):
        if obj == self.log and event.type() == QEvent.Type.Resize:
            rect = self.log.rect()
            # position the indicator in the bottom right corner, accounting for scrollbar
            sb_width = self.log.verticalScrollBar().width() if self.log.verticalScrollBar().isVisible() else 15
            self.indicator.move(rect.width() - sb_width - 35, rect.height() - 35)
        return super().eventFilter(obj, event)

    def _scroll_to_bottom(self):
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.indicator.hide()
        self.indicator_anim.stop()

    def _on_log_scroll(self, value):
        sb = self.log.verticalScrollBar()
        if value >= sb.maximum() - 2:
            self.indicator.hide()
            self.indicator_anim.stop()

    def _show_scroll_indicator(self):
        if not self.indicator.isVisible():
            rect = self.log.rect()
            sb_width = self.log.verticalScrollBar().width() if self.log.verticalScrollBar().isVisible() else 15
            self.indicator.move(rect.width() - sb_width - 35, rect.height() - 35)
            self.indicator.show()
            self.indicator_anim.start()

    def _log(self, msg: str):
        self._reveal_log()
        sb = self.log.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 2
        
        self.log.appendPlainText(msg.rstrip())
        
        if was_at_bottom:
            sb.setValue(sb.maximum())
        else:
            self._show_scroll_indicator()

    # ----------------------------------------------------- path I/O
    # ----------------------------------------------------- persistence
    @staticmethod
    def _paths_state_path() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "select_settings.json")

    def _load_saved_paths(self) -> dict:
        return load_tasker_paths()

    def _save_paths(self):
        # Don't persist while we're still populating the fields at startup.
        if not getattr(self, "_paths_ready", False):
            return
        save_tasker_paths({
            "workspace": self.row_ws.text(),
            "previews":  self.row_prev.text(),
            "database":  self.row_db.text(),
            # RAWs are intentionally NOT persisted: they live on whatever
            # SD/USB drive (D:/E:/...) the end-user plugs in this session.
        })

    def _reload_paths_from_dm(self):
        saved = self._load_saved_paths()
        self.row_ws.setText(saved.get("workspace") or self.dm.base_path)
        # Prefer saved → DB-stored previews dir → workspace/previews
        db_prev = self.dm.get_previews_dir_from_db()
        self.row_prev.setText(
            saved.get("previews") or db_prev or self.dm.previews_dir)
        # RAWs are NEVER restored from disk (per-session removable media).
        self.row_raws.setText(
            self.dm._raws_root_override or self.dm.default_raws)
        self.row_db.setText(saved.get("database") or self.dm.db_path)
        self._paths_ready = True

    def _restyle_paths(self):
        # Workspace - green if directory exists, else red
        ws = self.row_ws.text()
        if ws and os.path.isdir(ws):
            self.row_ws.set_state("ok")
        else:
            self.row_ws.set_state("bad")

        # Previews - green if dir exists and has >=1 image, yellow if empty, red if missing
        pv = self.row_prev.text()
        if pv and os.path.isdir(pv):
            try:
                has_any = any(
                    os.path.splitext(f)[1].lower() in PICTURE_EXTS
                    for f in os.listdir(pv)
                    if os.path.isfile(os.path.join(pv, f)))
                self.row_prev.set_state("ok" if has_any else "warn")
            except Exception:
                self.row_prev.set_state("bad")
        else:
            self.row_prev.set_state("bad")

        # RAWs - optional. green if dir and raws exists, yellow if empty/missing, neutral if not set
        rw = self.row_raws.text()
        raws_valid_external = False
        if not rw:
            self.row_raws.set_state("neutral")
        elif os.path.isdir(rw):
            try:
                has_raw = False
                for dirpath, _d, files in os.walk(rw):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in RAW_EXTS:
                            has_raw = True; break
                    if has_raw: break
                self.row_raws.set_state("ok" if has_raw else "warn")
                # The inline "copy to ./raws" toggle appears only when the RAWs
                # path is a valid RAW source that ISN'T already the workspace's
                # own ./raws.
                raws_valid_external = (
                    has_raw and os.path.abspath(rw) != os.path.abspath(self.dm.default_raws))
            except Exception:
                self.row_raws.set_state("bad")
        else:
            self.row_raws.set_state("warn" if rw else "neutral")

        # Inline "copy to ./raws" toggle: visible only for a valid external RAW src.
        self.row_raws.set_toggle_visible(raws_valid_external)

        # Database - green if exists, yellow if parent directory exists but file missing, red otherwise
        db = self.row_db.text()
        if db and os.path.isfile(db):
            self.row_db.set_state("ok")
        elif db and os.path.isdir(os.path.dirname(db)):
            self.row_db.set_state("warn")
        else:
            self.row_db.set_state("bad")

    def _browse_workspace(self):
        d = QFileDialog.getExistingDirectory(
            self, "Pick workspace directory", self.row_ws.text() or "")
        if d:
            self.row_ws.setText(d)
            self._auto_detect_from_workspace(silent=True)

    def _browse_dir(self, row: PathRow, title: str):
        d = QFileDialog.getExistingDirectory(self, title, row.text() or "")
        if d:
            row.setText(d)

    def _browse_db(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Pick ingest.db",
            os.path.dirname(self.row_db.text()) or "", "SQLite (*.db *.sqlite)")
        if f:
            self.row_db.setText(f)

    def _on_raws_changed(self):
        """
        Picks up RAWs path edits: reflect to the dm override, show the inline
        "copy to ./raws" toggle for a valid external source, AND try to
        auto-detect a Previews folder living in/near the RAWs root (photogs
        usually sort previews right next to their RAWs — it's cheap to check).
        """
        rw = self.row_raws.text().strip()
        if rw and os.path.isdir(rw) and rw != self.dm.default_raws:
            self.dm.set_raws_root_override(rw)
            self._probe_previews_near_raws(rw)
        elif not rw:
            self.dm.set_raws_root_override(None)
        # toggle visibility decided centrally in _restyle_paths()

    def _probe_previews_near_raws(self, raws_dir: str):
        """
        Autodetect maximization: when a RAWs dir is set, look for a previews
        folder (a) INSIDE it and (b) as a SIBLING at the same depth (e.g. the
        RAWs are on D: so the previews are likely on D: too).  Only fills the
        Previews field if it's currently empty or doesn't resolve to images.
        """
        cur = self.row_prev.text().strip()
        if cur and self._dir_has_any_ext(cur, PICTURE_EXTS):
            return  # already have a good previews dir — don't clobber it
        parent = os.path.dirname(os.path.normpath(raws_dir))
        names = ("previews", "Previews", "preview", "jpegs", "JPG", "jpg")
        candidates = [os.path.join(raws_dir, n) for n in names]      # inside
        candidates += [os.path.join(parent, n) for n in names]       # sibling
        for c in candidates:
            if self._dir_has_any_ext(c, PICTURE_EXTS):
                self.row_prev.setText(c)
                self._log(f"[autodetect] previews found near RAWs: {c}")
                self._restyle_paths()
                self._save_paths()
                return

    def _dir_has_any_ext(self, root: str, exts: set[str], recursive: bool = False) -> bool:
        if not root or not os.path.isdir(root):
            return False
        walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
        try:
            for dirpath, _dirs, files in walker:
                for name in files:
                    full = os.path.join(dirpath, name)
                    if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
                        return True
                if not recursive:
                    break
        except Exception:
            return False
        return False

    # ----------------------------------------------------- auto-detect
    def _auto_detect_from_workspace(self, silent: bool = False):
        ws = self.row_ws.text().strip()
        if not ws or not os.path.isdir(ws):
            if not silent:
                QMessageBox.warning(self, "Auto-detect",
                                    f"Workspace not a valid directory:\n{ws}")
            return

        # Move dm to the new workspace if changed
        if os.path.abspath(ws) != self.dm.base_path:
            self._log(f"[autodetect] re-pointing workspace to {ws}")
            self.dm = LVSDataManager(ws)
            self._reload_paths_from_dm()

        # Re-derive previews + database. Prefer real populated folders.
        local_prev = os.path.join(ws, "previews")
        db_prev = self.dm.get_previews_dir_from_db()
        preview_candidates = [db_prev, local_prev, os.path.join(ws, "Previews"), os.path.join(ws, "selected")]
        for candidate in preview_candidates:
            if candidate and self._dir_has_any_ext(candidate, PICTURE_EXTS):
                self.row_prev.setText(candidate)
                break
        else:
            self.row_prev.setText(db_prev if db_prev else local_prev)

        local_db = os.path.join(ws, "ingest.db")
        if os.path.isfile(local_db):
            self.row_db.setText(local_db)

        # Prefer local RAWs only when actually populated; otherwise find a
        # likely external source for copy-while-rating workflows.
        local_raws = os.path.join(ws, "raws")
        if self._dir_has_any_ext(local_raws, RAW_EXTS, recursive=True):
            self.row_raws.setText(local_raws)
        else:
            raw_candidates = [
                r"D:\DCIM",
                r"E:\DCIM",
                r"F:\DCIM",
                os.path.join(ws, "RAWS"),
                os.path.join(ws, "RAW"),
            ]
            for candidate in raw_candidates:
                if self._dir_has_any_ext(candidate, RAW_EXTS, recursive=True):
                    self.row_raws.setText(candidate)
                    break

        self._restyle_paths()
        self._save_paths()

        if not silent:
            self._log("[autodetect] complete")

    # ----------------------------------------------------- viewer selector
    def _on_viewer_pick(self, viewer_id: str):
        """
        Switch the active viewer.

        FastStone  -> reconnect the live FastStone watcher + overlay.
        Digikam    -> DISCONNECT FastStone (overlay goes blank — nothing to
                      match to) and reveal the red skull Autocull button.
        The actual (dis)connection is performed by lvs_main via the
        on_viewer_change callback; here we only update the UI + notify.
        """
        if viewer_id == self._active_viewer_id:
            return
        self._active_viewer_id = viewer_id
        for vb in self.viewer_boxes:
            vb.set_selected(vb.viewer_id == viewer_id)

        is_dk = (viewer_id == "digikam")
        self.btn_autocull.setVisible(is_dk)
        if is_dk:
            db_ok = os.path.isfile(self.row_db.text().strip() or self.dm.db_path)
            self.btn_autocull.setEnabled(db_ok)
            self.btn_autocull.setToolTip(
                "Radical AI-DIRECTED cull." if db_ok else
                "Autocull disabled — ingest.db not found in the workspace.")
            self._log("[viewer] Digikam selected — FastStone + overlay "
                      "DISCONNECTED (nothing to match to).")
        else:
            self._log("[viewer] FastStone selected — live overlay reconnected.")

        # Tell the host (lvs_main) to (dis)connect the watcher/overlay.
        if self._on_viewer_change:
            try:
                self._on_viewer_change(viewer_id)
            except Exception as e:
                self._log(f"[viewer] host switch error: {e}")

    # ----------------------------------------------------- Autocull (mockup)
    def _open_autocull(self):
        """
        Open the NON-transparent black Autocull sub-screen (a child overlay that
        covers the whole tasker window).  This is a MOCKUP of the future
        AI-directed cull: it shows the safety warning gate, then a LVS↔DigiKam
        score mixer and its own disconnected log, plus a button that runs the
        real diagnostic probe script against the local DigiKam install.
        """
        if not os.path.isfile(self.row_db.text().strip() or self.dm.db_path):
            QMessageBox.warning(
                self, "Autocull unavailable",
                "Autocull needs ingest.db (LVS scores) in the workspace.")
            return
        if self._autocull_overlay is not None:
            return
        self._autocull_overlay = AutocullPanel(
            self, dm=self.dm,
            on_close=self._close_autocull)
        self._autocull_overlay.setGeometry(self.centralWidget().rect())
        self._autocull_overlay.show()
        self._autocull_overlay.raise_()

    def _close_autocull(self):
        if self._autocull_overlay is not None:
            self._autocull_overlay.hide()
            self._autocull_overlay.deleteLater()
            self._autocull_overlay = None

    def resizeEvent(self, event):
        # keep the Autocull overlay covering the whole central area
        if self._autocull_overlay is not None:
            self._autocull_overlay.setGeometry(self.centralWidget().rect())
        super().resizeEvent(event)

    def _refresh_counts(self):
        try:
            per = self.dm.get_select_picture_counts()
            for b, n in zip(self.buckets, per):
                b.set_count(n)
        except Exception as e:
            self._log(f"[refresh] {e}")

    def _open_bucket_folder(self, idx: int):
        """Open the selectN folder for the clicked star bucket (creating it
        if needed so the user can drop files in)."""
        folder = self.dm.select_folders[idx - 1]
        try:
            os.makedirs(folder, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)          # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self._log(f"[open] could not open folder {folder}: {e}")

    # ----------------------------------------------------- parse
    def _reset_paste_box(self):
        if not self.btn_exec_paste.isEnabled() and not getattr(self, "_paste_executed", False) and self.paste.toPlainText() == "":
            return  # already clean
            
        self._paste_executed = False
        self.paste.setReadOnly(False)
        self.paste.setPlaceholderText(
            "Paste the output of `dir /s` or `Get-ChildItem -Recurse` "
            "or `ls -R` here. The tasker will parse filenames + selectN bucket.\n"
            "After Test Parse succeeds, Execute is unlocked. "
            "Once execution starts, this box becomes the live terminal."
        )
        self.paste.setPlainText("")
        self.paste.setFont(QFont("Segoe UI", 10))
        # back to the compact 2-line box
        self._paste_h_small = self._lines_to_px(self.paste, 2)
        self._paste_h_big = self._lines_to_px(self.paste, 8)
        self.paste.setFixedHeight(self._paste_h_small)
        self.btn_exec_paste.setEnabled(False)
        self.btn_test.setEnabled(True)
        self._log("[paste] reset")

    def _test_parse(self):
        text = self.paste.toPlainText()
        if not text.strip():
            self._log("[parse] paste box empty")
            return
        from lvs_tasker import parse_paste_block
        items = parse_paste_block(text)
        self._log(f"[parse] successfully parsed {len(items):,} items:")
        if items:
            for it in items[:8]:
                self._log(f"    - {it.filename} (Rating {it.rating}★, Dir: {it.source_dir})")
            if len(items) > 8:
                self._log(f"    ... and {len(items) - 8} more")
            # Lock paste box — immutable now until Reset
            self.paste.setReadOnly(True)
            self.btn_exec_paste.setEnabled(True)
            self.btn_test.setEnabled(False)
            self._log(f"[parse] {len(items):,} items ready — click Execute Paste")
            self._log("[parse] paste locked ▶ click Execute Paste; use Reset to re-edit")
        else:
            self.btn_exec_paste.setEnabled(False)

    # ----------------------------------------------------- execution
    def _run_normal_execute(self):
        ws = self.row_ws.text().strip()
        if not ws or not os.path.isdir(ws):
            QMessageBox.warning(self, "Execute Error", "Please specify a valid Workspace directory.")
            return

        raws = self.row_raws.text().strip()
        # Honour the inline RAWs "copy to ./raws" toggle: when ON (and the RAWs
        # path is a valid external source) the matching RAW originals are copied
        # into ./raws during this cull.
        copy_raws = self.row_raws.toggle_on()
        if copy_raws:
            self._log("[execute] copy-to-./raws is ON for this run")

        self.btn_normal.setEnabled(False)
        self._log("[execute] Starting Normal Ingest Pipeline...")

        # Spawn worker thread
        self._exec_thread = TaskerExecuteWorker(ws, raws, self.row_db.text().strip(), copy_raws)
        self._exec_thread.log_signal.connect(self._log_from_thread)
        self._exec_thread.finished_signal.connect(self._normal_execute_finished)
        self._exec_thread.start()

    def _normal_execute_finished(self, code: int):
        self.btn_normal.setEnabled(True)
        if code == 0:
            self._log("[execute] Normal Ingest Pipeline completed successfully.")
            self._refresh_counts()
            if self._writeback_to_digikam():
                self._open_digikam_if_written()
        else:
            self._log("[execute] Normal Ingest Pipeline failed.")
        self._exec_thread = None

    def _writeback_to_digikam(self) -> bool:
        """Push cull results into DigiKam's DB if one exists.
        Opportunistic — writes regardless of which viewer is active.
        If digikam4.db is found, the cull is mirrored there."""
        try:
            from lvs_digikam import DigikamCullWriter, _find_digikam_db
            db_path = _find_digikam_db()
            if not db_path:
                return False  # no DigiKam on this system — silent skip
            self._log("[DigiKam] Found database — mirroring cull results...")
            writer = DigikamCullWriter(db_path)
            ws = self.row_ws.text().strip()
            select_dir = os.path.join(ws, "select")
            selected = []
            if os.path.isdir(select_dir):
                selected = [f for f in os.listdir(select_dir)
                           if os.path.isfile(os.path.join(select_dir, f))]
            if not selected:
                self._log("[DigiKam] No files in select/ to write back.")
                return False
            result = writer.write_cull_results(selected=selected, rated={})
            if result.get("success"):
                self._log(f"[DigiKam] Cull written: {result['selected']} accepted, "
                         f"{result['rejected']} rejected, "
                         f"{result['rated']} rated — restart DigiKam to see results.")
                return True
            else:
                self._log(f"[DigiKam] Write-back failed: {result.get('error', 'unknown')}")
                return False
        except Exception as e:
            self._log(f"[DigiKam] Write-back error: {e}")
            return False

    def _open_digikam_if_written(self):
        """Launch DigiKam and navigate to the target album (the select/ folder)."""
        import subprocess
        candidates = [
            r"C:\Program Files\digiKam\digikam.exe",
            r"C:\Program Files (x86)\digiKam\digikam.exe",
        ]
        dk_exe = None
        for c in candidates:
            if os.path.isfile(c):
                dk_exe = c
                break
        if not dk_exe:
            dk_exe = shutil.which("digikam")
        if not dk_exe:
            self._log("[DigiKam] Could not find digikam.exe to launch.")
            return

        ws = self.row_ws.text().strip()
        target_album = os.path.join(ws, "select")

        # Don't prevent launching if already running — passing the path to
        # the existing instance tells it to switch to that Album.
        try:
            subprocess.Popen([dk_exe, target_album], cwd=os.path.dirname(dk_exe))
            self._log(f"[DigiKam] Opening album: {target_album}")
            self._log("[DigiKam] Use Alt+0/1/2/3 for Pick Labels, Ctrl+0..5 for stars.")
        except Exception as e:
            self._log(f"[DigiKam] Launch failed: {e}")

    def _run_paste_execute(self):
        ws    = self.row_ws.text().strip()
        prevs = self.row_prev.text().strip()
        raws  = self.row_raws.text().strip()
        db    = self.row_db.text().strip()
        text  = self.paste.toPlainText()

        if not ws or not os.path.isdir(ws):
            QMessageBox.warning(self, "Execute Error", "Please specify a valid Workspace directory.")
            return
        if not prevs or not os.path.isdir(prevs):
            QMessageBox.warning(self, "Execute Error", "Please specify a valid Previews directory.")
            return
        if not text.strip():
            QMessageBox.warning(self, "Execute Error", "Paste box is empty.")
            return

        # Honour the inline RAWs "copy to ./raws" toggle (see _run_normal_execute).
        copy_raws = self.row_raws.toggle_on()
        if copy_raws:
            self._log("[paste] copy-to-./raws is ON for this run")

        # ---- Paste block → terminal mode ----
        # The input is now committed. Transform the paste box into the live
        # terminal readout so the user can watch output in context.
        self._paste_executed = True
        self.paste.setReadOnly(True)
        self.paste.clear()
        self.paste.setFont(QFont("Consolas", 8))
        self.paste.setPlaceholderText("")
        # live terminal: keep it expanded
        self.paste.setFixedHeight(self._lines_to_px(self.paste, 12))
        self.paste.appendPlainText("[terminal] Paste Ingest Pipeline starting...")
        self.btn_exec_paste.setEnabled(False)
        self.btn_test.setEnabled(False)
        self._log("[paste] Executing...")
        self._log("[paste] paste block committed — this box is now the live terminal")

        # Spawn worker thread
        self._paste_thread = PasteExecuteWorker(ws, prevs, raws, db, text, copy_raws)
        self._paste_thread.log_signal.connect(self._paste_terminal_output)
        self._paste_thread.log_signal.connect(self._log_from_thread)
        self._paste_thread.finished_signal.connect(self._paste_execute_finished)
        self._paste_thread.start()

    def _paste_terminal_output(self, text: str):
        """Route worker output to the paste box while it is in terminal mode."""
        if self._paste_executed:
            cleaned = strip_ansi(text)
            self.paste.moveCursor(self.paste.textCursor().MoveOperation.End)
            self.paste.insertPlainText(cleaned)
            sb = self.paste.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _paste_execute_finished(self, code: int):
        if code == 0:
            self.paste.appendPlainText("\n[terminal] ✓ Completed successfully.")
            self._log("[paste] Paste Ingest Pipeline completed successfully.")
            self._log("[paste] Done — use Reset to run again")
            self._refresh_counts()
            if self._writeback_to_digikam():
                self._open_digikam_if_written()
        else:
            self.paste.appendPlainText("\n[terminal] ✗ Pipeline failed — see log above.")
            self._log("[paste] Paste Ingest Pipeline failed.")
            self._log("[paste] FAILED")
        self._paste_thread = None

    def _log_from_thread(self, text: str):
        self._reveal_log()
        cleaned = strip_ansi(text)
        sb = self.log.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 2
        
        self.log.insertPlainText(cleaned)
        
        if was_at_bottom:
            sb.setValue(sb.maximum())
        else:
            self._show_scroll_indicator()

    # ----------------------------------------------------- placeholders
    def _not_yet(self):
        QMessageBox.information(
            self, f"{__product_name__}",
            "This action is reserved for v1.0.5 once the legacy "
            "lvs_tasker.py backend is fully wired into the Qt GUI.")

    # ----------------------------------------------------- close
    def closeEvent(self, event):
        for attr in ("_copy_thread", "_exec_thread", "_paste_thread"):
            try:
                t = getattr(self, attr, None)
                if t is not None:
                    try:
                        if t.isRunning():
                            t.quit()
                            t.wait(2000)
                    except RuntimeError:
                        pass
            except Exception:
                pass
        if self._on_close:
            try: self._on_close()
            except Exception: pass
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
# Public launcher contract
# ─────────────────────────────────────────────────────────────────────────────
_tasker_window: Optional[LVSTaskerWindow] = None


def open_tasker(
    dm: LVSDataManager,
    active_adapter: Optional[ViewerAdapter] = None,
    placeholder_adapters: Optional[List[ViewerAdapter]] = None,  # unused now
    active_viewer_id: str = "faststone",
    on_close: Optional[Callable[[], None]] = None,
    on_viewer_change: Optional[Callable[[str], None]] = None,
    blocking: bool = False,
) -> "LVSTaskerWindow":
    """
    Open the tasker window in the SAME QApplication as the overlay.

    Unlike v1.0.3 (Tkinter), there is no second mainloop — the window is
    simply shown.  Re-entrant: clicking "Open Tasker" twice raises the
    existing window instead of spawning a duplicate.
    """
    global _tasker_window
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    if _tasker_window is not None and _tasker_window.isVisible():
        _tasker_window.raise_()
        _tasker_window.activateWindow()
        return _tasker_window

    def _on_close_wrapper():
        def delayed_cleanup():
            global _tasker_window
            _tasker_window = None
        QTimer.singleShot(0, delayed_cleanup)
        if on_close:
            on_close()

    _tasker_window = LVSTaskerWindow(
        dm, active_adapter=active_adapter,
        active_viewer_id=active_viewer_id,
        on_close=_on_close_wrapper,
        on_viewer_change=on_viewer_change)
    _tasker_window.show()
    _tasker_window.raise_()
    _tasker_window.activateWindow()

    if blocking and QApplication.instance() is app:
        app.exec()

    return _tasker_window


def launch_gui():
    """Public launcher entrypoint for --gui flag."""
    base = os.path.dirname(os.path.abspath(__file__))
    dm = LVSDataManager(base)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    # Try to import and build adapters
    try:
        from lvs_main import build_adapters
        active_adapter, _ = build_adapters(base)
    except Exception:
        active_adapter = None
        
    open_tasker(dm, active_adapter=active_adapter, blocking=True)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone smoke-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    dm = LVSDataManager(base)
    app = QApplication.instance() or QApplication(sys.argv)
    win = open_tasker(dm)
    sys.exit(app.exec())

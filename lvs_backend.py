#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  Backend (viewer-agnostic core)
#  Internal codename:  Aesthetic-Darwinism
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  Copyright (C) 2026 ArcticWinter
# -----------------------------------------------------------------------------
#
#  This module is the INVARIANT backend.  Anything that does not depend on
#  a specific image viewer (FastStone / digiKam / Apollo / etc.) lives here.
#
#  Public surface:
#     * Config / version / licence constants
#     * LVSDataManager        — SQLite + filesystem layer (no Qt, no Tk)
#     * AHKManager            — child-subprocess supervisor for AutoHotkey
#     * AHKPipeListener       — Windows-pipe IPC reader (viewer-agnostic)
#     * ViewerAdapter         — abstract base class for image-viewer adapters
#     * launch_gate()         — pre-launch readiness check used by lvs_main
#     * raws_copy_back()      — bulk copy RAWs back into ./raws from any source
#     * DelphiTPF0Settings    — FastStone FSSettings.db binary patcher
#     * patch_fsdb_cli()      — CLI entrypoint  `python -m lvs_backend --patch-fsdb`
#
#  Anything Qt or Tk lives in lvs_overlay_gui.py / lvs_tasker_gui.py.
#  Anything FastStone-specific (window title regex, hotkey adapter, etc.)
#  lives in lvs_faststone.py.
# -----------------------------------------------------------------------------

from __future__ import annotations

import sys
import os
import json
import sqlite3
import time
import threading
import signal
import re
import atexit
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple, Callable, Iterable

# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------
__version__       = "1.0.8"
__license__       = "AGPL-3.0-or-later"
__author__        = "ArcticWinter"
__codename__      = "Aesthetic-Darwinism"
__product_name__  = "LVS Selection Assist"


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
SELECT_PREFIX = "select"
SELECT_COUNT  = 5
SELECT_NAMES  = [f"{SELECT_PREFIX}{i}" for i in range(1, SELECT_COUNT + 1)]

# Canonical post-cull keepers pool (written by LVS Tasker, never by us).
SELECT_POOL_NAME = "select"
# Downstream editing pipeline output.
EDITS_OUTPUT_REL = os.path.join("edits", "output")
# Common RAW extensions for auto-detection.
RAW_EXTS = {
    ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".rw2", ".raf", ".dng",
    ".orf", ".pef", ".srw", ".raw", ".x3f", ".3fr", ".iiq",
}
# Picture (preview) extensions used by the launch gate.
PICTURE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

# AHK ↔ Python IPC pipe (Windows named pipe).
AHK_PIPE_NAME = r"\\.\pipe\LVS_AHK_IPC"


# -----------------------------------------------------------------------------
# Filename parsing helpers
# -----------------------------------------------------------------------------
_PREVIEW_HASH_RE = re.compile(r'_([0-9a-fA-F]{16})\.[^.]+$')


def extract_preview_hash(filename: str) -> Optional[str]:
    """Return the 16-char hex hash from an LVS preview filename, or None."""
    m = _PREVIEW_HASH_RE.search(filename)
    return m.group(1).lower() if m else None


def strip_preview_hash(filename: str) -> str:
    """'DSC_1234_b9caae83bfa555e8.jpg' → 'DSC_1234.jpg' (hash-less)."""
    name, ext = os.path.splitext(filename)
    m = re.search(r'^(.*?)_[0-9a-fA-F]{16}$', name)
    if m:
        return m.group(1) + ext
    return filename


def get_base_filename(filename: str) -> str:
    """Strip both the hash suffix AND the extension. Fallback only."""
    name, _ = os.path.splitext(filename)
    m = re.search(r'^(.*?)_[0-9a-fA-F]{16}$', name)
    return m.group(1) if m else name


def file_sha256(filepath: str, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# OS helpers
# -----------------------------------------------------------------------------
def reveal_in_explorer(path: str) -> bool:
    """
    Open Windows Explorer with `path` highlighted (drag/drop-friendly).
    Falls back to a folder-open on macOS / Linux.
    """
    try:
        if not os.path.exists(path):
            return False
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
            return True
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception as e:
        print(f"[Reveal] {e}")
        return False


# -----------------------------------------------------------------------------
# AHK subprocess manager
# -----------------------------------------------------------------------------
class AHKManager:
    """
    Launch + supervise the AutoHotkey culling-hotkey script as a child
    subprocess so it dies cleanly when LVS exits.
    """
    AHK_SCRIPT_NAME = "lvs_setup_shortcuts.ahk"

    AHK_CANDIDATES = [
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey32.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey.exe",
        r"C:\Program Files (x86)\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files (x86)\AutoHotkey\AutoHotkey.exe",
        r"%LocalAppData%\Programs\AutoHotkey\v2\AutoHotkey64.exe",
        r"%LocalAppData%\Programs\AutoHotkey\AutoHotkey.exe",
    ]

    def __init__(self, base_path: str):
        self.base_path = base_path
        self.script_path = os.path.join(base_path, self.AHK_SCRIPT_NAME)
        self.proc: Optional[subprocess.Popen] = None
        self.ahk_exe: Optional[str] = self._locate_ahk()

    def _locate_ahk(self) -> Optional[str]:
        for cand in self.AHK_CANDIDATES:
            cand = os.path.expandvars(cand)
            if os.path.exists(cand):
                return cand
        if os.name == "nt":
            for exe in ("AutoHotkey64.exe", "AutoHotkey.exe"):
                try:
                    out = subprocess.check_output(
                        ["where", exe], stderr=subprocess.DEVNULL, text=True
                    ).strip().splitlines()
                    if out:
                        return out[0]
                except Exception:
                    pass
        return None

    def start(self) -> bool:
        if not self.ahk_exe:
            print("[AHK] AutoHotkey v2 not found — hotkeys 1-5 disabled.")
            return False
        if not os.path.exists(self.script_path):
            print(f"[AHK] Script not found: {self.script_path}")
            return False
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                [self.ahk_exe, self.script_path],
                cwd=self.base_path,
                creationflags=flags,
            )
            print(f"[AHK] Launched: PID={self.proc.pid}  ({self.ahk_exe})")
            atexit.register(self.stop)
            return True
        except Exception as e:
            print(f"[AHK] Failed to launch: {e}")
            return False

    def reload(self) -> bool:
        self.stop()
        time.sleep(0.25)
        return self.start()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            print("[AHK] Macro terminated cleanly.")
        except Exception as e:
            print(f"[AHK] Error stopping AHK: {e}")
        finally:
            self.proc = None


# -----------------------------------------------------------------------------
# AHK → Python named-pipe listener
# -----------------------------------------------------------------------------
class AHKPipeListener(threading.Thread):
    """
    Windows named-pipe server reading newline-terminated JSON messages from
    the AutoHotkey macro after each Ctrl/digit copy attempt.

    Forwards the parsed dict to `on_message`.  Designed to be viewer-agnostic
    (the FastStone adapter is the producer, but any adapter could be).
    """

    def __init__(self, on_message: Callable[[dict], None]):
        super().__init__(daemon=True)
        self.on_message = on_message
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        # Unblock ConnectNamedPipe by connecting once with a sentinel.
        try:
            if os.name == "nt":
                import win32file
                h = win32file.CreateFile(
                    AHK_PIPE_NAME, win32file.GENERIC_WRITE, 0, None,
                    win32file.OPEN_EXISTING, 0, None
                )
                win32file.CloseHandle(h)
        except Exception:
            pass

    def run(self) -> None:
        if os.name != "nt":
            return
        try:
            import win32pipe
            import win32file
            import pywintypes
        except ImportError:
            print("[Pipe] pywin32 missing — AHK copy verification disabled.")
            return

        print(f"[Pipe] Listening on {AHK_PIPE_NAME}")
        while not self._stop.is_set():
            try:
                pipe = win32pipe.CreateNamedPipe(
                    AHK_PIPE_NAME,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_MESSAGE
                    | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT,
                    1, 65536, 65536, 0, None,
                )
                win32pipe.ConnectNamedPipe(pipe, None)
                if self._stop.is_set():
                    win32file.CloseHandle(pipe)
                    break
                buf = b""
                while True:
                    try:
                        _hr, chunk = win32file.ReadFile(pipe, 65536)
                        buf += chunk
                        if len(chunk) < 65536:
                            break
                    except pywintypes.error:
                        break
                win32file.CloseHandle(pipe)

                for line in buf.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        self.on_message(msg)
                    except json.JSONDecodeError as e:
                        # Soft-recover: occasionally AHK wraps the message in
                        # one or two stray brace characters (literal `{{ }}`).
                        # Strip a matched outer pair before reporting failure.
                        if line.startswith("{{") and line.endswith("}}"):
                            try:
                                msg = json.loads(line[1:-1])
                                self.on_message(msg)
                                continue
                            except Exception:
                                pass
                        print(f"[Pipe] Bad JSON: {line!r} ({e})")
            except Exception as e:
                if not self._stop.is_set():
                    # ERROR_PIPE_BUSY / ERROR_ACCESS_DENIED: another LVS already
                    # owns the pipe (double-run).  Don't spin a tight error loop.
                    msg = str(e)
                    if "231" in msg or "busy" in msg.lower() or "Access is denied" in msg:
                        print("[Pipe] The IPC pipe is already owned by another "
                              "LVS instance — copy verification disabled here. "
                              "(Close the duplicate; this instance keeps running.)")
                        return
                    print(f"[Pipe] Error: {e}")
                    time.sleep(0.5)


# -----------------------------------------------------------------------------
# Viewer Adapter — abstract base
# -----------------------------------------------------------------------------
class ViewerAdapter:
    """
    Pluggable image-viewer adapter.  A concrete subclass provides:

        id              — short machine id, e.g. "faststone"
        display_name    — human label, e.g. "FastStone Image Viewer"
        is_available()  — runtime check (process running OR executable found)
        start_watcher() — return a daemon thread that emits filename events
                          via a viewer-agnostic signal/callback bus
        configure()     — perform any first-launch setup (registry / settings)
        kill_macro()    — opportunity to terminate any helper process
        supports_hotkeys() → bool

    The overlay & tasker depend ONLY on this interface, not on FastStone.
    Future adapters: digiKam (Linux), Apollo (macOS), etc.
    """
    id: str = "abstract"
    display_name: str = "Abstract Viewer"

    def is_available(self) -> bool:
        return False

    def is_running(self) -> bool:
        return False

    def start_watcher(self, on_image, on_hide) -> "threading.Thread":
        raise NotImplementedError

    def stop_watcher(self) -> None:
        pass

    def supports_hotkeys(self) -> bool:
        return False

    def configure(self, base_path: str, select_paths: List[str]) -> Dict[str, Any]:
        """Return a dict like {'registry': True, 'binary': True, 'errors': []}."""
        return {"registry": False, "binary": False, "errors": ["not implemented"]}


# -----------------------------------------------------------------------------
# Data Manager — DB + filesystem + raws index
# -----------------------------------------------------------------------------
class LVSDataManager:
    """SQLite read-only + workspace filesystem inspector.  No UI deps."""

    def __init__(self, base_path: str):
        self.base_path      = os.path.abspath(base_path)
        self.db_path        = os.path.join(self.base_path, "ingest.db")
        self.select_folders = [os.path.join(self.base_path, n) for n in SELECT_NAMES]
        self.select_pool    = os.path.join(self.base_path, SELECT_POOL_NAME)
        self.edits_output   = os.path.join(self.base_path, EDITS_OUTPUT_REL)
        self.previews_dir   = os.path.join(self.base_path, "previews")
        self.default_raws   = os.path.join(self.base_path, "raws")

        # In-memory ONLY — never persisted.
        self._raws_root_override: Optional[str] = None
        self._raws_index_lock = threading.Lock()
        self._raws_index: Optional[Dict[str, List[str]]] = None
        self._raws_index_root: Optional[str] = None

    def ensure_edit_db_exists(self) -> None:
        """
        Dynamically rebuild/create edit.db from the live select1..5/edit1..5 folders
        if it is currently absent in the workspace.
        """
        edit_db = os.path.join(self.base_path, "edit.db")
        if os.path.exists(edit_db):
            return

        # Check if there are culling folders on disk with photos
        items = []
        for r in range(1, 6):
            # Check both selectN and editN prefixes
            for prefix in (SELECT_PREFIX, "edit"):
                folder = os.path.join(self.base_path, f"{prefix}{r}")
                if not os.path.isdir(folder):
                    continue
                try:
                    for f in os.listdir(folder):
                        full_path = os.path.join(folder, f)
                        if os.path.isfile(full_path) and os.path.splitext(f)[1].lower() in PICTURE_EXTS:
                            stem = get_base_filename(f)
                            items.append({
                                "stem": stem,
                                "rating": r
                            })
                except Exception:
                    pass

        if not items:
            return

        print(f"[edit.db] Creating absent edit.db from {len(items)} live edits...")
        try:
            conn = sqlite3.connect(edit_db)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS edits (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    clean_name        TEXT NOT NULL,
                    rating            INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                    score_overall     REAL,
                    score_quality     REAL,
                    score_composition REAL,
                    score_lighting    REAL,
                    score_color       REAL,
                    score_dof         REAL,
                    score_content     REAL,
                    caption           TEXT,
                    edit_status       TEXT DEFAULT 'pending',
                    edit_notes        TEXT,
                    edit_started_at   TEXT,
                    edit_completed_at TEXT,
                    output_path       TEXT,
                    created_at        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edits_rating ON edits(rating);
                CREATE INDEX IF NOT EXISTS idx_edits_status ON edits(edit_status);
            """)
            conn.commit()

            # Query metadata from ingest.db if present
            meta_cache = {}
            if os.path.exists(self.db_path):
                with self.get_connection() as iconn:
                    for item in items:
                        stem = item["stem"]
                        row = iconn.execute("""
                            SELECT p.score_overall, p.score_quality, p.score_composition,
                                   p.score_lighting, p.score_color, p.score_dof,
                                   p.score_content, p.caption
                            FROM previews p
                            JOIN files f ON p.file_id = f.file_id
                            WHERE f.file_name LIKE ? COLLATE NOCASE
                            LIMIT 1
                        """, (f"{stem}.%",)).fetchone()
                        if row:
                            meta_cache[stem.upper()] = {
                                "score_overall": row[0],
                                "score_quality": row[1],
                                "score_composition": row[2],
                                "score_lighting": row[3],
                                "score_color": row[4],
                                "score_dof": row[5],
                                "score_content": row[6],
                                "caption": row[7],
                            }

            import time
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            for item in items:
                stem = item["stem"]
                rating = item["rating"]
                meta = meta_cache.get(stem.upper(), {})
                conn.execute("""
                    INSERT INTO edits (
                        clean_name, rating,
                        score_overall, score_quality, score_composition,
                        score_lighting, score_color, score_dof, score_content,
                        caption, edit_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    stem, rating,
                    meta.get("score_overall"),
                    meta.get("score_quality"),
                    meta.get("score_composition"),
                    meta.get("score_lighting"),
                    meta.get("score_color"),
                    meta.get("score_dof"),
                    meta.get("score_content"),
                    meta.get("caption"),
                    "pending",
                    now
                ))
            conn.commit()
            conn.close()
            print(f"[edit.db] Successfully auto-created and populated edit.db with {len(items)} rows.")
        except Exception as e:
            print(f"[edit.db] Error auto-creating edit.db: {e}")

    # ------------------------------------------------------------ workspace
    def folders_exist(self) -> bool:
        return all(os.path.isdir(f) for f in self.select_folders)

    def get_connection(self):
        return sqlite3.connect(Path(self.db_path).as_uri() + "?mode=ro", uri=True)

    # ----------------------------------------------------- preview lookups
    def get_image_data(self, filename: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.db_path):
            return None
        try:
            with self.get_connection() as conn:
                conn.row_factory = sqlite3.Row
                preview_hash = extract_preview_hash(filename)
                if preview_hash:
                    row = conn.execute("""
                        SELECT f.file_id, f.burst_id, f.file_name, f.file_ext,
                               f.source_hash, f.capture_time, f.iso, f.aperture,
                               f.shutter_speed, f.focal_length_mm,
                               p.*
                        FROM files f
                        JOIN previews p ON f.file_id = p.file_id
                        WHERE LOWER(SUBSTR(f.source_hash, 1, 16)) = ?
                        LIMIT 1
                    """, (preview_hash,)).fetchone()
                    if row:
                        return dict(row)
                base_name = get_base_filename(filename)
                row = conn.execute("""
                    SELECT f.file_id, f.burst_id, f.file_name, f.file_ext,
                           f.source_hash, f.capture_time, f.iso, f.aperture,
                           f.shutter_speed, f.focal_length_mm,
                           p.*
                    FROM files f
                    JOIN previews p ON f.file_id = p.file_id
                    WHERE (f.file_name LIKE ? OR f.file_name = ?) COLLATE NOCASE
                    LIMIT 1
                """, (f"{base_name}.%", base_name)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"[DB] {e}")
            return None

    def get_batch_rank(self, burst_id: Optional[int],
                       current_overall: float) -> Dict[str, Any]:
        if burst_id is None:
            return {"rank": "mid", "is_best": False, "position": "N/A"}
        try:
            with self.get_connection() as conn:
                scores = sorted([r[0] for r in conn.execute("""
                    SELECT p.score_overall FROM files f
                    JOIN previews p ON f.file_id = p.file_id
                    WHERE f.burst_id = ? AND p.score_overall > 0
                """, (burst_id,)).fetchall()])
                if not scores:
                    return {"rank": "mid", "is_best": False, "position": "N/A"}
                pos = 1
                for idx, score in enumerate(scores):
                    if abs(score - current_overall) < 1e-5:
                        pos = idx + 1
                        break
                total = len(scores)
                pct = pos / total
                rank = ("lowest" if pct <= 0.25 else
                        "low"    if pct <= 0.50 else
                        "mid"    if pct <= 0.75 else
                        "high")
                if pos == total and total > 1:
                    rank = "best"
                is_best = (pos == total) if total > 1 else False
                return {"rank": rank, "is_best": is_best, "position": f"{pos}/{total}"}
        except Exception as e:
            print(f"[DB] {e}")
            return {"rank": "mid", "is_best": False, "position": "N/A"}

    def get_folder_ratings(self, filename: str) -> List[int]:
        fname_lower    = filename.lower()
        stripped_lower = strip_preview_hash(filename).lower()
        preview_hash   = extract_preview_hash(filename)
        base_name      = get_base_filename(filename).lower()

        found_in: List[int] = []
        # 1. Check live folders on disk first
        for i, folder in enumerate(self.select_folders, 1):
            if not os.path.isdir(folder):
                continue
            try:
                for f in os.listdir(folder):
                    fl = f.lower()
                    if fl == fname_lower or fl == stripped_lower:
                        found_in.append(i); break
                    if preview_hash and extract_preview_hash(f) == preview_hash:
                        found_in.append(i); break
                    if get_base_filename(f).lower() == base_name:
                        found_in.append(i); break
            except Exception as e:
                print(f"[Warn] scan {folder}: {e}")

        if found_in:
            return found_in

        # 2. Fallback to ingest.db tasker_ratings table (if it exists)
        if os.path.exists(self.db_path):
            try:
                with self.get_connection() as conn:
                    row = None
                    if preview_hash:
                        row = conn.execute("""
                            SELECT user_rating FROM tasker_ratings
                            WHERE LOWER(select_hash_prefix) = ?
                            LIMIT 1
                        """, (preview_hash.lower(),)).fetchone()
                    if not row:
                        row = conn.execute("""
                            SELECT user_rating FROM tasker_ratings
                            WHERE LOWER(select_stem) = ?
                            LIMIT 1
                        """, (base_name,)).fetchone()
                    if row:
                        return [row[0]]
            except Exception:
                pass

        # 3. Fallback to edit.db edits table
        edit_db = os.path.join(self.base_path, "edit.db")
        if os.path.exists(edit_db):
            try:
                with sqlite3.connect(edit_db) as conn:
                    row = conn.execute("""
                        SELECT rating FROM edits
                        WHERE LOWER(clean_name) = ?
                        LIMIT 1
                    """, (base_name,)).fetchone()
                    if row:
                        return [row[0]]
            except Exception:
                pass

        return found_in

    def get_select_folder_counts(self) -> List[int]:
        return [self.get_select_folder_count(i) for i in range(1, SELECT_COUNT + 1)]

    def get_select_folder_count(self, idx: int) -> int:
        folder = self.select_folders[idx - 1]
        if not os.path.isdir(folder):
            return 0
        try:
            return sum(1 for f in os.listdir(folder)
                       if os.path.isfile(os.path.join(folder, f)))
        except Exception:
            return 0

    def get_select_picture_counts(self) -> List[int]:
        """Like get_select_folder_counts, but only PICTURE files (jpg/png/...)."""
        out = []
        for folder in self.select_folders:
            if not os.path.isdir(folder):
                out.append(0); continue
            try:
                n = 0
                for f in os.listdir(folder):
                    full = os.path.join(folder, f)
                    if (os.path.isfile(full)
                            and os.path.splitext(f)[1].lower() in PICTURE_EXTS):
                        n += 1
                out.append(n)
            except Exception:
                out.append(0)
        return out

    def get_file_count(self) -> int:
        try:
            with self.get_connection() as conn:
                return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except Exception:
            return 0

    # -------------------------------------------------------------- raws
    def _scan_has_raws(self, root: str) -> bool:
        if not os.path.isdir(root):
            return False
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in RAW_EXTS:
                    return True
        return False

    def get_active_raws_root(self) -> Optional[str]:
        if self._raws_root_override and os.path.isdir(self._raws_root_override):
            return self._raws_root_override
        if self._scan_has_raws(self.default_raws):
            return self.default_raws
        return None

    def set_raws_root_override(self, path: Optional[str]) -> None:
        with self._raws_index_lock:
            self._raws_root_override = path if path else None
            self._raws_index = None
            self._raws_index_root = None
        print(f"[RAW] Session raws root: {path or '(cleared)'}")

    def needs_raws_prompt(self) -> bool:
        return self.get_active_raws_root() is None

    def _build_raws_index(self, root: str) -> Dict[str, List[str]]:
        """
        Walk `root` recursively, returning {filename.lower(): [full_path, ...]}.

        v1.0.4 fix: previous version was {name: single_path}, which lost
        duplicates when Nikon (and Canon/Sony) cameras rolled past 9999
        and started a new sub-folder beginning at 0001 — every name in
        the second sub-folder silently overwrote the first.  On a real
        12,229-file shoot this manifested as the printed "9,999" cap
        the user observed.  Storing a LIST per name preserves all hits;
        resolution prefers the first match by default and exposes the
        full list for downstream disambiguation.
        """
        idx: Dict[str, List[str]] = {}
        total = 0
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in RAW_EXTS:
                    idx.setdefault(f.lower(), []).append(os.path.join(dirpath, f))
                    total += 1
        # `total` counts files; `len(idx)` counts unique names — the gap
        # is the rollover-collision count.
        return idx

    def _index_total_files(self, idx: Dict[str, List[str]]) -> int:
        return sum(len(v) for v in idx.values())

    def find_raw_file(self, raw_filename: str,
                      expected_hash: Optional[str] = None) -> Optional[str]:
        """
        Resolve `raw_filename` to one full path on disk.

        Disambiguation order when duplicates exist (DSC_0001.NEF in 184NIKON/
        AND 185NIKON/):
          1. `expected_hash` (full SHA-256 of the RAW from ingest.db) — the
             ONLY trustworthy discriminator across rollover duplicates.
          2. mtime closeness to the DB capture_time (heuristic).
          3. first hit (last resort).

        Callers that have the originating preview filename should pass the
        expected hash so the wrong duplicate is never opened.
        """
        if not raw_filename:
            return None
        root = self.get_active_raws_root()
        if not root:
            return None
        with self._raws_index_lock:
            if self._raws_index is None or self._raws_index_root != root:
                print(f"[RAW] Indexing raws root: {root}")
                self._raws_index = self._build_raws_index(root)
                self._raws_index_root = root
                files = self._index_total_files(self._raws_index)
                uniq  = len(self._raws_index)
                dupes = files - uniq
                print(f"[RAW] Indexed {files:,} RAW files "
                      f"({uniq:,} unique names, {dupes:,} duplicate-name collisions).")
            hits = self._raws_index.get(raw_filename.lower(), [])
        hits = [h for h in hits if os.path.exists(h)]
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        # 1. Hash is authoritative.
        if expected_hash:
            for h in hits:
                if file_sha256(h) == expected_hash:
                    return h
            print(f"[RAW] {len(hits)} duplicate-named candidates for "
                  f"{raw_filename}; none match expected hash {expected_hash[:16]}… "
                  f"— falling back to capture-time heuristic.")
        # 2. Multi-hit disambiguation via DB capture_time
        capture_iso = self._lookup_capture_time(raw_filename)
        if capture_iso:
            try:
                import datetime
                target = datetime.datetime.fromisoformat(
                    capture_iso.replace("Z", "+00:00")).timestamp()
                hits.sort(key=lambda h: abs(os.path.getmtime(h) - target))
            except Exception:
                pass
        return hits[0]

    def get_source_hash_for_preview(self, preview_filename: str) -> Optional[str]:
        """Return the full RAW source_hash for a preview filename, or None."""
        if not os.path.exists(self.db_path):
            return None
        try:
            with self.get_connection() as conn:
                ph = extract_preview_hash(preview_filename)
                if ph:
                    row = conn.execute(
                        "SELECT source_hash FROM files "
                        "WHERE LOWER(SUBSTR(source_hash,1,16))=? LIMIT 1",
                        (ph,)).fetchone()
                    if row and row[0]:
                        return row[0]
                base = get_base_filename(preview_filename)
                row = conn.execute(
                    "SELECT source_hash FROM files "
                    "WHERE (file_name LIKE ? OR file_name=?) COLLATE NOCASE LIMIT 1",
                    (f"{base}.%", base)).fetchone()
                return row[0] if row and row[0] else None
        except Exception:
            return None

    def _lookup_capture_time(self, raw_filename: str) -> Optional[str]:
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT capture_time FROM files "
                    "WHERE file_name = ? COLLATE NOCASE LIMIT 1",
                    (raw_filename,)).fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def get_raw_filename(self, preview_filename: str) -> Optional[str]:
        try:
            with self.get_connection() as conn:
                conn.row_factory = sqlite3.Row
                ph = extract_preview_hash(preview_filename)
                if ph:
                    row = conn.execute(
                        "SELECT file_name FROM files "
                        "WHERE LOWER(SUBSTR(source_hash,1,16))=? LIMIT 1",
                        (ph,)).fetchone()
                    if row:
                        return row['file_name']
                base = get_base_filename(preview_filename)
                row = conn.execute(
                    "SELECT file_name FROM files "
                    "WHERE (file_name LIKE ? OR file_name=?) COLLATE NOCASE LIMIT 1",
                    (f"{base}.%", base)).fetchone()
                return row['file_name'] if row else None
        except Exception:
            return None

    # ---------------------------------------------- open-cascade helpers
    @staticmethod
    def find_in_folder(folder: str, candidates: List[str]) -> Optional[str]:
        if not os.path.isdir(folder):
            return None
        wanted = {c.lower() for c in candidates}
        try:
            for f in os.listdir(folder):
                if f.lower() in wanted:
                    return os.path.join(folder, f)
        except Exception:
            return None
        return None

    @staticmethod
    def find_in_folder_by_base(folder: str, base_no_ext: str,
                               exts: Tuple[str, ...] = (".jpg", ".jpeg")
                               ) -> Optional[str]:
        if not os.path.isdir(folder):
            return None
        base_lower = base_no_ext.lower()
        try:
            for f in os.listdir(folder):
                name, ext = os.path.splitext(f)
                if name.lower() == base_lower and ext.lower() in exts:
                    return os.path.join(folder, f)
        except Exception:
            return None
        return None

    @staticmethod
    def find_in_folder_by_stem(folder: str, clean_stem: str,
                               exts: Optional[Tuple[str, ...]] = None
                               ) -> Optional[str]:
        """
        Match a *clean* camera stem (e.g. 'DSC03806') against files in `folder`
        whose names carry an LVS hash suffix (e.g. 'DSC03806_4b2f758b29004ffe.jpg').

        This is the FAST, no-DB cascade matcher: a file is copied into selectN/
        with its ingest hash appended, so the on-disk name will NEVER equal the
        clean stem.  We strip the hash from each candidate and compare stems.

        NOTE on collisions: two different frames can theoretically share the same
        clean stem (10k-name SD-card rollover). We can't disambiguate without a
        DB roundtrip (deliberately avoided for speed), so we return the first
        stem match.  exts=None means "any picture extension".
        """
        if not os.path.isdir(folder):
            return None
        target = clean_stem.lower()
        ok_exts = set(e.lower() for e in exts) if exts else PICTURE_EXTS
        try:
            for f in os.listdir(folder):
                ext = os.path.splitext(f)[1].lower()
                if ext not in ok_exts:
                    continue
                # strip the _<16hex> ingest suffix, then compare clean stems
                cand_stem = get_base_filename(f).lower()
                if cand_stem == target:
                    return os.path.join(folder, f)
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # DB-driven preview path resolution  (v1.0.4)
    # ------------------------------------------------------------------
    # The previews table stores `preview_path` — the canonical on-disk
    # location of every extracted JPEG.  Resolving via the DB lets the
    # workspace's previews/ folder live anywhere on disk, not just next
    # to ingest.db.  When the DB path is missing/invalid, we fall back
    # to <workspace>/previews/<filename>.
    def get_preview_path_from_db(self, preview_filename: str) -> Optional[str]:
        try:
            with self.get_connection() as conn:
                conn.row_factory = sqlite3.Row
                ph = extract_preview_hash(preview_filename)
                if ph:
                    row = conn.execute(
                        "SELECT p.preview_path FROM files f "
                        "JOIN previews p ON p.file_id = f.file_id "
                        "WHERE LOWER(SUBSTR(f.source_hash,1,16))=? LIMIT 1",
                        (ph,)).fetchone()
                    if row and row["preview_path"]:
                        return row["preview_path"]
                base = get_base_filename(preview_filename)
                row = conn.execute(
                    "SELECT p.preview_path FROM files f "
                    "JOIN previews p ON p.file_id = f.file_id "
                    "WHERE (f.file_name LIKE ? OR f.file_name=?) "
                    "COLLATE NOCASE LIMIT 1",
                    (f"{base}.%", base)).fetchone()
                return row["preview_path"] if row and row["preview_path"] else None
        except Exception:
            return None

    def resolve_preview_path(self, preview_filename: str) -> Optional[str]:
        """
        v1.0.4: resolution order for a preview JPEG —
            1. DB-stored absolute path (preview_path in `previews` table)
            2. <workspace>/previews/<filename>
            3. <workspace>/<filename>
        Returns the first existing path, or None.
        """
        db_path = self.get_preview_path_from_db(preview_filename)
        if db_path and os.path.exists(db_path):
            return db_path
        local = os.path.join(self.previews_dir, preview_filename)
        if os.path.exists(local):
            return local
        loose = os.path.join(self.base_path, preview_filename)
        if os.path.exists(loose):
            return loose
        return None

    def get_previews_dir_from_db(self) -> Optional[str]:
        """
        Returns the directory containing previews as recorded in the DB
        (taking the parent of any preview_path row).  Used by the tasker
        Paths panel "Auto-detect from Workspace" feature.
        """
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT preview_path FROM previews "
                    "WHERE preview_path IS NOT NULL LIMIT 1").fetchone()
                if row and row[0]:
                    return os.path.dirname(row[0])
        except Exception:
            pass
        return None

    def get_distinct_session_count(self) -> int:
        try:
            with self.get_connection() as conn:
                return conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM files").fetchone()[0]
        except Exception:
            return 0


# -----------------------------------------------------------------------------
# Single-instance guard  (prevents the "All pipes are busy" double-run)
# -----------------------------------------------------------------------------
#
# Two LVS instances both try to CreateNamedPipe(LVS_AHK_IPC) with nMaxInstances=1.
# The second one fails with ERROR_PIPE_BUSY (231) -> "All pipes are busy".  We
# guard against that BEFORE any pipe work by holding a named mutex (Windows) or
# an exclusive lock file (POSIX).  If a live instance is detected, the launcher
# tells the user and offers to take over (kill the stale one) or abort.
# -----------------------------------------------------------------------------
_INSTANCE_MUTEX_NAME = "Global\\LVS_Selection_Assist_SingleInstance"
_INSTANCE_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".lvs_instance.lock")


class SingleInstance:
    """
    Acquire a process-wide single-instance lock.

    .acquired        -> True if WE now own the instance.
    .already_running  -> True if another live LVS already holds it.
    .pid             -> pid recorded in the lock (POSIX) if any.
    """

    def __init__(self):
        self.acquired = False
        self.already_running = False
        self.pid: Optional[int] = None
        self._handle = None
        self._fh = None
        self._acquire()

    # -- Windows: named mutex (authoritative, matches the pipe's namespace) ---
    def _acquire_windows(self) -> None:
        try:
            import win32event
            import win32api
            import winerror
        except ImportError:
            self._acquire_lockfile()
            return
        self._handle = win32event.CreateMutex(None, True, _INSTANCE_MUTEX_NAME)
        last = win32api.GetLastError()
        if last == winerror.ERROR_ALREADY_EXISTS:
            self.already_running = True
            self.acquired = False
        else:
            self.acquired = True

    # -- POSIX (and Windows fallback): exclusive lock file --------------------
    def _acquire_lockfile(self) -> None:
        try:
            if os.name == "nt":
                # Best-effort: if the file exists and is fresh, assume running.
                if os.path.exists(_INSTANCE_LOCK_PATH):
                    self.already_running = True
                    return
                self._fh = open(_INSTANCE_LOCK_PATH, "w")
                self._fh.write(str(os.getpid()))
                self._fh.flush()
                self.acquired = True
                return
            import fcntl
            self._fh = open(_INSTANCE_LOCK_PATH, "a+")
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh.seek(0); self._fh.truncate()
                self._fh.write(str(os.getpid())); self._fh.flush()
                self.acquired = True
            except OSError:
                self.already_running = True
                try:
                    self._fh.seek(0)
                    self.pid = int((self._fh.read() or "0").strip() or 0)
                except Exception:
                    self.pid = None
        except Exception:
            # If locking is impossible, don't block startup.
            self.acquired = True

    def _acquire(self) -> None:
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_lockfile()

    def takeover(self) -> bool:
        """
        Free a stale lock so THIS instance can run (the user chose "fix").
        On Windows the OS releases the mutex when the dead process exits; if a
        live instance is genuinely running we cannot steal its mutex, so we
        return False.  Mostly useful to clear a stale POSIX/Windows lock file.
        """
        try:
            if self._fh is None and os.path.exists(_INSTANCE_LOCK_PATH):
                os.remove(_INSTANCE_LOCK_PATH)
            self.already_running = False
            self._acquire()
            return self.acquired
        except Exception:
            return False

    def release(self) -> None:
        try:
            if self._handle is not None:
                import win32api
                win32api.CloseHandle(self._handle)
                self._handle = None
        except Exception:
            pass
        try:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            if os.name != "nt" or self.acquired:
                if os.path.exists(_INSTANCE_LOCK_PATH):
                    os.remove(_INSTANCE_LOCK_PATH)
        except Exception:
            pass


def free_stale_ipc_pipe() -> bool:
    """
    Connect-and-close the LVS named pipe once to unstick a half-open server
    (the cause of a lingering ERROR_PIPE_BUSY after an unclean exit).  No-op off
    Windows.  Returns True if a pipe handle was touched.
    """
    if os.name != "nt":
        return False
    try:
        import win32file
        h = win32file.CreateFile(
            AHK_PIPE_NAME, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None)
        win32file.CloseHandle(h)
        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Launch gate
# -----------------------------------------------------------------------------
def launch_gate(dm: LVSDataManager) -> Dict[str, Any]:
    """
    Pre-launch readiness check.  Returns a dict the launcher can act on:

        {
          "ready":          bool,   # True if at least one picture in any selectN
          "any_folder":     bool,   # True if any selectN directory exists
          "all_folders":    bool,   # True if every selectN exists
          "picture_total":  int,    # total picture files across all selectN
          "per_folder":     [int]*5,
          "needs_setup":    bool,   # True iff no folders exist AT ALL
          "missing":        [str],
          "present":        [str],
        }
    """
    present, missing = [], []
    for i, folder in enumerate(dm.select_folders, 1):
        name = SELECT_NAMES[i - 1]
        if os.path.isdir(folder):
            present.append(name)
        else:
            missing.append(name)

    per_folder = dm.get_select_picture_counts()
    picture_total = sum(per_folder)

    return {
        "ready":         picture_total > 0,
        "any_folder":    len(present) > 0,
        "all_folders":   len(missing) == 0,
        "picture_total": picture_total,
        "per_folder":    per_folder,
        "needs_setup":   len(present) == 0,    # NOTHING exists yet
        "missing":       missing,
        "present":       present,
    }


# -----------------------------------------------------------------------------
# RAW copy-back  (Tasker → backend service)
# -----------------------------------------------------------------------------
def raws_copy_back(
    dm: LVSDataManager,
    source_root: str,
    *,
    dry_run: bool = False,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    "All sorted" copy-back mode:

        For each PICTURE file present in ANY select1..select5 folder, look up
        its RAW filename in ingest.db, find that file under `source_root`
        (recursive), and copy it to ./raws/.  Skips files that already exist
        in ./raws/ with matching size.

    Args:
        dm            - LVSDataManager (provides DB + workspace)
        source_root   - external root containing the original RAWs.  Must
                        already exist; sub-trees (184NIKON/, 185NIKON/, …)
                        are walked.
        dry_run       - if True, do not perform copies, just report what
                        would happen.
        on_progress   - optional callback (filename, done, total) for GUI use.

    Returns a summary dict.  Never raises on per-file errors — captured in
    the returned `errors` list.
    """
    summary: Dict[str, Any] = {
        "source_root":      source_root,
        "destination":      dm.default_raws,
        "src_total_files":  0,     # all RAW files under source_root (with dupes)
        "src_unique_names": 0,     # distinct filenames
        "src_dupe_names":   0,     # duplicate filename collisions (rollover)
        "candidates":       0,     # picture files in selectN
        "resolved":         0,     # had DB record → got a raw filename
        "found":            0,     # raw filename was actually located on disk
        "copied":           0,
        "skipped":          0,     # already present in ./raws
        "missing":          0,     # RAW not found under source_root
        "errors":           [],    # list of {filename, reason}
        "dry_run":          dry_run,
    }

    if not os.path.isdir(source_root):
        summary["errors"].append({"filename": source_root,
                                  "reason": "source root does not exist"})
        return summary

    # Build a duplicate-aware RAW index for SOURCE_ROOT.
    # v1.0.4: previous {name: single_path} dict silently dropped Nikon/Canon
    # rollover duplicates (DSC_0001.NEF appearing in BOTH 184NIKON/ and
    # 185NIKON/).  The new {name: [paths...]} structure preserves all of them
    # and we count total files vs unique names for honest reporting.
    src_index: Dict[str, List[str]] = {}
    src_total = 0
    for dirpath, _dirs, files in os.walk(source_root):
        for f in files:
            if os.path.splitext(f)[1].lower() in RAW_EXTS:
                src_index.setdefault(f.lower(), []).append(os.path.join(dirpath, f))
                src_total += 1
    src_uniq  = len(src_index)
    src_dupes = src_total - src_uniq
    summary["src_total_files"]  = src_total
    summary["src_unique_names"] = src_uniq
    summary["src_dupe_names"]   = src_dupes
    print(f"[CopyBack] Indexed {src_total:,} RAW(s) under {source_root}  "
          f"({src_uniq:,} unique names, {src_dupes:,} duplicate-name collisions)")

    if not dry_run:
        os.makedirs(dm.default_raws, exist_ok=True)

    # Gather candidate previews across selectN
    candidates: List[str] = []
    for folder in dm.select_folders:
        if not os.path.isdir(folder):
            continue
        try:
            for f in os.listdir(folder):
                full = os.path.join(folder, f)
                if (os.path.isfile(full)
                        and os.path.splitext(f)[1].lower() in PICTURE_EXTS):
                    candidates.append(f)
        except Exception as e:
            summary["errors"].append({"filename": folder, "reason": str(e)})
    summary["candidates"] = len(candidates)

    # De-duplicate by base filename (a picture may live in multiple selectN
    # if a duplicate-cull edge case occurred).
    seen_bases = set()
    unique_picks: List[str] = []
    for f in candidates:
        key = strip_preview_hash(f).lower()
        if key in seen_bases:
            continue
        seen_bases.add(key)
        unique_picks.append(f)

    for i, fname in enumerate(unique_picks, 1):
        raw_name = dm.get_raw_filename(fname)
        if not raw_name:
            summary["errors"].append({"filename": fname,
                                      "reason": "no DB record"})
            if on_progress: on_progress(fname, i, len(unique_picks))
            continue
        summary["resolved"] += 1

        candidates_for_raw = src_index.get(raw_name.lower(), [])
        candidates_for_raw = [c for c in candidates_for_raw if os.path.exists(c)]
        if not candidates_for_raw:
            summary["missing"] += 1
            summary["errors"].append({"filename": raw_name,
                                      "reason": "not under source root"})
            if on_progress: on_progress(fname, i, len(unique_picks))
            continue

        # Look up source_hash from DB to do exact matching
        expected_hash = None
        ph = extract_preview_hash(fname)
        try:
            with dm.get_connection() as conn:
                row = None
                if ph:
                    row = conn.execute(
                        "SELECT source_hash FROM files WHERE LOWER(SUBSTR(source_hash,1,16)) = ? LIMIT 1",
                        (ph,)
                    ).fetchone()
                if not row:
                    base = get_base_filename(fname)
                    row = conn.execute(
                        "SELECT source_hash FROM files WHERE (file_name LIKE ? OR file_name = ?) COLLATE NOCASE LIMIT 1",
                        (f"{base}.%", base)
                    ).fetchone()
                if row:
                    expected_hash = row[0]
        except Exception:
            pass

        # ---- Pick the correct source RAW ----
        # The DB source_hash is the AUTHORITATIVE identity of the RAW.  With
        # Nikon/Canon counter rollover, the same filename (e.g. DSC_5000.NEF)
        # can exist in two sub-folders with DIFFERENT contents.  The hash —
        # not mtime, not folder order — decides which is correct.
        src_path = None
        if expected_hash:
            hash_hits = [c for c in candidates_for_raw
                         if file_sha256(c) == expected_hash]
            if hash_hits:
                src_path = hash_hits[0]
            elif len(candidates_for_raw) > 1:
                # Ambiguous: multiple same-named RAWs, NONE match the hash.
                # Previously this silently copied candidates_for_raw[0] (an
                # mtime guess) — i.e. the WRONG NEF.  Refuse to guess.
                summary["missing"] += 1
                summary["errors"].append({
                    "filename": raw_name,
                    "reason": (f"{len(candidates_for_raw)} same-named RAWs found "
                               f"but none match expected hash "
                               f"{expected_hash[:16]}… — skipped to avoid copying "
                               f"the wrong file"),
                })
                if on_progress:
                    on_progress(fname, i, len(unique_picks))
                continue
            else:
                # Single same-named candidate whose hash differs from the DB.
                # Most likely a re-export; copy it but record a note.
                src_path = candidates_for_raw[0]
                summary["errors"].append({
                    "filename": raw_name,
                    "reason": "hash mismatch (single candidate) — copied anyway",
                })

        # No hash available at all → mtime-vs-capture_time heuristic.
        if src_path is None:
            src_path = candidates_for_raw[0]
            if len(candidates_for_raw) > 1:
                cap = dm._lookup_capture_time(raw_name)
                if cap:
                    try:
                        import datetime as _dt
                        target = _dt.datetime.fromisoformat(
                            cap.replace("Z", "+00:00")).timestamp()
                        candidates_for_raw.sort(
                            key=lambda h: abs(os.path.getmtime(h) - target))
                        src_path = candidates_for_raw[0]
                    except Exception:
                        pass
        summary["found"] += 1

        # Deal with duplicate names at destination: append '0' if hash differs
        dst_path = os.path.join(dm.default_raws, raw_name)
        if expected_hash:
            stem, ext = os.path.splitext(raw_name)
            while os.path.exists(dst_path):
                # If the existing file matches the hash, it's already copied!
                if file_sha256(dst_path) == expected_hash:
                    break
                # Different file with same name: append a 0
                stem += "0"
                dst_path = os.path.join(dm.default_raws, f"{stem}{ext}")

        if os.path.exists(dst_path):
            try:
                if file_sha256(dst_path) == expected_hash or os.path.getsize(dst_path) == os.path.getsize(src_path):
                    summary["skipped"] += 1
                    if on_progress: on_progress(fname, i, len(unique_picks))
                    continue
            except OSError:
                pass

        if dry_run:
            summary["copied"] += 1
            if on_progress: on_progress(fname, i, len(unique_picks))
            continue
        try:
            shutil.copy2(src_path, dst_path)
            summary["copied"] += 1
        except Exception as e:
            summary["errors"].append({"filename": os.path.basename(dst_path), "reason": str(e)})
        if on_progress:
            on_progress(fname, i, len(unique_picks))

    # Invalidate dm's raws index so the overlay picks up the newly-copied files.
    dm.set_raws_root_override(None)
    return summary


# -----------------------------------------------------------------------------
# FASTSTONE FSSettings.db  (Delphi TPF0)  PATCHER
# -----------------------------------------------------------------------------
#
# FastStone stores "Copy/Move to Folder" 1..9 slot paths inside a proprietary
# Delphi-serialized binary at:
#     %LocalAppData%\FastStone\FSIV\FSSettings.db
#
# These are NOT the same as registry "Favorite Folders" used by Ctrl+1..5.
# v1.0.2 patches BOTH so all FastStone shortcuts converge on select1..select5.
#
# The TPF0 byte-level layout is documented in the LVS Tasker spec.  This
# implementation is byte-for-byte verified roundtrip.
# -----------------------------------------------------------------------------
FSDB_RELATIVE_PARTS = ("FastStone", "FSIV", "FSSettings.db")
FSDB_TARGET_KEY = "CopyMove19StringText"


class DelphiTPF0Settings:
    """Parser + serializer for FastStone FSSettings.db (Delphi TPF0 stream)."""
    vaList=1; vaInt8=2; vaInt16=3; vaInt32=4; vaExtended=5
    vaString=6; vaIdent=7; vaFalse=8; vaTrue=9; vaBinary=10
    vaSet=11; vaLString=12; vaWString=13; vaInt64=14; vaUTF8String=15
    vaCollection=16

    def __init__(self):
        self.class_name = b"TProgramSettings"
        self.obj_name = b""
        self.properties: List[Tuple[bytes, int, Any]] = []

    def load(self, filepath: str) -> None:
        with open(filepath, "rb") as f:
            data = f.read()
        if data[:4] != b"TPF0":
            raise ValueError("Not a valid Delphi TPF0 binary stream.")
        offset = 4

        def read_bytes(n):
            nonlocal offset
            res = data[offset:offset+n]; offset += n
            return res

        def parse_val(vt):
            nonlocal offset
            if vt == self.vaList:
                res = []
                while True:
                    t = data[offset]; offset += 1
                    if t == 0: break
                    res.append((t, parse_val(t)))
                return res
            if vt == self.vaInt8:     return struct.unpack("<b", read_bytes(1))[0]
            if vt == self.vaInt16:    return struct.unpack("<h", read_bytes(2))[0]
            if vt == self.vaInt32:    return struct.unpack("<i", read_bytes(4))[0]
            if vt == self.vaInt64:    return struct.unpack("<q", read_bytes(8))[0]
            if vt == self.vaExtended: return read_bytes(10)
            if vt in (self.vaString, self.vaIdent):
                slen = data[offset]; offset += 1
                return read_bytes(slen)
            if vt in (self.vaFalse, self.vaTrue): return None
            if vt == self.vaBinary:
                blen = struct.unpack("<i", read_bytes(4))[0]
                return read_bytes(blen)
            if vt == self.vaSet:
                res = []
                while True:
                    slen = data[offset]; offset += 1
                    if slen == 0: break
                    res.append(read_bytes(slen))
                return res
            if vt in (self.vaLString, self.vaUTF8String):
                slen = struct.unpack("<i", read_bytes(4))[0]
                return read_bytes(slen)
            if vt == self.vaWString:
                cl = struct.unpack("<i", read_bytes(4))[0]
                return read_bytes(cl * 2)
            if vt == self.vaCollection:
                res = []
                while True:
                    marker = data[offset]; offset += 1
                    if marker == 0: break
                    item_props = []
                    while True:
                        nl = data[offset]; offset += 1
                        if nl == 0: break
                        pname = read_bytes(nl)
                        ptype = data[offset]; offset += 1
                        item_props.append((pname, ptype, parse_val(ptype)))
                    res.append(item_props)
                return res
            raise ValueError(f"Unknown Delphi ValType {vt} at offset {offset-1}")

        class_len = data[offset]; offset += 1
        self.class_name = read_bytes(class_len)
        obj_len = data[offset]; offset += 1
        self.obj_name = read_bytes(obj_len)
        self.properties = []
        while offset < len(data):
            nl = data[offset]; offset += 1
            if nl == 0: break
            pname = read_bytes(nl)
            ptype = data[offset]; offset += 1
            self.properties.append((pname, ptype, parse_val(ptype)))

    def save(self, filepath: str) -> None:
        out = bytearray()
        out.extend(b"TPF0")
        out.append(len(self.class_name)); out.extend(self.class_name)
        out.append(len(self.obj_name));   out.extend(self.obj_name)

        def write_val(vt, val):
            if vt == self.vaList:
                for t, v in val:
                    out.append(t); write_val(t, v)
                out.append(0)
            elif vt == self.vaInt8:     out.extend(struct.pack("<b", val))
            elif vt == self.vaInt16:    out.extend(struct.pack("<h", val))
            elif vt == self.vaInt32:    out.extend(struct.pack("<i", val))
            elif vt == self.vaInt64:    out.extend(struct.pack("<q", val))
            elif vt == self.vaExtended: out.extend(val)
            elif vt in (self.vaString, self.vaIdent):
                out.append(len(val)); out.extend(val)
            elif vt in (self.vaFalse, self.vaTrue): pass
            elif vt == self.vaBinary:
                out.extend(struct.pack("<i", len(val))); out.extend(val)
            elif vt == self.vaSet:
                for item in val:
                    out.append(len(item)); out.extend(item)
                out.append(0)
            elif vt in (self.vaLString, self.vaUTF8String):
                out.extend(struct.pack("<i", len(val))); out.extend(val)
            elif vt == self.vaWString:
                cl = len(val) // 2
                out.extend(struct.pack("<i", cl)); out.extend(val)
            elif vt == self.vaCollection:
                for item_props in val:
                    out.append(1)
                    for pname, ptype, pval in item_props:
                        out.append(len(pname)); out.extend(pname)
                        out.append(ptype); write_val(ptype, pval)
                    out.append(0)
                out.append(0)
            else:
                raise ValueError(f"Cannot serialize ValType {vt}")

        for pname, ptype, pval in self.properties:
            out.append(len(pname)); out.extend(pname)
            out.append(ptype); write_val(ptype, pval)
        out.append(0); out.append(0)
        with open(filepath, "wb") as f:
            f.write(bytes(out))

    def get_property(self, name_str: str):
        nb = name_str.encode("ascii").lower()
        for i, (pn, pt, pv) in enumerate(self.properties):
            if pn.lower() == nb:
                return i, pt, pv
        return None

    def set_property_string(self, name_str: str, new_str: str) -> None:
        found = self.get_property(name_str)
        try:
            new_bytes = new_str.encode("cp1252")
        except UnicodeEncodeError:
            new_bytes = new_str.encode("utf-8", errors="replace")
        if found is None:
            self.properties.append(
                (name_str.encode("ascii"), self.vaLString, new_bytes))
            return
        idx, ptype, _ = found
        if ptype == self.vaString and len(new_bytes) > 255:
            ptype = self.vaLString
        self.properties[idx] = (self.properties[idx][0], ptype, new_bytes)


def patch_fsdb_cli() -> int:
    """
    CLI entrypoint invoked by the AHK installer:
        python lvs_backend.py --patch-fsdb
    """
    base_path = os.path.dirname(os.path.abspath(__file__))
    select_paths = [os.path.join(base_path, n) for n in SELECT_NAMES]

    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        print("LOCALAPPDATA not set — cannot locate FSSettings.db")
        return 1
    fsdb = os.path.join(localapp, *FSDB_RELATIVE_PARTS)
    fsdb_dir = os.path.dirname(fsdb)

    slot_lines = list(select_paths) + ["", "", "", ""]
    payload = "\r\n".join(slot_lines) + "\r\n"

    settings = DelphiTPF0Settings()
    if os.path.exists(fsdb):
        try:
            settings.load(fsdb)
        except Exception as e:
            print(f"Failed to parse existing FSSettings.db: {e}")
            return 1
        try:
            shutil.copy2(fsdb, fsdb + ".lvs.bak")
        except Exception as e:
            print(f"Could not back up FSSettings.db: {e}")
            return 1
    else:
        try:
            os.makedirs(fsdb_dir, exist_ok=True)
        except Exception as e:
            print(f"Could not create {fsdb_dir}: {e}")
            return 1

    try:
        settings.set_property_string(FSDB_TARGET_KEY, payload)
        settings.save(fsdb)
    except PermissionError:
        print("FSSettings.db is locked — close FastStone and try again.")
        return 1
    except Exception as e:
        print(f"Failed to write FSSettings.db: {e}")
        return 1

    print(f"FSDB_OK  wrote {len(select_paths)} slots to {fsdb}")
    return 0


# -----------------------------------------------------------------------------
# Unified settings persistence (select_settings.json)
# -----------------------------------------------------------------------------
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "select_settings.json")


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_hud_pos() -> Tuple[Optional[int], Optional[int]]:
    """Restore overlay position from select_settings.json."""
    d = _load_settings().get("hud", {})
    return d.get("x"), d.get("y")


def save_hud_pos(x: int, y: int) -> None:
    """Persist overlay position into select_settings.json."""
    s = _load_settings()
    s["hud"] = {"x": x, "y": y}
    _save_settings(s)


def load_tasker_paths() -> Dict[str, str]:
    """Restore persistent tasker paths (workspace, previews, database).
    RAWs is deliberately NOT persisted — it is per-session removable media."""
    return _load_settings().get("paths", {})


def save_tasker_paths(paths: Dict[str, str]) -> None:
    """Persist tasker paths into select_settings.json."""
    s = _load_settings()
    s["paths"] = {k: v for k, v in paths.items() if k != "raws"}
    _save_settings(s)


# -----------------------------------------------------------------------------
# Sample database for first-run / demo
# -----------------------------------------------------------------------------
def ensure_sample_database(db_path: str) -> None:
    """
    DEPRECATED / DISABLED.

    LVS is a strict READER of ingest.db (it is produced upstream by the ingest
    pipeline).  Auto-creating a sample/demo database on first launch polluted
    real workspaces and is now disabled.  This function is retained only so any
    external caller importing the symbol does not break; it intentionally does
    NOTHING and never writes a file.

    Set LVS_ALLOW_SAMPLE_DB=1 if you explicitly want the old demo DB for testing.
    """
    if os.environ.get("LVS_ALLOW_SAMPLE_DB") != "1":
        return
    if os.path.exists(db_path):
        return
    print(f"\n[LVS DB] (LVS_ALLOW_SAMPLE_DB=1) Creating sample database at {db_path}...")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file_id INTEGER PRIMARY KEY,
            file_name TEXT, file_ext TEXT,
            burst_id INTEGER,
            source_hash TEXT,
            capture_time TEXT, shutter_speed TEXT,
            iso INTEGER, aperture REAL, focal_length_mm REAL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS previews (
            file_id INTEGER PRIMARY KEY,
            score_overall REAL, score_lighting REAL, score_quality REAL,
            score_composition REAL, score_color REAL, score_dof REAL,
            score_content REAL, caption TEXT
        )""")
        files = [
            (1, "wedding_01.NEF",   ".NEF", 100, "b2786c7afe34f891"+"0"*48, "2026-01-15T14:23:11", "1/250", 200, 2.8, 85),
            (2, "wedding_02.NEF",   ".NEF", 100, "b2786c7afe34f892"+"0"*48, "2026-01-15T14:23:12", "1/250", 200, 2.8, 85),
            (3, "wedding_03.NEF",   ".NEF", 100, "b2786c7afe34f893"+"0"*48, "2026-01-15T14:23:13", "1/250", 200, 2.8, 85),
            (4, "landscape_01.CR3", ".CR3", 200, "b2786c7afe34f894"+"0"*48, "2026-01-16T07:11:05", "1/60",  100, 8.0, 24),
            (5, "landscape_02.CR3", ".CR3", 200, "b2786c7afe34f895"+"0"*48, "2026-01-16T07:11:06", "1/60",  100, 8.0, 24),
            (6, "portrait_01.jpg",  ".jpg", None, "deadbeefcafefeed"+"0"*48, "2026-01-20T16:45:00", "1/125", 400, 1.8, 50),
        ]
        previews = [
            (1, 0.75, 0.60, 0.80, 0.75, 0.70, 0.85, 0.80, "Bride and groom walking in a sunny park with soft lens flare"),
            (2, 0.94, 0.85, 0.95, 0.92, 0.90, 0.95, 0.95, "Elegant black & white portrait of the bride smiling radiantly"),
            (3, 0.45, 0.30, 0.50, 0.40, 0.50, 0.45, 0.50, "Slightly out-of-focus candid photo of wedding guests talking"),
            (4, 0.88, 0.80, 0.90, 0.85, 0.95, 0.70, 0.90, "Stunning sunset over snowy mountain peaks reflecting in a lake"),
            (5, 0.62, 0.55, 0.65, 0.60, 0.70, 0.70, 0.60, "Overcast cloudy sky blocking the mountain range view"),
            (6, 0.79, 0.75, 0.80, 0.75, 0.85, 0.85, 0.80, "Close up headshot with warm, creamy golden hour bokeh"),
        ]
        cur.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", files)
        cur.executemany("INSERT OR REPLACE INTO previews VALUES (?,?,?,?,?,?,?,?,?)", previews)
        conn.commit(); conn.close()
        print("[LVS DB] Sample database populated.\n")
    except Exception as e:
        print(f"[LVS DB] {e}")


# -----------------------------------------------------------------------------
# CLI dispatch  (so backend can be invoked standalone for utility ops)
# -----------------------------------------------------------------------------
def _print_banner() -> None:
    print("-" * 64)
    print(f" {__product_name__} v{__version__}  ({__license__})")
    print(f" Codename: \"{__codename__}\"     (C) 2026 {__author__}")
    print("-" * 64)


def _cli_launch_gate(base_path: str) -> int:
    dm = LVSDataManager(base_path)
    g = launch_gate(dm)
    print(json.dumps(g, indent=2))
    return 0 if g["ready"] else 2   # 2 = "not ready" so .bat can branch


if __name__ == "__main__":
    if "--patch-fsdb" in sys.argv:
        sys.exit(patch_fsdb_cli())
    if "--gate" in sys.argv:
        sys.exit(_cli_launch_gate(os.path.dirname(os.path.abspath(__file__))))
    _print_banner()
    print("This module is a library.  Run lvs_main.py to launch the app.")
    print("Diagnostic CLI:")
    print("    python lvs_backend.py --patch-fsdb     # patch FSSettings.db")
    print("    python lvs_backend.py --gate           # print launch readiness")

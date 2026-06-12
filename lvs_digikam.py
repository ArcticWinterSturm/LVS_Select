#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  DigiKam viewer adapter
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  DigiKam integration for LVS. Two surfaces:
#
#  1. VIEWER ADAPTER — polls DigiKam's window title for the live HUD overlay.
#     Same pattern as FastStone. Title-based EnumWindows (no brittle Qt class
#     names — those change between Qt 5/6 versions).
#
#  2. CULL WRITE-BACK — after the tasker finalises, writes results INTO
#     DigiKam's SQLite database so ratings and Pick Labels appear natively
#     inside DigiKam as though the user did an assisted cull.
#
#  SCHEMA (verified against digikam dbconfig.xml.cmake upstream):
#
#    Images            — id, album, name, status, category, modificationDate,
#                        fileSize, uniqueHash, manualOrder.  UNIQUE(album,name).
#    ImageInformation  — imageid INTEGER PRIMARY KEY, rating INTEGER, ...
#                        FK: imageid → Images.id
#    Albums            — id, albumRoot, relativePath, date, caption, ...
#    AlbumRoots        — id, label, status, type, identifier, specificPath
#    Tags              — id, pid, name, icon, iconkde.  UNIQUE(name,pid).
#    TagsTree          — id, pid.  UNIQUE(id,pid).  Closure-table for hierarchy.
#    ImageTags         — imageid, tagid.  UNIQUE(imageid,tagid).
#    TagProperties     — tagid, property, value  (e.g. internalTag, pickLabelTag)
#
#  Pick Labels are stored as Tags with names:
#       "Pick Label - Rejected"  (PickLabel=1 in XMP)
#       "Pick Label - Pending"   (PickLabel=2 in XMP)
#       "Pick Label - Accepted"  (PickLabel=3 in XMP)
#       "Pick Label - None"      (PickLabel=0 in XMP)
#  and linked to images via ImageTags(imageid, tagid).
#
#  SAFETY:
#    * Only writes when DigiKam is NOT running (SQLite single-writer).
#    * Creates .lvs.bak backup before touching digikam4.db.
#    * All write-back runs in a single transaction — commit or rollback.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import sys
import time
import sqlite3
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, List, Callable, Dict, Any, Tuple

from lvs_backend import (
    ViewerAdapter, SELECT_NAMES, SELECT_COUNT,
    RAW_EXTS, PICTURE_EXTS,
)

try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


# ============================================================================
# Constants — DigiKam Pick Label tag names (verified against upstream source)
# ============================================================================
TAG_PICK_ACCEPTED = "Pick Label - Accepted"
TAG_PICK_REJECTED = "Pick Label - Rejected"
TAG_PICK_PENDING  = "Pick Label - Pending"

# XMP-digiKam:PickLabel values (exiv2 verified)
PICK_NONE     = 0
PICK_REJECTED = 1
PICK_PENDING  = 2
PICK_ACCEPTED = 3


# ============================================================================
# DB discovery
# ============================================================================
def _find_digikam_db() -> Optional[str]:
    """Locate digikam4.db on this machine. Checks known locations."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Pictures", "digikam4.db"),
        os.path.join(home, "Pictures", "digikam", "digikam4.db"),
    ]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA",
                               os.path.join(home, "AppData", "Local"))
        candidates += [
            os.path.join(local, "digikam", "digikam4.db"),
        ]

    # Walk common collection dirs one level deep
    for base in [os.path.join(home, "Pictures"),
                 os.path.join(home, "Photos"),
                 os.path.join(home, "Images")]:
        if os.path.isdir(base):
            try:
                for entry in os.scandir(base):
                    if entry.is_dir() and not entry.name.startswith("."):
                        cand = os.path.join(entry.path, "digikam4.db")
                        if os.path.isfile(cand):
                            candidates.append(cand)
            except PermissionError:
                pass

    for c in candidates:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def _digikam_is_running() -> bool:
    """Check if digikam.exe process exists."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq digikam.exe", "/NH"],
                capture_output=True, text=True, timeout=6)
            return "digikam.exe" in result.stdout.lower()
        else:
            result = subprocess.run(
                ["pgrep", "-x", "digikam"], capture_output=True, text=True,
                timeout=6)
            return result.returncode == 0
    except Exception:
        pass
    return False


# ============================================================================
# Viewer Adapter  (title-bar polling — no brittle Qt class name assumptions)
# ============================================================================
class DigikamAdapter(ViewerAdapter):
    """Concrete ViewerAdapter for DigiKam (Windows + Linux)."""

    id           = "digikam"
    display_name = "digiKam"

    # DigiKam title patterns (Qt6/Windows tested):
    #   "image.jpg — digiKam 9.0.0"
    #   "image.jpg – digiKam"
    #   "image.jpg (digiKam)"
    #   "image.jpg - digiKam"
    _DK_TITLE_RE = re.compile(
        r'^(.+?)\s+[-–—]\s+digiKam',
        re.IGNORECASE,
    )
    # Secondary: parenthetical form
    _DK_TITLE_RE2 = re.compile(
        r'^(.+?)\s+\(digiKam',
        re.IGNORECASE,
    )

    EXE_CANDIDATES = [
        r"C:\Program Files\digiKam\digikam.exe",
        r"C:\Program Files (x86)\digiKam\digikam.exe",
    ]

    def __init__(self, base_path: str):
        self.base_path = base_path
        self._watcher: Optional[DigikamWatcher] = None

    def is_available(self) -> bool:
        if self.is_running():
            return True
        for c in self.EXE_CANDIDATES:
            if os.path.exists(c):
                return True
        if shutil.which("digikam"):
            return True
        return bool(_find_digikam_db())

    def is_running(self) -> bool:
        if not HAS_WIN32:
            return _digikam_is_running()
        # Check for any visible window with "digiKam" in the title
        found = []
        def _enum(hwnd, _):
            try:
                t = win32gui.GetWindowText(hwnd)
                if ("digiKam" in t or "digikam" in t) and win32gui.IsWindowVisible(hwnd):
                    found.append(hwnd)
            except Exception:
                pass
            return True
        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            pass
        return bool(found)

    def supports_hotkeys(self) -> bool:
        return False  # AHK hotkeys are FastStone-specific

    def start_watcher(self, on_image, on_hide) -> threading.Thread:
        self._watcher = DigikamWatcher(on_image, on_hide)
        self._watcher.start()
        return self._watcher

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def configure(self, base_path, select_paths) -> Dict[str, Any]:
        return {"registry": False, "binary": False,
                "errors": ["DigiKam: use in-app Settings → Configure"]}


# ============================================================================
# Watcher — polls DigiKam titles via EnumWindows ~3×/s
# ============================================================================
class DigikamWatcher(threading.Thread):
    """Polls visible windows for a DigiKam title, extracts current filename."""

    def __init__(self,
                 on_image: Callable[[str, bool], None],
                 on_hide:  Callable[[], None]):
        super().__init__(daemon=True, name="DigikamWatcher")
        self.on_image = on_image
        self.on_hide  = on_hide
        self._stop    = threading.Event()
        self._last_filename: Optional[str] = None
        self._was_visible = False

    def stop(self) -> None:
        self._stop.set()

    def _extract_filename(self, title: str) -> Optional[str]:
        """DSC_1234.NEF — digiKam 9.0.0 → 'DSC_1234.NEF'"""
        for pat in (DigikamAdapter._DK_TITLE_RE, DigikamAdapter._DK_TITLE_RE2):
            m = pat.search(title)
            if m:
                raw = m.group(1).strip()
                # Could be a full path – take just the basename
                base = os.path.basename(raw)
                if base and '.' in base:
                    return base
                return base if base else None
        return None

    def run(self) -> None:
        print("\n[Watcher/DK] DigiKam background monitor started.")
        while not self._stop.is_set():
            try:
                if not HAS_WIN32:
                    time.sleep(1.0)
                    continue

                # Enumerate all visible windows, find the first DigiKam one
                best_hwnd = None
                best_title = ""
                def _enum(hwnd, _):
                    nonlocal best_hwnd, best_title
                    try:
                        t = win32gui.GetWindowText(hwnd)
                        if "digiKam" in t and win32gui.IsWindowVisible(hwnd):
                            fname = self._extract_filename(t)
                            # Prefer windows showing an image filename
                            if fname and not best_hwnd:
                                best_hwnd = hwnd
                                best_title = t
                            elif not best_hwnd:
                                best_hwnd = hwnd
                                best_title = t
                    except Exception:
                        pass
                    return True
                try:
                    win32gui.EnumWindows(_enum, None)
                except Exception:
                    pass

                if best_hwnd:
                    fname = self._extract_filename(best_title)
                    if fname:
                        if fname != self._last_filename:
                            print(f"[Watcher/DK] Image: "
                                  f"'{self._last_filename}' → '{fname}'")
                            self._last_filename = fname
                        try:
                            self.on_image(fname, True)
                        except Exception as e:
                            print(f"[Watcher/DK.on_image] {e}")
                    self._was_visible = True
                else:
                    if self._was_visible:
                        try:
                            self.on_hide()
                        except Exception:
                            pass
                        self._last_filename = None
                        self._was_visible = False
                        print("[Watcher/DK] DigiKam hidden/closed.")
            except Exception:
                import traceback
                print(f"\n[Watcher/DK Exception]\n{traceback.format_exc()}")
            self._stop.wait(0.3)


# ============================================================================
# CULL WRITE-BACK — writes LVS results INTO DigiKam's database
# ============================================================================
class DigikamCullWriter:
    """
    Writes LVS cull results back into DigiKam's SQLite core database.

    After the tasker finalises, call write_cull_results() to set:
      * ImageInformation.rating (0–5 stars) on accepted images
      * ImageTags → Pick Label - Accepted tag on selected images
      * ImageTags → Pick Label - Rejected tag on non-selected images

    DigiKam reads these natively — the cull appears as though done inside
    the app.  Ratings sync back to XMP on next "Write Metadata to Files".
    """

    def __init__(self, digikam_db_path: str):
        self.db_path = digikam_db_path
        self._backup_path = digikam_db_path + ".lvs.bak"

    # -------------------------------------------------------------- tag cache
    def _ensure_tags(self, cur: sqlite3.Cursor) -> Dict[str, int]:
        """
        Ensure Pick Label tags exist. Returns {name: id}.
        Only inserts if the tag name doesn't already exist (UNIQUE name,pid).
        """
        tag_ids: Dict[str, int] = {}
        for tag_name in [TAG_PICK_ACCEPTED, TAG_PICK_REJECTED, TAG_PICK_PENDING]:
            cur.execute("SELECT id FROM Tags WHERE name = ?", (tag_name,))
            row = cur.fetchone()
            if row:
                tag_ids[tag_name] = row[0]
            else:
                # pid=0 = root-level tag; icon/iconkde default
                cur.execute(
                    "INSERT INTO Tags (pid, name, icon, iconkde) "
                    "VALUES (0, ?, 0, '')", (tag_name,))
                tag_ids[tag_name] = cur.lastrowid
        return tag_ids

    # -------------------------------------------------------------- image lookup
    def _find_image_by_filename(self, cur: sqlite3.Cursor,
                                filename: str) -> Optional[int]:
        """
        Find Images.id for a filename in DigiKam's Images table.
        Images.name stores just the filename (e.g. 'DSC08625.JPG').
        Returns the image id or None.
        """
        stem = os.path.splitext(filename)[0]
        # Try exact name match first
        cur.execute(
            "SELECT id FROM Images WHERE name = ? LIMIT 1",
            (filename,))
        row = cur.fetchone()
        if row:
            return row[0]
        # Try LIKE with stem (handles extension variants)
        cur.execute(
            "SELECT id FROM Images WHERE name LIKE ? LIMIT 1",
            (f"{stem}.%",))
        row = cur.fetchone()
        if row:
            return row[0]
        return None

    # -------------------------------------------------------------- rating
    def _set_rating(self, cur: sqlite3.Cursor, imageid: int, rating: int):
        """Set ImageInformation.rating. Upsert (INSERT OR REPLACE)."""
        cur.execute(
            "SELECT imageid FROM ImageInformation WHERE imageid = ?",
            (imageid,))
        if cur.fetchone():
            cur.execute(
                "UPDATE ImageInformation SET rating = ? WHERE imageid = ?",
                (rating, imageid))
        else:
            cur.execute(
                "INSERT INTO ImageInformation (imageid, rating) "
                "VALUES (?, ?)", (imageid, rating))

    # -------------------------------------------------------------- pick label
    def _set_pick_label(self, cur: sqlite3.Cursor, imageid: int,
                        tag_id: int, all_pick_ids: set):
        """Assign a Pick Label tag, removing any prior Pick Label first."""
        # Remove existing Pick Label tags from this image
        if all_pick_ids:
            placeholders = ",".join("?" * len(all_pick_ids))
            cur.execute(
                f"DELETE FROM ImageTags WHERE imageid = ? "
                f"AND tagid IN ({placeholders})",
                (imageid, *all_pick_ids))
        # Assign the new label
        cur.execute(
            "INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)",
            (imageid, tag_id))

    # -------------------------------------------------------------- main API
    def write_cull_results(
        self,
        selected: List[str],          # clean filenames in select/ pool
        rated: Optional[Dict[str, int]] = None,  # filename → rating (1-5)
        rejected: Optional[List[str]] = None,     # explicit reject filenames
    ) -> Dict[str, Any]:
        """
        Write cull results into DigiKam's database.

        Returns {success, selected, rejected, rated, errors, error?}
        """
        if _digikam_is_running():
            return {
                "success": False,
                "error": "DigiKam is running — close it before writing cull results.",
                "selected": 0, "rejected": 0, "rated": 0, "errors": 1,
            }

        if not os.path.isfile(self.db_path):
            return {
                "success": False,
                "error": f"DigiKam database not found: {self.db_path}",
                "selected": 0, "rejected": 0, "rated": 0, "errors": 1,
            }

        # Backup before touching
        try:
            shutil.copy2(self.db_path, self._backup_path)
        except Exception as e:
            return {
                "success": False,
                "error": f"Could not back up digikam4.db: {e}",
                "selected": 0, "rejected": 0, "rated": 0, "errors": 1,
            }

        if rated is None:
            rated = {}

        stats = {"selected": 0, "rejected": 0, "rated": 0, "errors": 0}
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("BEGIN")

            tag_ids = self._ensure_tags(cur)
            all_pick_ids = set(tag_ids.values())

            # --- Selected images → rating + Accepted pick label ---
            for fname in selected:
                imageid = self._find_image_by_filename(cur, fname)
                if not imageid:
                    # Try without hash suffix
                    clean = re.sub(r'_[0-9a-fA-F]{16}', '', fname)
                    if clean != fname:
                        imageid = self._find_image_by_filename(cur, clean)
                if not imageid:
                    # Try common extensions (.JPG/.jpg/.NEF etc.)
                    stem = os.path.splitext(fname)[0]
                    for ext in (".JPG", ".jpg", ".NEF", ".nef", ".ARW", ".arw",
                                ".CR2", ".cr2", ".CR3", ".cr3", ".DNG", ".dng"):
                        cur.execute(
                            "SELECT id FROM Images WHERE name LIKE ? LIMIT 1",
                            (f"{stem}{ext}",))
                        row = cur.fetchone()
                        if row:
                            imageid = row[0]
                            break
                if not imageid:
                    stats["errors"] += 1
                    continue

                rating = rated.get(fname, 3)  # default 3★
                self._set_rating(cur, imageid, rating)
                self._set_pick_label(
                    cur, imageid, tag_ids[TAG_PICK_ACCEPTED], all_pick_ids)
                stats["selected"] += 1
                stats["rated"] += 1

            # --- Rejected images → Rejected pick label ---
            if rejected:
                for fname in rejected:
                    imageid = self._find_image_by_filename(cur, fname)
                    if not imageid:
                        stats["errors"] += 1
                        continue
                    self._set_pick_label(
                        cur, imageid, tag_ids[TAG_PICK_REJECTED], all_pick_ids)
                    stats["rejected"] += 1

            conn.commit()
            stats["success"] = True

            print(f"[DigiKam] Cull written: {stats['selected']} accepted, "
                  f"{stats['rejected']} rejected, {stats['rated']} rated, "
                  f"{stats['errors']} not found.")

        except Exception as e:
            stats["success"] = False
            stats["error"] = str(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DigiKam] Write-back failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return stats


# ============================================================================
# Exiftool fallback — write PickLabel + Rating to file metadata
# Works while DigiKam is OPEN (user runs "Read Metadata from Files" after)
# ============================================================================
def exiftool_write_labels(
    filepath: str,
    rating: int = 0,
    accepted: Optional[bool] = None,
    exiftool_bin: str = "exiftool",
) -> Tuple[bool, str]:
    """
    Write Rating and XMP-digiKam:PickLabel via exiftool.
    DigiKam re-reads these on 'Item → Read Metadata from Files'.

    Returns (success, message).
    """
    cmd = [exiftool_bin, "-overwrite_original", "-P"]
    if rating > 0:
        cmd.append(f"-Rating={rating}")
    if accepted is not None:
        label = PICK_ACCEPTED if accepted else PICK_REJECTED
        cmd.append(f"-XMP-digiKam:PickLabel={label}")
    cmd.append(str(filepath))

    if len(cmd) <= 4:  # nothing to write
        return True, "nothing to write"

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0:
            return True, f"Rating/PickLabel written to {os.path.basename(filepath)}"
        return False, f"exiftool error: {res.stderr.strip()}"
    except Exception as e:
        return False, str(e)


# ============================================================================
# Smoke test
# ============================================================================
if __name__ == "__main__":
    db = _find_digikam_db()
    if db:
        print(f"DigiKam DB found: {db}")
        print(f"DigiKam running: {_digikam_is_running()}")
    else:
        print("DigiKam DB not found on this system.")

    a = DigikamAdapter(os.getcwd())
    print(f"Adapter available: {a.is_available()}")
    print(f"Adapter running:   {a.is_running()}")
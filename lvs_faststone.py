#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  FastStone viewer adapter
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  Everything FastStone-Image-Viewer-specific lives in this file.  Anything
#  invariant (DB, raws, IPC, overlay UI) lives in lvs_backend.py / lvs_*_gui.py.
#
#  When porting to another viewer (digiKam, ApolloOne, etc.) you write a sibling
#  adapter file and register it in lvs_main.ADAPTERS — no overlay code changes.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import threading
import subprocess
from typing import Optional, List, Callable, Dict, Any

from lvs_backend import (
    ViewerAdapter,
    SELECT_NAMES,
    AHKManager,
)

try:
    import win32gui
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


# -----------------------------------------------------------------------------
# Registry helper (used by the configure() phase as a safety net — the AHK
# installer still writes them too).
# -----------------------------------------------------------------------------
def _write_registry_favourites(select_paths: List[str]) -> List[str]:
    """Write select_paths into BOTH known FastStone registry hives.  Returns
       a list of error strings (empty on success)."""
    errors: List[str] = []
    if os.name != "nt":
        return ["not Windows"]
    try:
        import winreg
    except ImportError:
        return ["winreg unavailable"]

    hives = [
        r"Software\FastStone\FSViewer",
        r"Software\FastStone\FastStone Image Viewer",
    ]
    for idx, path in enumerate(select_paths, 1):
        for hive_subpath in hives:
            sub = fr"{hive_subpath}\FavoriteFolder{idx}"
            try:
                k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0,
                                       winreg.KEY_SET_VALUE)
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, path)
                winreg.CloseKey(k)
            except OSError as e:
                errors.append(f"{sub}: {e}")
    return errors


# -----------------------------------------------------------------------------
# FastStone Adapter
# -----------------------------------------------------------------------------
class FastStoneAdapter(ViewerAdapter):
    """Concrete ViewerAdapter for FastStone Image Viewer (Windows)."""

    id           = "faststone"
    display_name = "FastStone Image Viewer"

    # Title pattern handles:
    #   "photo.jpg - FastStone Image Viewer"        (v7)
    #   "photo.jpg  -  FastStone Image Viewer 8.3"  (v8+)
    #   "C:\path\photo.jpg  -  FastStone Image Viewer"
    _FS_TITLE_RE = re.compile(r'^(.+?)\s+-\s+FastStone Image Viewer',
                              re.IGNORECASE)

    # Common executable name (for is_available even when not currently running)
    EXE_CANDIDATES = [
        r"C:\Program Files\FastStone Image Viewer\FSViewer.exe",
        r"C:\Program Files (x86)\FastStone Image Viewer\FSViewer.exe",
        r"C:\Program Files\FastStone Soft\FastStone Image Viewer\FSViewer.exe",
    ]

    def __init__(self, base_path: str):
        self.base_path = base_path
        self._watcher: Optional[FastStoneWatcher] = None

    # --------------------------------------------------------- availability
    def is_available(self) -> bool:
        if not HAS_WIN32:
            return False
        # Available if executable is installed OR currently running
        if self.is_running():
            return True
        for c in self.EXE_CANDIDATES:
            if os.path.exists(c):
                return True
        # Fallback: anything in PATH
        if shutil.which("FSViewer.exe"):
            return True
        return False

    def is_running(self) -> bool:
        if not HAS_WIN32:
            return False
        h = (win32gui.FindWindow("FastStoneImageViewerMainForm.UnicodeClass", None)
             or win32gui.FindWindow("FSViewer", None))
        if h:
            return True
        # Also check fullscreen window class (single-image, no main form)
        found = []
        def _enum(hwnd, _):
            try:
                if (win32gui.GetClassName(hwnd) == "TFullScreenWindow"
                        and win32gui.IsWindowVisible(hwnd)):
                    found.append(hwnd)
            except Exception:
                pass
            return True
        try: win32gui.EnumWindows(_enum, None)
        except Exception: pass
        return bool(found)

    def supports_hotkeys(self) -> bool:
        # Hotkeys are provided by the bundled AHK script — always Yes on Win.
        return os.name == "nt"

    # ------------------------------------------------------------ watcher
    def start_watcher(
        self,
        on_image: Callable[[str, bool], None],
        on_hide:  Callable[[], None],
    ) -> threading.Thread:
        self._watcher = FastStoneWatcher(on_image, on_hide)
        self._watcher.start()
        return self._watcher

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    # ------------------------------------------------------------ configure
    def configure(self, base_path: str,
                  select_paths: List[str]) -> Dict[str, Any]:
        """
        Belt-and-braces registry pass.  The AHK installer also does this;
        having it here as well means launching from inside Python (e.g.
        a tasker GUI button "Re-apply FastStone shortcuts") works without
        going through AHK.  The Delphi FSSettings.db patch is left to the
        CLI helper because closing FastStone first is required.
        """
        errors = _write_registry_favourites(select_paths)
        return {
            "registry": len(errors) == 0,
            "binary":   False,
            "errors":   errors,
        }


# -----------------------------------------------------------------------------
# FastStoneWatcher — title-bar polling thread
# -----------------------------------------------------------------------------
class FastStoneWatcher(threading.Thread):
    """
    Polls win32 windows ~3× per second and emits viewer-agnostic
    `on_image(filename, is_fullscreen=True)` / `on_hide()` callbacks.
    """

    _FS_TITLE_RE = FastStoneAdapter._FS_TITLE_RE

    def __init__(self,
                 on_image: Callable[[str, bool], None],
                 on_hide:  Callable[[], None]):
        super().__init__(daemon=True, name="FastStoneWatcher")
        self.on_image = on_image
        self.on_hide  = on_hide
        self._stop    = threading.Event()
        self.last_state: Dict[str, Any] = {
            "faststone_running":   False,
            "fullscreen_active":   False,
            "single_image_active": False,
            "hwnd":     None,
            "filename": None,
        }

    def stop(self) -> None:
        self._stop.set()

    def _emit_image(self, fname: str) -> None:
        try:
            self.on_image(fname, True)
        except Exception as e:
            print(f"[Watcher.on_image] {e}")

    def _emit_hide(self) -> None:
        try:
            self.on_hide()
        except Exception as e:
            print(f"[Watcher.on_hide] {e}")

    def run(self) -> None:
        print("\n[Watcher/FS] Live background monitor thread started.")
        while not self._stop.is_set():
            try:
                if not HAS_WIN32:
                    time.sleep(1.0); continue

                # 1. Look for visible TFullScreenWindow
                fs_hwnds: List[int] = []
                def enum_fs(h, extra):
                    try:
                        if (win32gui.GetClassName(h) == "TFullScreenWindow"
                                and win32gui.IsWindowVisible(h)):
                            extra.append(h)
                    except Exception:
                        pass
                    return True
                try: win32gui.EnumWindows(enum_fs, fs_hwnds)
                except Exception: pass

                is_fs = bool(fs_hwnds)
                if is_fs != self.last_state["fullscreen_active"]:
                    print(f"[Watcher/FS] Fullscreen "
                          f"{'entered' if is_fs else 'exited'}.")
                    self.last_state["fullscreen_active"] = is_fs

                if is_fs:
                    fs_hwnd = fs_hwnds[0]
                    _, fs_pid = win32process.GetWindowThreadProcessId(fs_hwnd)
                    main_hwnds: List[int] = []
                    def enum_main(h, extra):
                        try:
                            cls = win32gui.GetClassName(h)
                            if cls in ("FastStoneImageViewerMainForm.UnicodeClass",
                                       "FSViewer"):
                                _, pid = win32process.GetWindowThreadProcessId(h)
                                if pid == fs_pid:
                                    extra.append(h)
                        except Exception:
                            pass
                        return True
                    try: win32gui.EnumWindows(enum_main, main_hwnds)
                    except Exception: pass

                    if not main_hwnds:
                        # Last-ditch: any window in that PID with matching title
                        def enum_fb(h, extra):
                            try:
                                t = win32gui.GetWindowText(h)
                                if FastStoneWatcher._FS_TITLE_RE.search(t):
                                    _, pid = win32process.GetWindowThreadProcessId(h)
                                    if pid == fs_pid:
                                        extra.append(h)
                            except Exception:
                                pass
                            return True
                        try: win32gui.EnumWindows(enum_fb, main_hwnds)
                        except Exception: pass

                    if main_hwnds:
                        title = win32gui.GetWindowText(main_hwnds[0])
                        m = FastStoneWatcher._FS_TITLE_RE.match(title)
                        if m:
                            fname = os.path.basename(m.group(1).strip())
                            if fname != self.last_state["filename"]:
                                print(f"[Watcher/FS] FS image: "
                                      f"'{self.last_state['filename']}' → '{fname}'")
                                self.last_state["filename"] = fname
                                self.last_state["faststone_running"] = True
                            self._emit_image(fname)
                        else:
                            self._emit_hide()
                    else:
                        self._emit_hide()
                else:
                    # Browser / single-image mode
                    main_hwnd = (win32gui.FindWindow(
                        "FastStoneImageViewerMainForm.UnicodeClass", None)
                        or win32gui.FindWindow("FSViewer", None))
                    if main_hwnd:
                        if not self.last_state["faststone_running"]:
                            print(f"[Watcher/FS] FastStone detected, HWND={main_hwnd}")
                            self.last_state["faststone_running"] = True
                            self.last_state["hwnd"] = main_hwnd
                        title = win32gui.GetWindowText(main_hwnd)
                        m = FastStoneWatcher._FS_TITLE_RE.match(title)
                        if m:
                            fname = os.path.basename(m.group(1).strip())
                            if not self.last_state["single_image_active"]:
                                print("[Watcher/FS] Single-image viewer entered.")
                                self.last_state["single_image_active"] = True
                            if fname != self.last_state["filename"]:
                                print(f"[Watcher/FS] Image: "
                                      f"'{self.last_state['filename']}' → '{fname}'")
                                self.last_state["filename"] = fname
                            self._emit_image(fname)
                        else:
                            if self.last_state["single_image_active"]:
                                print("[Watcher/FS] Returned to browser mode.")
                                self.last_state["single_image_active"] = False
                                self.last_state["filename"] = None
                            self._emit_hide()
                    else:
                        if self.last_state["faststone_running"]:
                            print("[Watcher/FS] FastStone closed.")
                            self.last_state = {
                                "faststone_running": False,
                                "fullscreen_active": False,
                                "single_image_active": False,
                                "hwnd": None, "filename": None,
                            }
                        self._emit_hide()
            except Exception:
                import traceback
                print(f"\n[Watcher/FS Exception]\n{traceback.format_exc()}")
            self._stop.wait(0.3)


# -----------------------------------------------------------------------------
# Placeholder stub adapters (greyed out in the tasker UI)
# -----------------------------------------------------------------------------
class PlaceholderViewerAdapter(ViewerAdapter):
    """Generic stub for viewers planned but not yet implemented."""
    def __init__(self, viewer_id: str, display_name: str):
        self.id = viewer_id
        self.display_name = display_name

    def is_available(self) -> bool: return False
    def is_running(self)   -> bool: return False
    def start_watcher(self, on_image, on_hide):
        raise NotImplementedError(
            f"{self.display_name} adapter is not yet implemented")
    def configure(self, base_path, select_paths):
        return {"registry": False, "binary": False,
                "errors": ["adapter not implemented"]}


# -----------------------------------------------------------------------------
# Convenience  (for `python lvs_faststone.py` smoke-test)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    a = FastStoneAdapter(os.getcwd())
    print(f"available: {a.is_available()}")
    print(f"running:   {a.is_running()}")
    print(f"hotkeys:   {a.supports_hotkeys()}")

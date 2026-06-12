#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  Unified entry point
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  Internal codename "Aesthetic-Darwinism".
#
#  Launch sequence
#  ---------------
#    1. Locate workspace = directory of this script.
#    2. ingest.db is READ-ONLY input produced upstream — LVS never creates it.
#       If it is missing the app runs without DB-backed scores.
#    3. Run launch_gate():
#         a. If NO select1..5 folders exist at all → show a setup-only
#            dialog (the AHK installer runs, creates folders + registry).
#            Exit so the user can re-launch when ready.
#         b. If select folders exist but contain NO pictures → cull is
#            considered "not undertaken yet"; launch overlay only (no
#            tasker auto-open, no AHK auto-launch), so user can stage
#            the first picks.  Tasker is available via tray.
#         c. If at least ONE picture is in ANY selectN → cull is in
#            progress: launch overlay + AHK + (iff viewer is RUNNING)
#            launch tasker side-by-side.
#    4. Wire IPC pipe, signal handlers, atexit cleanup.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import signal
import time
import threading
import atexit
from typing import Optional, List

from lvs_backend import (
    LVSDataManager, AHKManager, AHKPipeListener,
    ViewerAdapter, launch_gate,
    SingleInstance, free_stale_ipc_pipe,
    SELECT_NAMES, AHK_PIPE_NAME,
    __version__, __product_name__, __codename__, __license__, __author__,
)

# Adapters (FastStone is the only real one today; placeholders are stubs)
from lvs_faststone import FastStoneAdapter, PlaceholderViewerAdapter
from lvs_digikam import DigikamAdapter


# ============================================================================
# Adapter registry
# ============================================================================
def build_adapters(base_path: str):
    active = FastStoneAdapter(base_path)
    # Real adapters (available on this system if the app is installed)
    alternates = [
        DigikamAdapter(base_path),
    ]
    placeholders = [
        PlaceholderViewerAdapter("gthumb",   "gThumb (Linux)"),
        PlaceholderViewerAdapter("apollo",   "ApolloOne (macOS)"),
        PlaceholderViewerAdapter("preview",  "Preview (macOS)"),
    ]
    return active, alternates, placeholders


# ============================================================================
# Banner
# ============================================================================
def _banner(dm: LVSDataManager, gate: dict,
            adapter: ViewerAdapter, alternates: list,
            ahk_started: bool, tasker_started: bool):
    print("-" * 68)
    print(f" {__product_name__}  v{__version__}  ({__license__})")
    print(f" Codename: \"{__codename__}\"     (C) 2026 {__author__}")
    print("-" * 68)
    print(f" Workspace      : {dm.base_path}")
    print(f" Database       : {dm.db_path}  ({dm.get_file_count():,} records)")
    print(f" Viewer         : {adapter.display_name}  "
          f"(available={adapter.is_available()}, running={adapter.is_running()})")
    for alt in alternates:
        print(f" Alternate      : {alt.display_name}  "
              f"(available={alt.is_available()}, running={alt.is_running()})")
    print(f" Hotkeys (AHK)  : {'active' if ahk_started else 'INACTIVE'}")
    print(f" IPC pipe       : {AHK_PIPE_NAME}")
    print(f" Tasker         : {'OPEN' if tasker_started else 'closed (tray to launch)'}")
    print(f" Cull picture # : {gate['picture_total']:,} across "
          f"{sum(1 for n in gate['per_folder'] if n > 0)}/5 buckets")
    print(f" Tray icon      : right-click for Pause / Reset / Raws / "
          f"Reload / Quit")
    print(f" Press Ctrl+C in this terminal to exit cleanly "
          f"(AHK macro is killed too).")
    print("-" * 68)


# ============================================================================
# Setup-only mode (gate failure: no folders at all)
# ============================================================================
def _setup_only_mode(base_path: str, dm: LVSDataManager) -> int:
    """
    When the launch gate reports "no select folders exist" we don't open
    the overlay.  Instead, kick off the AHK installer (which presents the
    Yes/No setup MsgBox), let it create the folders, and exit.  The user
    re-launches the .bat when they want to start culling.
    """
    print()
    print("=" * 68)
    print(" LVS Setup — no select1..5 folders found in:")
    print(f"   {base_path}")
    print(" Launching the AHK setup installer.  It will:")
    print("   * create select1..select5")
    print("   * register FastStone Favorite Folders 1..5 (registry)")
    print("   * write FastStone Copy/Move 1..5 slots (FSSettings.db)")
    print(" Re-launch run_as_admin.bat when you're ready to start culling.")
    print("=" * 68)
    ahk = AHKManager(base_path)
    if not ahk.start():
        print("[Setup] AHK launch failed — install AutoHotkey v2 and retry.")
        return 1
    # The AHK setup is interactive (MsgBox); the user will close it.
    # We just wait for the AHK process to exit, then quit.
    try:
        if ahk.proc:
            ahk.proc.wait()
    except KeyboardInterrupt:
        pass
    print("[Setup] AHK installer exited.  Re-launch the .bat to begin culling.")
    return 0


# ============================================================================
# Full run mode
# ============================================================================
def _run_full(base_path: str, dm: LVSDataManager, gate: dict) -> int:
    # Defer Qt imports so setup-only mode doesn't need them.
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QTimer

    from lvs_overlay_gui import (
        LVSOverlay, LVSTrayIcon, WatcherSignals, IPCSignals,
    )
    from lvs_tasker_gui import open_tasker

    # Adapters
    active_adapter, alternate_adapters, placeholder_adapters = build_adapters(base_path)

    # Qt application
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Cross-thread signals
    signals = WatcherSignals()
    overlay = LVSOverlay(dm, signals)

    # AHK macro
    ahk = AHKManager(base_path)
    ahk_started = ahk.start()

    # IPC pipe
    ipc = IPCSignals()
    ipc.copy_event.connect(overlay.on_ahk_event)
    pipe = AHKPipeListener(on_message=lambda m: ipc.copy_event.emit(m))
    pipe.start()

    # --- Viewer watchers (start/stop so we can switch between FastStone and Digikam) ---
    _viewer_state = {"active": "faststone", "watching": False}
    _dk_adapter = None
    # Find the Digikam adapter from alternates
    for a in alternate_adapters:
        if a.id == "digikam":
            _dk_adapter = a
            break

    def start_fs_watcher():
        if _viewer_state["watching"]:
            return
        active_adapter.start_watcher(
            on_image=lambda fname, fs: signals.update_request.emit(fname, fs),
            on_hide=lambda: signals.hide_request.emit(),
        )
        _viewer_state["watching"] = True

    def stop_fs_watcher():
        if not _viewer_state["watching"]:
            return
        try: active_adapter.stop_watcher()
        except Exception: pass
        _viewer_state["watching"] = False

    def start_dk_watcher():
        if _dk_adapter is None or not _dk_adapter.is_running():
            return
        try: _dk_adapter.stop_watcher()
        except Exception: pass
        _dk_adapter.start_watcher(
            on_image=lambda fname, fs: signals.update_request.emit(fname, fs),
            on_hide=lambda: signals.hide_request.emit(),
        )
        print("[Viewer] DigiKam watcher started.")

    def stop_dk_watcher():
        if _dk_adapter is None:
            return
        try: _dk_adapter.stop_watcher()
        except Exception: pass

    def on_viewer_change(viewer_id: str):
        """Called by the Tasker viewer picker (Qt main thread)."""
        _viewer_state["active"] = viewer_id
        if viewer_id == "digikam":
            stop_fs_watcher()
            start_dk_watcher()
            try:
                overlay._force_visible = True
                signals.hide_request.emit()
            except Exception:
                pass
            print("[Viewer] Switched to Digikam — FastStone watcher stopped, "
                  "DigiKam watcher active.")
        else:
            stop_dk_watcher()
            start_fs_watcher()
            print("[Viewer] Switched to FastStone — DigiKam watcher stopped, "
                  "FastStone watcher reconnected.")

    # Tray
    # Closure for "Open Tasker" — runs in a daemon thread.
    _tasker_open_flag = {"open": False}
    def open_tasker_async():
        if _tasker_open_flag["open"]:
            return
        _tasker_open_flag["open"] = True
        def reset(): _tasker_open_flag["open"] = False
        open_tasker(
            dm,
            active_adapter=active_adapter,
            placeholder_adapters=placeholder_adapters,
            active_viewer_id=_viewer_state["active"],
            on_close=reset,
            on_viewer_change=on_viewer_change,
            blocking=False,
        )

    tray = LVSTrayIcon(overlay, dm, ahk, on_open_tasker=open_tasker_async)

    # Start the FastStone window watcher
    start_fs_watcher()

    # Auto-open the tasker IF cull is in progress (pictures in any selectN folder)
    tasker_auto = False
    if gate["picture_total"] > 0:
        open_tasker_async()
        tasker_auto = True

    # Ctrl+C / Qt quit handlers
    py_timer = QTimer(); py_timer.start(500)
    py_timer.timeout.connect(lambda: None)

    def shutdown(*_a):
        print("\n[LVS] Shutting down...")
        try: tray.hide()
        except Exception: pass
        try: active_adapter.stop_watcher()
        except Exception: pass
        try: pipe.stop()
        except Exception: pass
        try: ahk.stop()
        except Exception: pass
        try: dm.set_raws_root_override(None)  # wipe in-memory raws path
        except Exception: pass
        QApplication.quit()

    signal.signal(signal.SIGINT, lambda *a: shutdown())
    app.aboutToQuit.connect(lambda: (ahk.stop(), pipe.stop()))

    _banner(dm, gate, active_adapter, alternate_adapters, ahk_started, tasker_auto)

    try:
        rc = app.exec()
    finally:
        try: active_adapter.stop_watcher()
        except Exception: pass
        try: pipe.stop()
        except Exception: pass
        try: ahk.stop()
        except Exception: pass
    return rc


# ============================================================================
# Entry
# ============================================================================
def _single_instance_or_exit() -> Optional["SingleInstance"]:
    """
    Guard against a second LVS launch (the "All pipes are busy" double-run).
    If another instance is already live, tell the user and offer to take over
    by pressing a key (frees a stale pipe/lock); pressing 'q' aborts.
    Returns the held SingleInstance on success, or None if the user aborts.
    """
    inst = SingleInstance()
    if not inst.already_running:
        return inst

    print()
    print("=" * 64)
    print(" LVS is ALREADY RUNNING.")
    print(" (A second copy can't open the AHK IPC pipe — 'All pipes are busy'.)")
    print()
    print("   [Enter] try to FIX it (free the stale pipe/lock and continue)")
    print("   [q]     abort this launch")
    print("=" * 64)
    try:
        ans = input(" > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "q"
    if ans == "q":
        print("[LVS] Aborted — the existing instance keeps running.")
        return None
    # User asked us to fix it: unstick a half-open pipe + clear a stale lock.
    free_stale_ipc_pipe()
    if inst.takeover():
        print("[LVS] Took over — continuing launch.")
        return inst
    print("[LVS] A live instance is genuinely running; close it first, then "
          "re-launch.  Aborting.")
    return None


def main() -> int:
    base_path = os.path.dirname(os.path.abspath(__file__))

    # Single-instance guard (prevents the double-bat "All pipes are busy").
    instance = _single_instance_or_exit()
    if instance is None:
        return 0

    # NOTE: LVS is strictly a READER of ingest.db.  It is produced upstream by
    # the ingest pipeline and must NEVER be created here.  If it is absent the
    # app still runs fully (overlay/tray/tasker) — every DB read degrades to a
    # sensible default.  (Previously ensure_sample_database() fabricated a demo
    # ingest.db on first launch, which polluted real workspaces.)
    dm = LVSDataManager(base_path)
    if not os.path.exists(dm.db_path):
        print(f"[LVS] ingest.db not found at {dm.db_path} — "
              f"running without DB-backed scores (read-only by design).")

    # Gate
    gate = launch_gate(dm)
    print(f"[Gate] folders present: {gate['present']}  "
          f"missing: {gate['missing']}  "
          f"pictures: {gate['picture_total']} "
          f"({gate['per_folder']})")

    try:
        if gate["needs_setup"]:
            return _setup_only_mode(base_path, dm)

        # Even if zero pictures yet (cull "not undertaken"), we still allow the
        # overlay to run — the user might want to read AI scores before staging
        # the first pick.  Auto-tasker only fires when pictures exist.
        return _run_full(base_path, dm, gate)
    finally:
        try:
            instance.release()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

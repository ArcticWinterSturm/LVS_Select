# LVS Selection Assist — Architecture Map
> **Codename:** Aesthetic-Darwinism  
> **Version:** 1.0.8  
> **License:** AGPL-3.0-or-later  
> **Pipeline position:** Turns a *human cull* (FastStone star sorting) into a rated, RAW-resolved manifest (`select/`, `edit.db`, `task.md`). The downstream half (Batonpass photo-edit + Rifinire mask-gen) consumes `edit.db`/`task.md` and the `select/` previews to develop the RAWs. **LVS only READS `ingest.db`; it never writes it.**

---

## 0. Architecture at a glance

LVS is a **Qt6 desktop assistant that rides on top of an image viewer** (FastStone today; the viewer is abstracted behind `ViewerAdapter`). It does three jobs:

| Job | Surface | Entry |
|-----|---------|-------|
| **Live HUD** — show AI scores/captions for whatever image the viewer is displaying, and offer click-to-open | `LVSOverlay` (Qt frameless always-on-top) | `lvs_main._run_full` → `lvs_overlay_gui.LVSOverlay` |
| **Cull capture** — map FastStone "Copy/Move to slot 1..5" hotkeys to `select1..5` folders and confirm each copy | AHK macro + named-pipe IPC | `lvs_setup_shortcuts.ahk` → `AHKPipeListener` → `LVSOverlay.on_ahk_event` |
| **Ingest/Tasker** — turn the `select1..5` cull into `select/` + `edit.db` + `task.md`, resolving each preview to its RAW and writing EXIF ratings | `LVSTaskerWindow` (Qt) and `LVSTasker` (CLI core) | tray "Open Tasker" / `python lvs_tasker.py` |

Data (relative to the script dir = "workspace"):

```
workspace/
├── ingest.db            # READ-ONLY input from the upstream ingest pipeline
│                        #   files(file_id, file_name, source_hash, capture_time, …)
│                        #   previews(file_id, score_*, caption, preview_path)
│                        #   tasker_ratings(…)  ← legacy/optional bridge table
├── raws/                # RAW originals (may have nested 184NIKON/185NIKON/ rollover dirs)
├── previews/            # the score-preview JPEGs the model saw
├── select1/ … select5/  # cull buckets (rating = folder number); JPEGs, optional _<16hex> hash suffix
├── select/              # tasker output: clean-named keepers (no hash)
├── edits/output/        # downstream editor output ({stem}_edited.jpg)
├── edit.db              # tasker OUTPUT handoff DB (edits table) — NOT ingest.db
├── task.md              # generated editing manifest for the downstream agent
└── select_settings.json # unified HUD position + tasker paths persistence
```

The **preview→RAW identity link** is a SHA-256:  
`files.source_hash` is the full SHA-256 of the RAW; the select/preview JPEG carries the **first 16 hex chars** of it as a `_<16hex>` filename suffix. This hash is the *only* reliable discriminator when camera counter rollover produces two RAWs with the same name (e.g. `DSC_5000.NEF` in both `184NIKON/` and `185NIKON/`).

```
                          ┌──────────── shared backend (no UI) ────────────┐
lvs_main.py ──imports──►  │  lvs_backend.py: LVSDataManager, AHKManager,    │
   │                      │  AHKPipeListener, ViewerAdapter, launch_gate,    │
   │                      │  raws_copy_back, DelphiTPF0Settings,              │
   │                      │  SingleInstance, free_stale_ipc_pipe              │
   │                      └─────────────────────────────────────────────────────┘
   │ builds adapters            ▲           ▲                 ▲
   ├─ lvs_faststone.py ─────────┘           │                 │ imports
   │   (FastStoneAdapter + watcher thread)   │                 │
   ├─ lvs_digikam.py ────────────────────────┤                 │
   │   (DigikamAdapter + DigikamCullWriter) │                 │
   ├─ lvs_overlay_gui.py ────────────────────┘ (HUD + tray)    │
   └─ lvs_tasker_gui.py ────────────────────────────────────────┘ (Tasker window)
        └─ imports lvs_tasker.py (LVSTasker core, paste parser, RawIndex, edit.db/task.md writers)
   └─ hesitancy_parser.py ──────────────────────────────────────── (caption clean + score nudges)

AHK macro (lvs_setup_shortcuts.ahk) ──named pipe \\.\pipe\LVS_AHK_IPC──► AHKPipeListener ──► overlay toast
```

---

## 1. File-by-file responsibility

| File | Lines | Role | Key symbols |
|------|-------|------|-------------|
| **`lvs_main.py`** | ~375 | Entry point + launch gate orchestration. Picks setup-only vs full-run, wires QApplication, overlay, tray, AHK, IPC, watcher, shutdown, single-instance guard, viewer-change callbacks. | `main`, `_run_full`, `_setup_only_mode`, `build_adapters`, `_single_instance_or_exit`, `_banner` |
| **`lvs_backend.py`** | ~1781 | **The invariant core.** SQLite reader + filesystem inspector (`LVSDataManager`), AHK subprocess supervisor, named-pipe IPC, `ViewerAdapter` ABC, launch gate, RAW copy-back, FastStone `FSSettings.db` Delphi patcher, HUD-position + tasker-path persistence, single-instance lock. **No Qt, no Tk, no FastStone specifics.** | `LVSDataManager`, `AHKManager`, `AHKPipeListener`, `ViewerAdapter`, `launch_gate`, `raws_copy_back`, `DelphiTPF0Settings`, `patch_fsdb_cli`, `SingleInstance`, `free_stale_ipc_pipe` |
| **`lvs_faststone.py`** | ~358 | Everything FastStone-specific: window-title regex, availability/running detection, the polling watcher thread, registry favourites writer. Implements `ViewerAdapter`. | `FastStoneAdapter`, `FastStoneWatcher`, `_write_registry_favourites`, `PlaceholderViewerAdapter` |
| **`lvs_digikam.py`** | ~569 | DigiKam viewer adapter + cull write-back. Window-title polling watcher, `DigikamCullWriter` writes LVS cull results (ratings, Pick Labels Accepted/Rejected) into DigiKam's `digikam4.db` SQLite database — the "friendly API" for assisted culling. Also supports exiftool round-trip when DigiKam is open. | `DigikamAdapter`, `DigikamWatcher`, `DigikamCullWriter`, `exiftool_write_labels`, `_find_digikam_db` |
| **`lvs_overlay_gui.py`** | ~925 | Qt6 HUD overlay + system tray. Viewer-agnostic (depends only on `LVSDataManager` + signal buses). Score grid, rating dots, JPEG/RAW toggle, open-cascade, burst celebration, fade toast, tray menu, caption anchoring with hesitancy flags. | `LVSOverlay`, `LVSTrayIcon`, `WatcherSignals`, `IPCSignals`, `RatingIndicator`, `ToggleWidget`, `trigger_burst_celebration`, `show_fade_toast`, `_render_caption_html`, `_open_jpeg_cascade` |
| **`lvs_tasker_gui.py`** | ~1996 | Qt6 Tasker window. Paths panel (colour-coded), star buckets, viewer switcher (FastStone/DigiKam), Mode A (normal execute), Mode B (paste parse + execute), inline RAW copy-back toggle, live log, DigiKam cull write-back on execute, Autocull mockup panel. Threads: `TaskerExecuteWorker`, `PasteExecuteWorker`, `CopyBackWorker`, `DigikamProbeWorker`. | `LVSTaskerWindow`, `open_tasker`, `PathRow`, `StarBucket`, `ViewerBox`, `AutocullPanel`, the four workers, `_writeback_to_digikam_if_active`, `_probe_previews_near_raws` |
| **`lvs_tasker.py`** | ~2256 | **Tasker CLI core** (also imported by the GUI). 7-phase ingest pipeline (scan→hash-authoritative verify→resolve RAW→EXIF→organize→edit.db→task.md), paste parser, `RawIndex`, `edit.db`/`task.md` writers, copy-while-rating helpers. `decision_callback` parameter for GUI-mode non-blocking hash mismatch handling. | `LVSTasker`, `RawIndex`, `parse_paste_block`, `populate_edit_db`, `write_task_md`, `find_exiftool`, `exiftool_set_rating`, `verify_against_ingest_db`, `query_ingest_metadata_by_stems` |
| **`lvs_setup_shortcuts.ahk`** | ~385 | AutoHotkey v2 macro: first-run setup wizard (creates `select1..5`, writes registry + `FSSettings.db`) and the `1..5` cull hotkeys that drive FastStone's Copy/Move dialog and report each copy over the pipe. | `RunSetup`, `CopyToSlot`, `PatchFSSettings`, `NotifyPipe`, `Build*Json` |
| **`hesitancy_parser.py`** | ~372 | Conservative Florence-2 caption cleaner + phrase-based score nudges. Reads optional `hesitancy.txt` in workspace. Display-only, ephemeral, no DB writes. | `HesitancyParser`, `PHRASE_SCORE_RULES`, `clean_caption`, `apply_score_modifiers`, `phrase_directions`, `display_phrases` |
| **`run_as_admin.bat`** | ~153 | Elevation + dependency check launcher. Pure-ASCII by design. | — |

---

### 2A. Launch (`lvs_main.main`)
`base_path` = script dir = workspace. `ingest.db` is **never created** by LVS — strict reader; absent → warn + degrade, never fail.

`launch_gate(dm)` dispatches: `needs_setup` (no `selectN` dirs) → `_setup_only_mode` (run AHK installer, exit); else → `_run_full`: build adapters → `QApplication` → `LVSOverlay` → `AHKManager.start` → `AHKPipeListener.start` → `LVSTrayIcon` → `FastStoneAdapter.start_watcher`. Tasker auto-opens when `picture_total > 0`.

Single-instance guard: `SingleInstance` (Windows named mutex / POSIX lock file) prevents the double-run "All pipes are busy" error. On collision, user can `[Enter]` take over or `[q]` abort.

### 2B. Live HUD update
`FastStoneWatcher` polls ~3×/s → emits `on_image(filename, is_fullscreen)` → `WatcherSignals.update_request` → `LVSOverlay.update_data`: `get_image_data` (DB) → scores/caption → `get_batch_rank` (burst position) → border colour → `_refresh_rating_dots` (`get_folder_ratings`) → render. Caption is cleaned via `HesitancyParser` and anchored on the most important flag (bold + red/green).

### 2C. Cull capture (AHK → overlay)
User presses `1..5` over FastStone → `CopyToSlot` sends `C`/digit/Enter, polls the dest folder count, sends `{"event":"copied",…}` (or `copy_failed`) over the named pipe → `AHKPipeListener.run` parses → `IPCSignals.copy_event` → `LVSOverlay.on_ahk_event` → silent rating-dot refresh (no toast).

### 2D. Open cascade (intentional — DO NOT "simplify")
`LVSOverlay._open_jpeg_cascade(idx)`:
- **T1** `edits/output/{stem}_edited.*` → `os.startfile` (open the edited result)
- **T2** `select/` pool match → **reveal in Explorer** (drag-friendly)
- **T3** `select{idx}/` match → open
- **T4** DB `previews.preview_path` → `previews/` → workspace → open
This file→folder→file fallback is deliberate. RAW mode (`_open_raw`) is a separate branch keyed by the JPEG/RAW pill toggle. RAW open shows a fade toast: "Opening …" that fades over 3s.

### 2E. Tasker ingest pipeline (`LVSTasker.execute`)
Phases: scan `selectN` → verify hashes → **resolve RAW (hash-authoritative)** → EXIF rating → move to `select/` → write `edit.db` → cleanup empty `selectN` → `task.md`. `ingest.db` is never written (tasker_run/record methods are intentional no-ops). RAW resolution gate: aborts if <80% of files resolve to a RAW.

### 2F. Paste mode (`PasteExecuteWorker` + `parse_paste_block`)
Parse `dir`/`ls`/`Get-ChildItem` output → dedupe by stem (highest rating wins) → match previews from `previews/` → resolve RAW via `RawIndex` → EXIF → copy to `select/` → write `edit.db` + `task.md`. Optional `tasker_ratings` insert into `ingest.db` (bridge table only — the one LVS write; file must already exist).

### 2G. RAW copy-back (`raws_copy_back`)
For every picture in any `selectN`, resolve its RAW name via DB, locate it under an external source root (walked recursively), and copy to `./raws/` — using the **hash as the authoritative discriminator** for rollover duplicates. *(see FIX-2)*

### 2H. DigiKam write-back (`_writeback_to_digikam_if_active`)
On every successful execute, if a local `digikam4.db` is found, LVS mirrors cull results into it: `ImageInformation.rating` + `ImageTags` Pick Label Accepted/Rejected. Then launches DigiKam and navigates to the `select/` album. This happens regardless of which viewer was active during culling.

---

## 3. Symbol → file index

### Preview ↔ RAW identity / hashing
| Change | Location |
|--------|----------|
| The 16-hex preview suffix regex | `lvs_backend.py` `_PREVIEW_HASH_RE`, `extract_preview_hash`, `strip_preview_hash`, `get_base_filename` (~83) |
| Full SHA-256 of a file | `lvs_backend.file_sha256`; `lvs_tasker.file_sha256` (two copies — keep in sync) |
| Get the full RAW hash for a preview | `lvs_backend.LVSDataManager.get_source_hash_for_preview` |

### RAW resolution / duplicate disambiguation  ← the fragile part
| Change | Location |
|--------|----------|
| Tasker/paste RAW index + match | `lvs_tasker.RawIndex` (`_build`, `resolve`, `_best_match`). **`resolve` treats `expected_hash` as authoritative; on an ambiguous miss it returns None instead of guessing.** |
| Overlay "Open RAW" resolution | `lvs_backend.LVSDataManager.find_raw_file(raw_filename, expected_hash=…)` |
| Copy-back source selection | `lvs_backend.raws_copy_back` (hash-hit → use; ambiguous miss → skip & record; single mismatch → copy + note; no hash → mtime/capture_time heuristic) |
| Camera-number extraction | `lvs_tasker.RE_CAMERA_NUMBER`, `extract_camera_number` |
| Ext preference for ties | `RawIndex.ext_priority` (NEF>NRW>ARW>DNG) |

### DB reads (LVS is read-only on ingest.db)
| Change | Location |
|--------|----------|
| Open ingest.db (read-only `mode=ro`) | `LVSDataManager.get_connection` |
| Scores/caption for HUD | `get_image_data` |
| Burst rank position | `get_batch_rank` |
| Folder ratings for the dots | `get_folder_ratings` (live folders → `tasker_ratings` → `edit.db`) |
| Preview path resolution order | `resolve_preview_path` / `get_preview_path_from_db` |
| RAW filename for a preview | `get_raw_filename` |

### Launch gate / modes / single-instance
| Change | Location |
|--------|----------|
| Readiness classification | `lvs_backend.launch_gate` (`needs_setup`/`picture_total`/`per_folder`) |
| Single-instance guard | `lvs_backend.SingleInstance`, `free_stale_ipc_pipe` |
| Setup-only flow | `lvs_main._setup_only_mode` |
| Full-run wiring & auto-open rules | `lvs_main._run_full` |

### Overlay (HUD) & tray
| Change | Location |
|--------|----------|
| Score weights / labels / rank colours | `lvs_overlay_gui` `WEIGHTS`, `SCORE_LABELS`, `RANK_COLORS_RGB` |
| HUD geometry / border QSS | `LVSOverlay._apply_border_qss`, `HUD_WIDTH`, `TOP_MARGIN`, `OPACITY` |
| Open cascade tiers | `LVSOverlay._open_jpeg_cascade` / `_open_raw` |
| Burst celebration (paint-only flash) | `trigger_burst_celebration`, `_flash_paint_on/off` |
| Fade toast (RAW "Opening…") | `show_fade_toast`, `QGraphicsOpacityEffect` + `QPropertyAnimation` |
| Caption anchoring / HTML rendering | `_render_caption_html`, `CAP_BUDGET`, `CAP_LEAD` |
| Tray menu items / lifetime | `LVSTrayIcon._build_menu`, `__init__` (**parented to overlay so it isn't GC'd**) |
| Tray retry (system tray not ready) | `LVSTrayIcon._retry_show` |

### Tasker GUI
| Change | Location |
|--------|-------|
| Palette / stylesheet | `lvs_tasker_gui` colour consts + `_stylesheet()` |
| Path rows + colour states | `PathRow`, `LVSTaskerWindow._restyle_paths` |
| Auto-detect from workspace | `_auto_detect_from_workspace` |
| Viewer picker (FastStone/DigiKam) | `ViewerBox`, `_on_viewer_pick` |
| Star buckets (clickable counts) | `StarBucket`, `clicked` → `_open_bucket_folder` |
| Mode A / Mode B execution | `_run_normal_execute` / `_run_paste_execute` + the QThread workers |
| Inline RAW copy-back toggle | `PathRow` (checkable `QPushButton`), `#RawCopyToggle` stylesheet |
| Paste box autosize | `_autosize_paste`, `_lines_to_px` |
| Log reveal animation | `_reveal_log` (`QPropertyAnimation`) |
| Path persistence | `_save_paths`, `_load_saved_paths`, `_reload_paths_from_dm` |
| Autocull mockup | `AutocullPanel`, `_open_autocull`, `_close_autocull` |
| DigiKam probe worker | `DigikamProbeWorker`, `_run_probe`, `_probe_done` |
| DigiKam write-back | `_writeback_to_digikam_if_active`, `_open_digikam_if_active_viewer` |

### Ingest pipeline (CLI core)
| Change | Location |
|--------|-------|
| Phase orchestration | `LVSTasker.execute` and `phaseN_*` |
| Paste parser patterns | `lvs_tasker.parse_paste_block`, `_extract_rating_from_dirpath` |
| EXIF rating write/verify | `exiftool_set_rating`, `find_exiftool` |
| edit.db schema/writer | `SQL_CREATE_EDIT_DB`, `init_edit_db`, `populate_edit_db` |
| task.md format | `write_task_md` (GUI/paste path) and `LVSTasker.phase7_report` (CLI path) — **two writers, keep aligned** |
| copy-while-rating | `should_offer_copy_raws`, `copy_raw_to_local`, `is_external_dir` |
| RAW resolution gate (<80% abort) | `LVSTasker.execute` post-phase3 |
| Non-interactive copy default | `LVSTasker.execute` pre-phase4 |
| Hesitancy caption clean | `_hesitancy_clean`, `HesitancyParser.clean_caption` |

### FastStone settings patcher
| Change | Location |
|--------|-------|
| Delphi TPF0 parse/serialize | `lvs_backend.DelphiTPF0Settings` (`load`/`save`/`set_property_string`) |
| CLI entry `--patch-fsdb` | `patch_fsdb_cli`; key `FSDB_TARGET_KEY="CopyMove19StringText"` |
| Registry favourites | `lvs_faststone._write_registry_favourites` and AHK `RunSetup` |

### AHK macro
| Change | Location |
|--------|-------|
| Cull hotkeys 1..5 | `CopyToSlot` under the `#HotIf WinActive(... FastStone ...)` block |
| Copy verification window | `VERIFY_TIMEOUT`/`VERIFY_TICK`/`VERIFY_GRACE` + the poll loop |
| IPC JSON to the pipe | `BuildCopiedJson`/`BuildFailedJson`/`NotifyPipe`/`JsonEscape` |
| Setup wizard | `RunSetup`, `PatchFSSettings` |

### Hesitancy / caption cleaning
| Change | Location |
|--------|-------|
| Conservative caption rules | `hesitancy_parser.py` `_parse_hesitancy_text`, `_Rule`, `_apply_rules` |
| Phrase score modifiers | `PHRASE_SCORE_RULES` (DoF boosters, focus penalties, quality tells) |
| Public facade | `HesitancyParser` (`clean_caption`, `apply_score_modifiers`, `phrase_directions`, `display_phrases`) |
| Module singleton | `get_parser` |

### Configuration (environment variables)
| Var | Used by | Effect |
|-----|---------|--------|
| `LVS_ALLOW_SAMPLE_DB=1` | `ensure_sample_database` | Opt-in to the old demo ingest.db (testing only; default = never create) |
| `LOCALAPPDATA` | `patch_fsdb_cli` | Locates `FastStone/FSIV/FSSettings.db` |
| `LVS_HESITANCY_PATH` | `HesitancyParser._resolve_path` | Override path to `hesitancy.txt` |

---

## 4. Non-obvious behaviours / gotchas (read before editing)

1. **LVS never creates `ingest.db`.** It is produced upstream. The only LVS write to ingest.db is an *insert into the `tasker_ratings` bridge table* inside paste mode, and only if the file already exists. `ensure_sample_database` is **disabled** unless `LVS_ALLOW_SAMPLE_DB=1`.
2. **The open cascade is intentional** (file→folder→file). T2 reveals in Explorer rather than opening — by design.
3. **Hash beats mtime for duplicate RAWs.** Counter rollover means two RAWs can share a name. `source_hash` is authoritative; mtime/capture_time is only a *no-hash* fallback. Resolvers refuse to guess when a hash is given but nothing matches and >1 candidate exists.
4. **Two ImageMagick-free worlds.** `task.md` is written in two places (`write_task_md` vs `phase7_report`) with slightly different formatting — keep them aligned if you change the manifest.
5. **`file_sha256` exists twice** (backend + tasker). Same algorithm; change both.
6. **The tray icon must stay parented** (to the overlay). An unparented `QSystemTrayIcon` held only by a local variable is GC'd and never appears.
7. **The Tasker live timer touches `time`/`Path`** every 2 s (`_tick_status`, `_do_copy_back`). Missing module-level imports here crash the running window silently.
8. **Detached AHK process** is started by `AHKManager` so it dies with LVS (`CREATE_NEW_PROCESS_GROUP`, `atexit` stop).
9. **`.bat` is pure ASCII on purpose** — UTF-8 inside parenthesised `IF` blocks corrupts cmd.exe's cached block.
10. **`edit.db` (output handoff) ≠ `ingest.db` (input).** Creating `edit.db` is fine and expected; creating `ingest.db` is forbidden.
11. **`find_in_folder_by_stem` is the FAST no-DB cascade matcher.** Files copied into `selectN/` carry an ingest hash suffix, so exact-name matching never hits. This method strips the hash from each candidate and compares clean stems. Collisions return the first stem match (deliberate, for 10k-name/512GB-card speed).
12. **RAW open shows a fade toast, not a confirm dialog.** JPEG opens silently; RAW opens a heavy external editor, so the overlay shows "Opening …" and fades over 3s.
13. **DigiKam write-back is opportunistic.** It runs on every successful execute, regardless of which viewer was active. It requires DigiKam to be closed (SQLite single-writer) and creates `.lvs.bak` before touching `digikam4.db`.

---

## 5. Bug-sweep FIXES applied

| # | File | Bug | Fix |
|------|------|-------------|-----|
| FIX-1 | `lvs_main.py`, `lvs_backend.py` | App created a demo `ingest.db` at startup via `ensure_sample_database`, polluting real workspaces. LVS must never write ingest.db. | Removed the startup call; `main` now just warns if absent. `ensure_sample_database` is a no-op unless `LVS_ALLOW_SAMPLE_DB=1`. |
| FIX-2 | `lvs_tasker.py` `RawIndex.resolve`; `lvs_backend.raws_copy_back`; `lvs_backend.find_raw_file` | When a preview's expected RAW hash was supplied but matched none of several same-named candidates (Nikon/Canon rollover), code **silently picked the wrong NEF** by NEF-preference/mtime/alpha. | Hash is now authoritative: hit → use; ambiguous miss (>1 candidate) → refuse and report unresolved; single mismatch → use + record note; no hash → heuristic. `find_raw_file` gained `expected_hash`; overlay "Open RAW" now passes it via `get_source_hash_for_preview`. |
| FIX-3 | `lvs_tasker_gui.py` | `_tick_status` used `time.strftime` but `time` was never imported → `NameError` thrown every 2 s by the live timer (Tasker window broken). | Added `import time`. |
| FIX-4 | `lvs_tasker_gui.py` | `_do_copy_back` used `Path(...)` (no `from pathlib import Path`) and imported a **nonexistent** module `run_real_sort_execute` for the disk-space check → copy-back UI errored. | Added `from pathlib import Path`; replaced the missing dependency with a self-contained `shutil.disk_usage` estimate. |
| FIX-5 | `lvs_overlay_gui.py` | System-tray icon never appeared ("taskbar not showing"): unparented `QSystemTrayIcon` held only by a local var was GC'd; no availability guard. | Parented the tray to the overlay (shared lifetime), added `isSystemTrayAvailable()` guard + log, parented the refresh `QTimer`. |
| FIX-6 | `lvs_backend.py` | `find_in_folder_by_base` had a **double `@staticmethod`** decorator. At runtime this produced a `staticmethod` object wrapping another `staticmethod`, causing `TypeError: 'staticmethod' object is not callable` when the overlay cascade tried to use it. | Removed the duplicate decorator. |
| FIX-7 | `lvs_backend.py` | `LVSDataManager.get_connection()` built a raw file path into a SQLite URI: `f"file:{self.db_path}?mode=ro"`. On Windows this embeds backslashes (`C:\...`) which are **invalid in RFC-3986 file URIs**; SQLite silently mis-parsed or failed on paths with spaces or special characters. | Switched to `Path(self.db_path).as_uri() + "?mode=ro"`, which correctly produces `file:///C:/...` and percent-encodes special characters. |
| FIX-8 | `lvs_overlay_gui.py` | `LVSTrayIcon.__init__` annotated `on_open_tasker` as `Optional[callable]` — `callable` is a built-in function, not a type. While `from __future__ import annotations` defers evaluation, static analysis and `typing.get_type_hints()` choke on it. | Changed to `Optional[Callable]`. |
| FIX-9 | `lvs_backend.py` | `LVSDataManager._raws_index` was typed `Optional[Dict[str, str]]` but `_build_raws_index` returns `Dict[str, List[str]]`. The mismatch meant static checkers flagged valid list operations on the dict values as errors. | Corrected the type annotation to `Optional[Dict[str, List[str]]]`. |
| FIX-10 | `lvs_tasker.py` | `write_task_md` called `_hesitancy_clean(raw_cap)` without passing `workspace_dir`, so captions were cleaned against the script directory instead of the actual workspace. `phase7_report` already passed the workspace correctly, causing inconsistent output between the two manifest writers. | Added `str(workspace)` as the second argument so both writers use the same cleaning context. |
| FIX-11 | `lvs_setup_shortcuts.ahk` | Header comment claimed version `1.0.6` while `APP_VERSION` global was already bumped to `1.0.8`. | Updated header comment to `1.0.8` for consistency. |

---

## 6. Change recipes

- **Port to a new viewer (e.g. digiKam).** Write `lvs_<viewer>.py` with a `ViewerAdapter` subclass (`is_available`, `is_running`, `start_watcher`, `configure`). Register it in `lvs_main.build_adapters`. No overlay/tasker changes needed.
- **Change how the current image filename is read.** `FastStoneWatcher.run` + `_FS_TITLE_RE` in `lvs_faststone.py` (or `DigikamWatcher` in `lvs_digikam.py`).
- **Add a score to the HUD.** Add to `WEIGHTS` + `SCORE_LABELS` (`lvs_overlay_gui`) and ensure the column exists in `previews`.
- **Change the cull → select mapping.** Folder names: `SELECT_PREFIX`/`SELECT_COUNT`/`SELECT_NAMES` in `lvs_backend.py`, mirrored in the AHK globals and the registry/FSSettings writers.
- **Make RAW matching stricter/looser.** `RawIndex.resolve` strategies and the ambiguous-miss policy; mirror any change in `raws_copy_back`.
- **Change the editing manifest.** Edit both `write_task_md` and `LVSTasker.phase7_report`.
- **Add a new hesitancy phrase.** Add to `PHRASE_SCORE_RULES` in `hesitancy_parser.py`; update `_render_caption_html` in `lvs_overlay_gui.py` if the display logic changes.

---

## 7. Quick symbol locator (grep targets)

```
launch_gate / LVSDataManager / raws_copy_back / SingleInstance
    → lvs_backend.py
get_image_data / get_batch_rank / get_folder_ratings / get_connection
    → lvs_backend.py  (DB reads)
find_raw_file / get_source_hash_for_preview / find_in_folder_by_stem
    → lvs_backend.py  (RAW + hash + cascade)
DelphiTPF0Settings / patch_fsdb_cli / free_stale_ipc_pipe
    → lvs_backend.py  (FSSettings + single-instance)
load_hud_pos / save_hud_pos / load_tasker_paths / save_tasker_paths
    → lvs_backend.py  (settings persistence)
FastStoneAdapter / FastStoneWatcher / _FS_TITLE_RE
    → lvs_faststone.py
DigikamAdapter / DigikamWatcher / DigikamCullWriter / exiftool_write_labels
    → lvs_digikam.py
LVSOverlay / LVSTrayIcon / _open_jpeg_cascade / trigger_burst_celebration
    → lvs_overlay_gui.py
show_fade_toast / _render_caption_html / CAP_BUDGET / CAP_LEAD
    → lvs_overlay_gui.py
LVSTaskerWindow / open_tasker / ViewerBox / StarBucket / AutocullPanel
    → lvs_tasker_gui.py
TaskerExecuteWorker / PasteExecuteWorker / CopyBackWorker / DigikamProbeWorker
    → lvs_tasker_gui.py
LVSTasker / RawIndex / parse_paste_block / verify_against_ingest_db
    → lvs_tasker.py
populate_edit_db / write_task_md / exiftool_set_rating / query_ingest_metadata_by_stems
    → lvs_tasker.py
HesitancyParser / PHRASE_SCORE_RULES / clean_caption / apply_score_modifiers
    → hesitancy_parser.py
CopyToSlot / NotifyPipe / RunSetup / BuildCopiedJson / BuildFailedJson
    → lvs_setup_shortcuts.ahk
```

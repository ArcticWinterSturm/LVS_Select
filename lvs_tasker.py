#!/usr/bin/env python3
"""
lvs_tasker.py — LVS Tasker (Ingest Pipeline First Touchpoint)
==============================================================

This script is the FIRST database touchpoint in the Latent Vision Studio
editing pipeline. It reads user ratings from select1-5 cull folders,
resolves each rated preview to its RAW counterpart, writes EXIF Rating
tags to BOTH the select file and the RAW file, commits everything to
ingest.db, and generates a task.md for the downstream editing agent.

Two databases exist in the pipeline:
  - ingest.db       ← THIS script writes here (ratings, file resolution, EXIF status)
  - photoedit.sqlite ← photoedit.py writes here later (edit operations, verification)

The tasker_ratings table in ingest.db is the bridge. When photoedit.py runs
overnight on 130 images, it can query this table to know the user rating and
RAW path for each file without re-discovering anything.

Workspace schema (new):
  workspace/
  ├── ingest.db
  ├── raws/                 ← RAW files (.NEF, .ARW, etc.) — NEVER modified by tasker
  ├── select1/              ← Rating 1 culled selects (JPEG/PNG with optional hash suffix)
  ├── select2/              ← Rating 2
  ├── select3/              ← Rating 3
  ├── select4/              ← Rating 4
  ├── select5/              ← Rating 5
  ├── select/               ← Combined output (clean names, no hashes)
  └── task.md               ← Generated editing manifest

Legacy compatibility:
  The script also recognizes edit1-5/ folders and falls back if select1-5
  are absent. This handles workspaces created before the schema rename.

RAW resolution:
  The script builds an in-memory index of all RAW files in raws/ at startup.
  Each rated file is matched to its RAW by extracting the numeric identifier
  from the filename (DSC01116 → 01116, IMG_5678 → 5678, etc.) and looking
  up the index. This handles Sony (DSC####), Nikon (DSC_####), Canon (IMG_####),
  Fujifilm (DSCF####), and generic naming patterns.

EXIF writing:
  exiftool is used to set the Rating tag. For RAW files this writes into
  the native manufacturer metadata block. For JPEGs it writes XMP.
  Both are verified by reading back after write.

Usage:
  python lvs_tasker.py [workspace_dir]

  If no argument, uses current working directory.
"""

import os
import sys
import re
import time
import sqlite3
import shutil
import tempfile
import subprocess
import hashlib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
import dataclasses

# =============================================================================
# Constants
# =============================================================================

# RAW file extensions we recognize (must match photoedit.py's set)
RAW_EXTS: Set[str] = {
    ".nef", ".nrw", ".arw", ".dng", ".raf",
    ".cr2", ".cr3", ".orf", ".rw2", ".pef", ".srw", ".raw"
}

# Preview/select file extensions (JPEGs and PNGs from culling)
PREVIEW_EXTS: Set[str] = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Hesitancy caption cleaner (lazy init, one instance per workspace)
_hesitancy_parsers: Dict[str, object] = {}

def _hesitancy_clean(caption: str, workspace_dir: str = "") -> str:
    """Clean a Florence-2 caption using the same rules as the overlay HUD.
    
    Looks for hesitancy.txt in workspace_dir first, then falls back to
    the script directory.  Cached per workspace path."""
    global _hesitancy_parsers
    key = workspace_dir or "__script__"
    parser = _hesitancy_parsers.get(key)
    if parser is None:
        try:
            from hesitancy_parser import HesitancyParser
            # Priority: workspace / script dir / explicit path
            paths = []
            if workspace_dir:
                paths.append(workspace_dir)
            paths.append(os.path.dirname(os.path.abspath(__file__)))
            parser = HesitancyParser(workspace_dir=paths[0])
            _hesitancy_parsers[key] = parser
        except Exception:
            parser = False
            _hesitancy_parsers[key] = False
    if parser is False or not caption:
        return caption
    try:
        return parser.clean_caption(caption)
    except Exception:
        return caption

# Regex: LVS files with 16-char hex hash suffix, e.g. DSC01116_cf479ea38d56610d.jpg
# The hash is an ingest-time fingerprint — it lets us verify file identity
# without reading the full contents. Groups: (stem)_(hash16).(ext)
RE_LVS_HASHED = re.compile(r"^(.+?)_([0-9a-fA-F]{16})(\.[a-zA-Z0-9]+)$")

# Regex: extract the numeric sequence from a camera filename.
# Handles: DSC01116, DSC_01116, _DSC01116, IMG_5678, DSCF1234, etc.
# The camera number is the PRIMARY key for matching selects to RAWs.
RE_CAMERA_NUMBER = re.compile(r"(?<!\d)(\d{3,7})(?!\d)")

# Folder names for rated selects (new schema)
RATED_FOLDER_PREFIX = "select"

# Folder names for rated selects (legacy schema, backward compat)
LEGACY_FOLDER_PREFIX = "edit"

# Terminal colors for structured logging — these are cosmetic but critical
# for agentic debugging. The agent reads stderr to understand what happened.
C_GREEN = "\033[92m"
C_BLUE = "\033[94m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

# exiftool timeout in seconds — if exiftool hangs on a corrupt file,
# we don't want to block the entire pipeline.
EXIFTOOL_TIMEOUT = 15


# =============================================================================
# Logging — structured for agentic readability
# =============================================================================

def log_ok(msg: str) -> None:
    """Success confirmation. Agent can trust these."""
    print(f"{C_GREEN}{C_BOLD}[✓]{C_RESET} {msg}")

def log_info(msg: str) -> None:
    """Informational progress. Agent reads these for pipeline state."""
    print(f"{C_BLUE}{C_BOLD}[i]{C_RESET} {msg}")

def log_warn(msg: str) -> None:
    """Non-fatal issue. Agent should report these but continue."""
    print(f"{C_YELLOW}{C_BOLD}[!]{C_RESET} {msg}")

def log_err(msg: str) -> None:
    """Fatal or serious issue. Agent should stop and report."""
    print(f"{C_RED}{C_BOLD}[✗]{C_RESET} {msg}")

def log_step(msg: str) -> None:
    """Sub-step detail. Agent can skip these unless debugging."""
    print(f"  {C_DIM}→{C_RESET} {msg}")


# =============================================================================
# File Hashing
# =============================================================================

def file_sha256(filepath: Path, chunk_size: int = 65536) -> str:
    """
    Compute SHA-256 of a file. Used for:
    1. Verifying file identity against ingest.db records
    2. Storing in tasker_ratings for downstream photoedit.py to reference
    
    Returns empty string on failure (never raises — caller checks).
    """
    h = hashlib.sha256()
    try:
        with filepath.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        log_warn(f"SHA-256 computation failed for {filepath.name}: {exc}")
        return ""


# =============================================================================
# EXIF Operations
# =============================================================================

def find_exiftool() -> Optional[str]:
    """
    Locate exiftool binary. Searches:
    0. Script directory (local fallback first)
    1. System PATH (shutil.which)
    2. Common Windows install paths
    3. Chocolatey bin directory
    
    Returns the path string or None.
    """
    # 0. Script directory
    try:
        script_dir = Path(__file__).resolve().parent
        local_exif = script_dir / "exiftool.exe"
        if local_exif.is_file():
            return str(local_exif)
    except Exception:
        pass

    # System PATH
    found = shutil.which("exiftool")
    if found:
        return found

    # Windows-specific probe paths
    if os.name == "nt":
        candidates = [
            r"C:\ProgramData\chocolatey\bin\exiftool.exe",
            r"C:\Program Files\ExifTool\exiftool.exe",
            r"C:\Program Files\exiftool\exiftool.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

    return None


def exiftool_set_rating(filepath: Path, rating: int, exiftool_bin: str) -> Tuple[bool, str]:
    """
    Write EXIF Rating tag to a file using exiftool.
    
    Works on both JPEG (writes XMP) and RAW (writes manufacturer-specific block).
    Returns (success: bool, message: str).
    
    The Rating tag is the universal standard — Lightroom, Capture One, Bridge,
    and all DAM systems read it. For RAW files it maps to the manufacturer's
    rating field (e.g., Nikon's V_tagRating, Sony's Rating).
    
    We use -overwrite_original to avoid creating .jpg_original sidecar files
    that would clutter the workspace.
    """
    cmd = [
        exiftool_bin,
        f"-Rating={rating}",
        "-overwrite_original",
        "-P",  # preserve filesystem modification date
        str(filepath),
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=EXIFTOOL_TIMEOUT,
        )
        if res.returncode == 0:
            # Verify by reading back
            verify_cmd = [exiftool_bin, "-Rating", "-b", str(filepath)]
            try:
                vres = subprocess.run(
                    verify_cmd,
                    capture_output=True, text=True,
                    timeout=EXIFTOOL_TIMEOUT,
                )
                readback = vres.stdout.strip()
                if readback == str(rating):
                    return True, f"EXIF Rating {rating}★ verified on {filepath.name}"
                else:
                    return (
                        False,
                        f"EXIF Rating write succeeded but readback mismatch: "
                        f"expected {rating}, got '{readback}' on {filepath.name}"
                    )
            except subprocess.TimeoutExpired:
                # Write succeeded but verification timed out — treat as partial success
                return True, f"EXIF Rating {rating}★ written to {filepath.name} (verify timeout)"
        else:
            return False, f"exiftool exit code {res.returncode}: {res.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, f"exiftool timed out after {EXIFTOOL_TIMEOUT}s on {filepath.name}"
    except FileNotFoundError:
        return False, f"exiftool binary not found at: {exiftool_bin}"
    except Exception as exc:
        return False, f"exiftool exception: {exc}"


# =============================================================================
# RAW Resolution
# =============================================================================



# =============================================================================
# Paste Parser — Parse dir/ls/Get-ChildItem output into rated file lists
# =============================================================================

@dataclasses.dataclass
class ParsedPasteItem:
    """One file extracted from pasted terminal output with its rating."""
    filename: str
    stem: str
    rating: int
    source_dir: str          # The directory header this file was under
    source_path: Optional[str] = None  # Full path if extractable
    preview_path: Optional[Path] = None  # Resolved later by GUI
    raw_path: Optional[Path] = None      # Resolved later by GUI
    duplicate: bool = False

    # All file extensions we consider valid in paste input
    # This is broader than RAW_EXTS because the user might paste
    # JPEG, TIFF, PSD, or any other file type from their sort
    VALID_PASTE_EXTS = {
        ".nef", ".nrw", ".arw", ".dng", ".raf",
        ".cr2", ".cr3", ".orf", ".rw2", ".pef", ".srw", ".raw",
        ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd",
        ".bmp", ".webp", ".heic", ".avif",
    }


def _is_valid_paste_filename(token: str) -> bool:
    """Check if a token looks like a camera file (has a known extension)."""
    lower = token.lower().strip()
    for ext in ParsedPasteItem.VALID_PASTE_EXTS:
        if lower.endswith(ext):
            return True
    return False


def _extract_rating_from_dirpath(dirpath: str) -> Optional[int]:
    """
    Extract rating number from a directory path/header.
    Handles:
      C:/Users/User/Desktop/edit2     -> 2
      C:/Users/User/Desktop/select3   -> 3
      /home/user/edit5                 -> 5
      /mnt/photos/sort_select4         -> 4
      Directory of C:/.../edit2        -> 2
    """
    # Try editN or selectN patterns
    for prefix in ("edit", "select", "sort", "rating", "rate", "star"):
        # Match prefix followed by a digit at word boundary / path separator
        pat = re.compile(rf'{prefix}(\d)(?=[/\\]|$)', re.IGNORECASE)
        m = pat.search(dirpath)
        if m:
            r = int(m.group(1))
            if 1 <= r <= 5:
                return r
    return None


def parse_paste_block(text: str) -> List[ParsedPasteItem]:
    """
    Parse messy terminal output into a list of rated file items.

    Handles:
      - Windows `dir` output with volume labels, dates, sizes
      - Linux `ls -la` output with permissions, dates
      - PowerShell `Get-ChildItem` output
      - Bare file lists with occasional directory headers
      - Mixed ARW/NEF/JPG/etc filenames

    The algorithm:
      1. Detect directory headers (lines containing 'editN', 'selectN', path patterns)
      2. Track the current rating from the most recent directory header
      3. Extract filenames from lines that look like file entries
      4. Build ParsedPasteItem for each found file
    """
    items: List[ParsedPasteItem] = []
    current_rating = 0  # 0 = unknown, waiting for a directory header
    current_dir = ""

    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # --- Check if this line is a directory header ---
        # Windows: "Directory of C:\...\edit2"
        # Windows: "C:\Users\User>dir C:\...\edit3"
        # Linux:   "/home/user/edit4:"
        # Linux:   "total 128" or block headers are ignored
        # Generic: any path containing editN/selectN

        rating_from_line = None
        dir_from_line = ""

        # Pattern 1: "dir PATH" or "dir PATH\" (Windows command)
        m = re.match(r'(?:C:)?[^\s>]*>\s*dir\s+(.+)', stripped, re.IGNORECASE)
        if m:
            dir_from_line = m.group(1).strip().rstrip('\\')
            rating_from_line = _extract_rating_from_dirpath(dir_from_line)

        # Pattern 2: "Directory of PATH" (Windows dir output header)
        if not rating_from_line:
            m = re.match(r'Directory\s+of\s+(.+)', stripped, re.IGNORECASE)
            if m:
                dir_from_line = m.group(1).strip()
                rating_from_line = _extract_rating_from_dirpath(dir_from_line)

        # Pattern 3: Path ending with editN/selectN (Linux ls block header or cd)
        if not rating_from_line:
            m = re.match(r'(.*/(?:edit|select|sort|rating)\d+)\s*:?', stripped, re.IGNORECASE)
            if m:
                dir_from_line = m.group(1).strip().rstrip(':')
                rating_from_line = _extract_rating_from_dirpath(dir_from_line)

        # Pattern 4: Bare "editN/" or "selectN/" as a header
        if not rating_from_line:
            m = re.match(r'^(?:edit|select|sort|rating)(\d)/?\s*$', stripped, re.IGNORECASE)
            if m:
                r = int(m.group(1))
                if 1 <= r <= 5:
                    rating_from_line = r
                    dir_from_line = stripped

        # Pattern 5: PowerShell "Directory: PATH"
        if not rating_from_line:
            m = re.match(r'Directory:\s*(.+)', stripped, re.IGNORECASE)
            if m:
                dir_from_line = m.group(1).strip()
                rating_from_line = _extract_rating_from_dirpath(dir_from_line)

        if rating_from_line:
            current_rating = rating_from_line
            current_dir = dir_from_line
            continue

        # --- Skip noise lines ---
        # Volume labels, serial numbers, file counts, "total N", etc.
        if re.match(r'^\s*(Volume|Serial|Total|File|Dir|bytes free|Mode|LastWriteTime)', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\s*\d+\s+File\(s\)', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\s*\d+\s+Dir\(s\)', stripped, re.IGNORECASE):
            continue
        if stripped.startswith('<DIR>'):
            continue
        if re.match(r'^\s*$', stripped):
            continue

        # --- Try to extract a filename from this line ---
        # Strategy: find the last token that looks like a filename with a valid extension
        tokens = stripped.split()
        filename = None

        for token in reversed(tokens):
            # Clean up token (remove commas, trailing punctuation)
            clean = token.strip(',;').strip()
            if _is_valid_paste_filename(clean):
                filename = clean
                break

        if not filename and len(tokens) == 1:
            # Single token line — might be just a filename
            if _is_valid_paste_filename(tokens[0].strip(',;')):
                filename = tokens[0].strip(',;')

        if filename and current_rating >= 1:
            # Build the item — paste input is from `dir`/`ls`, filenames are CLEAN
            # (no hash suffixes). Keep the full stem so Nikon DSC_7957 stays intact.
            stem = Path(filename).stem

            # Try to construct a full source path
            source_path = None
            if current_dir:
                candidate = Path(current_dir) / filename
                source_path = str(candidate)

            items.append(ParsedPasteItem(
                filename=filename,
                stem=stem,
                rating=current_rating,
                source_dir=current_dir,
                source_path=source_path,
            ))

    return items

class RawIndex:
    """
    In-memory index of all RAW files in the workspace's raws/ directory.
    
    Built once at startup. Provides O(1) lookup by:
    - Normalized stem (e.g., "DSC01116")
    - Camera number (e.g., "01116")
    
    The index stores multiple resolution strategies because camera naming
    conventions vary across manufacturers:
    - Sony:    DSC01116.ARW  → stem "DSC01116", number "01116"
    - Nikon:   DSC_0116.NEF  → stem "DSC_0116", number "0116"
    - Canon:   IMG_5678.CR2  → stem "IMG_5678", number "5678"
    - Fuji:    DSCF1234.RAF  → stem "DSCF1234", number "1234"
    - Generic: P1010989.RW2  → stem "P1010989", number "1010989"
    
    Resolution priority:
    1. Exact stem match (case-insensitive)
    2. Camera number match (extracted 3-7 digit sequence)
    3. Stem contains the query (substring match — last resort)
    
    When multiple RAWs match (e.g., same number different prefix), we prefer
    NEF over ARW (Nikon is the primary camera for this workflow), then shortest
    filename, then alphabetical.
    """

    def __init__(self, raws_dir: Path):
        self.raws_dir = raws_dir
        # stem_lower → [Path, ...]
        self.by_stem: Dict[str, List[Path]] = {}
        # number_string → [Path, ...]
        self.by_number: Dict[str, List[Path]] = {}
        # All discovered RAW paths
        self.all_raws: List[Path] = []
        # Extension preference order (lower = preferred)
        self.ext_priority = {".nef": 0, ".nrw": 1, ".arw": 2, ".dng": 3}

        self._build()

    def _build(self) -> None:
        """Scan raws/ directory and populate indices."""
        if not self.raws_dir.is_dir():
            log_warn(f"RAW directory does not exist: {self.raws_dir}")
            return

        # Use os.walk instead of rglob to gracefully handle PermissionError on external drives
        for root, dirs, files in os.walk(str(self.raws_dir)):
            for filename in files:
                p = Path(root) / filename
                if p.suffix.lower() not in RAW_EXTS:
                    continue

                self.all_raws.append(p)

                # Index by normalized stem (uppercase, no spaces)
                stem = p.stem.upper().replace(" ", "")
                self.by_stem.setdefault(stem, []).append(p)

                # Index by extracted camera number
                m = RE_CAMERA_NUMBER.search(p.stem)
                if m:
                    num = m.group(1)
                    self.by_number.setdefault(num, []).append(p)

        for k in self.by_stem:
            self.by_stem[k].sort()
        for k in self.by_number:
            self.by_number[k].sort()

        log_info(f"RAW index built: {len(self.all_raws)} files from {self.raws_dir}")

    def resolve(self, query: str, expected_hash: Optional[str] = None) -> Optional[Path]:
        """
        Resolve a query string (filename, stem, or number) to a RAW file path.

        Hash semantics (the part that was silently wrong):
          - `expected_hash` is the full SHA-256 of the *source RAW* recorded in
            ingest.db (`files.source_hash`).  The select JPEG carries the first
            16 hex chars of this in its filename.
          - When a hash is supplied it is AUTHORITATIVE.  If exactly one
            candidate on disk hashes to it, that is the answer.
          - The previous code, when the hash matched NOTHING, *silently fell
            back* to `_best_match` (NEF-preference + alphabetical).  With
            Nikon/Canon counter rollover (DSC_5000.NEF existing in both
            184NIKON/ and 185NIKON/) that returned the WRONG NEF for the hash.
            We now refuse to guess in that ambiguous case.

        Returns the resolved Path, or None when it cannot be resolved
        *confidently* (caller logs it as "missing" rather than mis-copying).
        """
        query_stem = Path(query).stem if any(
            query.lower().endswith(e) for e in (RAW_EXTS | PREVIEW_EXTS)
        ) else query
        query_stem = query_stem.strip().upper().replace(" ", "")

        candidates = []
        # Strategy 1: Exact stem match
        if query_stem in self.by_stem:
            candidates.extend(self.by_stem[query_stem])

        # Strategy 2: Camera number match
        m = RE_CAMERA_NUMBER.search(query)
        if m:
            num = m.group(1)
            if num in self.by_number:
                for p in self.by_number[num]:
                    if p not in candidates:
                        candidates.append(p)

        # Strategy 3: Substring match (loosest — only ever a last resort)
        if not candidates:
            for stem, paths in self.by_stem.items():
                if query_stem in stem or stem in query_stem:
                    for p in paths:
                        if p not in candidates:
                            candidates.append(p)

        if not candidates:
            return None

        # ---- Hash is authoritative when supplied ----
        if expected_hash:
            hash_hits = [p for p in candidates if file_sha256(p) == expected_hash]
            if hash_hits:
                return hash_hits[0]
            # Hash supplied but nothing matched.
            #  * Single candidate name on disk → the file is the same name but a
            #    different binary (re-export / different copy).  Returning it is
            #    reasonable but flagged.
            #  * Multiple candidates (rollover duplicates) → we genuinely cannot
            #    tell which is correct.  DO NOT guess; report as unresolved so
            #    the wrong RAW is never copied/rated.
            if len(candidates) == 1:
                log_warn(
                    f"  RAW '{candidates[0].name}' name-matches '{query}' but its "
                    f"SHA-256 does not match ingest.db — using it (only candidate)."
                )
                return candidates[0]
            log_warn(
                f"  Ambiguous RAW for '{query}': {len(candidates)} same-named "
                f"candidates and NONE match the expected hash "
                f"{expected_hash[:16]}… — refusing to guess (left unresolved)."
            )
            return None

        # No hash available at all → fall back to deterministic best-match.
        if len(candidates) > 1:
            log_warn(
                f"  No expected hash for '{query}' and {len(candidates)} "
                f"candidates exist — picking best-match heuristically."
            )
        return self._best_match(candidates)

    def _best_match(self, paths: List[Path]) -> Path:
        """
        From multiple candidate RAW paths, pick the best one.
        Priority: NEF > NRW > ARW > DNG, then shortest name, then alphabetical.
        """
        return sorted(
            paths,
            key=lambda p: (
                self.ext_priority.get(p.suffix.lower(), 9),
                len(p.name),
                str(p),
            )
        )[0]


# =============================================================================
# Database Operations
# =============================================================================

# SQL for creating the tasker_ratings table in ingest.db.
# This is the BRIDGE table between the tasker (ratings + file resolution)
# and the downstream photoedit.py pipeline (edit operations).
#
# Design decisions:
# - raw_path is nullable because RAW resolution may fail
# - exif_select_ok and exif_raw_ok are separate booleans because partial
#   success is possible (JPEG written but RAW failed)
# - ingest_file_id links to the existing files table if a match was found
# - raw_sha256 is computed and stored for downstream verification
# - user_rating has a CHECK constraint for data integrity

SQL_CREATE_TASKER_RATINGS = """
CREATE TABLE IF NOT EXISTS tasker_ratings (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    select_path     TEXT NOT NULL,
    select_stem     TEXT NOT NULL,
    select_hash_prefix TEXT,
    select_clean_name TEXT NOT NULL,
    select_final_path TEXT,
    raw_path        TEXT,
    raw_resolved    INTEGER DEFAULT 0,
    raw_ext         TEXT,
    raw_sha256      TEXT,
    user_rating     INTEGER NOT NULL CHECK(user_rating BETWEEN 1 AND 5),
    exif_select_ok  INTEGER DEFAULT 0,
    exif_raw_ok     INTEGER DEFAULT 0,
    db_linked       INTEGER DEFAULT 0,
    ingest_file_id  INTEGER,
    notes           TEXT
);
"""

SQL_CREATE_TASKER_RUN = """
CREATE TABLE IF NOT EXISTS tasker_run (
    id          INTEGER PRIMARY KEY,
    run_at      TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    total_files INTEGER DEFAULT 0,
    total_raws  INTEGER DEFAULT 0,
    exif_ok     INTEGER DEFAULT 0,
    exif_fail   INTEGER DEFAULT 0,
    db_ok       INTEGER DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'started'
);
"""


def open_db_copy(db_path: Path) -> Tuple[sqlite3.Connection, Path]:
    """
    Open a TEMPORARY COPY of ingest.db for safe read operations.
    
    Why copy? ingest.db may be locked by the main LVS process running
    concurrently. SQLite only allows one writer. By copying to a temp
    directory, we can read freely without lock contention.
    
    The caller is responsible for cleaning up the temp directory.
    Returns (connection, temp_dir_path).
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="lvs_tasker_"))
    temp_db = temp_dir / db_path.name
    shutil.copy2(db_path, temp_db)

    # Copy WAL and SHM sidecar files if they exist (SQLite WAL mode artifacts)
    for ext in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + ext)
        if sidecar.exists():
            try:
                shutil.copy2(sidecar, temp_dir / (temp_db.name + ext))
            except Exception:
                pass  # WAL files are ephemeral — failure is non-critical

    conn = sqlite3.connect(str(temp_db))
    conn.row_factory = sqlite3.Row
    return conn, temp_dir


def init_tasker_tables(db_path: Path) -> None:
    """
    Create tasker tables in ingest.db if they don't exist.
    
    This is a WRITE operation on the actual ingest.db (not a copy).
    It runs before any other DB operations. If the tables already exist
    (from a previous tasker run), this is a no-op due to IF NOT EXISTS.
    
    Uses execute() with autocommit since we're only creating tables.
    """
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SQL_CREATE_TASKER_RATINGS + SQL_CREATE_TASKER_RUN)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# File Parsing
# =============================================================================

def parse_lvs_filename(filename: str) -> Dict[str, str]:
    """
    Parse an LVS filename into its components.
    
    LVS filenames may have a 16-character hex hash suffix appended during
    ingest. This suffix is a truncated SHA-256 of the source file and serves
    as a fingerprint for identity verification.
    
    Examples:
        DSC01116_cf479ea38d56610d.jpg → stem=DSC01116, hash_prefix=cf479ea38d56610d, ext=.jpg
        DSC01116.jpg                   → stem=DSC01116, hash_prefix="", ext=.jpg
        IMG_5678_aabbccdd11223344.png  → stem=IMG_5678, hash_prefix=aabbccdd11223344, ext=.png
    
    Returns dict with keys: stem, hash_prefix, ext, clean_name, original
    """
    m = RE_LVS_HASHED.match(filename)
    if m:
        stem = m.group(1)
        hash_prefix = m.group(2)
        ext = m.group(3)
        clean_name = f"{stem}{ext}"
    else:
        stem = Path(filename).stem
        hash_prefix = ""
        ext = Path(filename).suffix
        clean_name = filename

    return {
        "stem": stem,
        "hash_prefix": hash_prefix,
        "ext": ext,
        "clean_name": clean_name,
        "original": filename,
    }


def extract_camera_number(filename: str) -> Optional[str]:
    """
    Extract the camera-assigned sequence number from a filename.
    
    This is the PRIMARY key for matching select JPEGs to RAW files.
    Different cameras embed the number at different positions:
    
    DSC01116 → 01116    (Sony A7 series)
    DSC_0116 → 0116     (Nikon Z30, Nikon Zf)
    _DSC1234 → 1234     (Nikon alternate)
    IMG_5678 → 5678     (Canon EOS)
    DSCF1234 → 1234     (Fujifilm X-series)
    P1010989 → 1010989  (Panasonic Lumix)
    
    Returns the number string or None if no 3-7 digit sequence found.
    We require at least 3 digits to avoid false matches on 2-digit suffixes.
    """
    m = RE_CAMERA_NUMBER.search(filename)
    return m.group(1) if m else None


# =============================================================================
# Hash Verification
# =============================================================================

def verify_against_ingest_db(
    items: List[dict],
    db_path: Path,
    decision_callback: Optional[Callable[[str, list], bool]] = None,
) -> List[dict]:
    """
    Verify file identity against ingest.db records.
    
    This is a read-only operation on a COPY of the database (see open_db_copy).
    It checks two things:
    1. The hash prefix in the filename matches the source_hash stored in DB
    2. The actual file contents match the preview_hash stored in DB
    
    Mismatches are logged as warnings but don't block processing by default.
    The user can choose to skip mismatched files interactively.
    
    WHY WE VERIFY: The LVS ingest pipeline generates preview JPEGs with hash
    suffixes. If a file was corrupted during transfer or a filename collision
    occurred, the hashes won't match. Catching this before editing prevents
    wasting GPU time on wrong files.
    """
    if not db_path.exists():
        log_warn("ingest.db not found — skipping hash verification")
        return items

    conn, temp_dir = open_db_copy(db_path)
    verified = []

    try:
        for item in items:
            stem = item["stem"]
            prefix = item["hash_prefix"]
            filepath = item["original_path"]

            # Query the files and previews tables.
            # When a hash prefix is present, use it to DISAMBIGUATE same-stem
            # files that came from different camera rollover folders (e.g.
            # 184NIKON/DSC08625 and 185NIKON/DSC08625). Without this, the
            # LIMIT 1 returns whichever row the DB sees first, and the second
            # file always appears to mismatch even though it is correct.
            row = None
            try:
                if prefix:
                    row = conn.execute(
                        "SELECT f.source_hash, p.preview_hash "
                        "FROM files f "
                        "LEFT JOIN previews p ON f.file_id = p.file_id "
                        "WHERE f.file_name LIKE ? AND f.source_hash LIKE ? "
                        "LIMIT 1",
                        (f"{stem}.%", f"{prefix}%"),
                    ).fetchone()
                # Fallback: no hash prefix — match by stem only (older files).
                if not row:
                    row = conn.execute(
                        "SELECT f.source_hash, p.preview_hash "
                        "FROM files f "
                        "LEFT JOIN previews p ON f.file_id = p.file_id "
                        "WHERE f.file_name LIKE ? LIMIT 1",
                        (f"{stem}.%",),
                    ).fetchone()
            except sqlite3.OperationalError:
                verified.append(item)
                continue

            if not row:
                # No DB record — can't verify, proceed anyway
                verified.append(item)
                continue

            db_source_hash = row["source_hash"] or ""
            db_preview_hash = row["preview_hash"] or ""
            mismatches = []

            prefix_matched = False

            # Check 1: Filename hash prefix vs DB source hash
            if prefix and db_source_hash:
                if db_source_hash.startswith(prefix):
                    prefix_matched = True
                else:
                    mismatches.append(
                        f"Filename hash '{prefix}' ≠ DB source_hash "
                        f"'{db_source_hash[:16]}...'"
                    )

            # Check 2: File contents vs DB preview hash
            # ONLY if the prefix didn't definitively prove identity.
            # If the filename suffix matches the RAW hash, we trust it even if 
            # the JPEG body was modified (e.g. rotated/rated in FastStone).
            if db_preview_hash and not prefix_matched:
                computed = file_sha256(filepath)
                if computed and computed != db_preview_hash:
                    mismatches.append(
                        f"File SHA-256 '{computed[:16]}...' ≠ DB preview_hash "
                        f"'{db_preview_hash[:16]}...'"
                    )

            if mismatches:
                print(f"\n{C_RED}{C_BOLD}[WARNING] SHA mismatch: {item['original_name']}{C_RESET}")
                for reason in mismatches:
                    print(f"  {C_YELLOW}→ {reason}{C_RESET}")

                # Use the decision_callback if provided (GUI mode), otherwise
                # fall back to interactive input() (CLI mode). When neither is
                # available (non-interactive pipe), default to SKIP — we never
                # silently accept a hash mismatch.
                decision = False
                if decision_callback is not None:
                    decision = decision_callback(item['original_name'], mismatches)
                elif sys.stdin.isatty():
                    try:
                        ans = input("Proceed with this file? (y/N): ").strip().lower()
                        decision = ans in ("y", "yes")
                    except (KeyboardInterrupt, EOFError):
                        decision = False
                else:
                    log_warn(
                        "Non-interactive run — auto-skipping mismatched file. "
                        "Use --force-mismatch to accept mismatches."
                    )
                    decision = False

                if decision:
                    verified.append(item)
                else:
                    log_warn(f"Skipping {item['original_name']} due to hash mismatch")
            else:
                verified.append(item)

    finally:
        conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return verified


# =============================================================================
# Query Ingest DB Metadata (for task.md scoring)
# =============================================================================

def query_ingest_metadata(
    items: List[dict],
    db_path: Path,
) -> Dict[str, dict]:
    """
    Query aesthetic scores and captions from ingest.db for task.md generation.
    
    The ingest pipeline may have already computed:
    - score_overall, score_quality, score_composition, score_lighting
    - score_color, score_dof, score_content
    - caption (from Florence-2 or similar)
    
    These are used in task.md to give the editing agent context about each image.
    Falls back to "Pending" if no data found.
    
    Returns dict mapping clean_name → metadata dict.
    """
    if not db_path.exists():
        return {}

    conn, temp_dir = open_db_copy(db_path)
    results = {}

    try:
        for item in items:
            stem = item["stem"]
            prefix = item["hash_prefix"]
            row = None

            # Strategy 1: Match by hash prefix (most reliable)
            if prefix:
                try:
                    row = conn.execute(
                        "SELECT p.*, f.file_name, f.file_path "
                        "FROM previews p "
                        "JOIN files f ON p.file_id = f.file_id "
                        "WHERE p.source_hash LIKE ? LIMIT 1",
                        (f"{prefix}%",),
                    ).fetchone()
                except sqlite3.OperationalError:
                    pass

            # Strategy 2: Match by filename stem
            if not row and stem:
                try:
                    row = conn.execute(
                        "SELECT p.*, f.file_name, f.file_path "
                        "FROM previews p "
                        "JOIN files f ON p.file_id = f.file_id "
                        "WHERE f.file_name LIKE ? LIMIT 1",
                        (f"{stem}.%",),
                    ).fetchone()
                except sqlite3.OperationalError:
                    pass

            if row:
                results[item["clean_name"]] = dict(row)

    except Exception as exc:
        log_warn(f"Metadata query failed: {exc}")
    finally:
        conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return results


# =============================================================================
# Ingest DB Metadata Lookup by Stem  (works for paste mode & normal mode)
# =============================================================================

def query_ingest_metadata_by_stems(
    stems: List[str],
    db_path: Path,
) -> Dict[str, dict]:
    """
    Look up aesthetic scores and Florence-2 captions in ingest.db by file STEM.

    Returns dict mapping stem.upper() -> {score_overall, score_quality, ...,
    caption} for whatever data is present. Missing entries simply absent.
    The ingest pipeline may not have scored/captioned everything yet; we take
    a one-shot snapshot of whatever is in the WAL/main db at call time.

    Strategy: read from a TEMP COPY so we never block the live ingest writer.
    Match by:
        f.file_name LIKE 'STEM.%'  (extension-insensitive match against RAW name)
    """
    out: Dict[str, dict] = {}
    if not db_path.exists() or not stems:
        return out

    try:
        conn, temp_dir = open_db_copy(db_path)
    except Exception as exc:
        log_warn(f"Could not snapshot ingest.db for metadata lookup: {exc}")
        return out

    try:
        for stem in stems:
            try:
                row = conn.execute(
                    "SELECT p.score_overall, p.score_quality, p.score_composition, "
                    "       p.score_lighting, p.score_color, p.score_dof, "
                    "       p.score_content, p.caption, f.file_name "
                    "FROM previews p "
                    "JOIN files f ON p.file_id = f.file_id "
                    "WHERE f.file_name LIKE ? COLLATE NOCASE "
                    "LIMIT 1",
                    (f"{stem}.%",),
                ).fetchone()
            except sqlite3.OperationalError:
                # Schema differs (older ingest.db etc.); skip silently
                row = None

            if row:
                out[stem.upper()] = dict(row)
    finally:
        try:
            conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return out


# =============================================================================
# task.md Writer  (shared by paste mode and normal mode)
# =============================================================================

def write_task_md(
    items: List[dict],
    meta_cache: Dict[str, dict],
    md_path: Path,
    workspace: Path,
    raws_dir: Optional[Path] = None,
) -> bool:
    """
    Write task.md — the editing manifest the downstream agent consumes.

    `items` is a list of dicts with at minimum:
        - rating:      int 1..5
        - clean_name:  e.g. "DSC03997.jpg" (just the JPEG name in select/)
        - stem:        e.g. "DSC03997" or "DSC_7957"
    Optional:
        - raw_path:    Path to RAW file (str/Path) if resolved
        - original_name: pre-cleanup filename if known

    `meta_cache` maps stem.upper() -> {score_*, caption} from query_ingest_metadata_by_stems.
    Missing scores/captions render as "Pending" (matches old format).

    Output mirrors the old `lvs_tasker_old` format exactly: one ★N section
    per rating (5 first), with Original | Clean | 7 scores | caption columns.

    Returns True on success.
    """
    md_content = "# 📷 LVS Master Agentic Edit Task\n\n"
    md_content += f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    md_content += f"Workspace: `{workspace}`\n"
    if raws_dir:
        md_content += f"RAWs: `{raws_dir}`\n"
    md_content += f"Total culled: {len(items)}\n\n"

    fmt = lambda x: f"{x:+.2f}" if isinstance(x, (int, float)) else "Pending"

    for rating in range(5, 0, -1):
        rating_items = [x for x in items if int(x.get("rating", 0)) == rating]
        if not rating_items:
            continue

        md_content += f"---\n\n"
        md_content += f"## 🌟 Rating {rating}★ Selection ({len(rating_items)} Photo(s))\n\n"
        md_content += (
            "| Original File | Clean Filename | Overall | Quality | Comp | "
            "Light | Color | DoF | Content | Florence-2 Caption |\n"
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |\n"
        )
        for it in sorted(rating_items, key=lambda x: x.get("clean_name", "")):
            meta = meta_cache.get(it.get("stem", "").upper(), {})
            original = it.get("original_name") or it.get("clean_name", "—")
            clean = it.get("clean_name", "—")
            raw_cap = meta.get('caption') or ''
            clean_cap = _hesitancy_clean(raw_cap, str(workspace)) if raw_cap else 'Pending'
            md_content += (
                f"| {original} | {clean} | "
                f"{fmt(meta.get('score_overall'))} | "
                f"{fmt(meta.get('score_quality'))} | "
                f"{fmt(meta.get('score_composition'))} | "
                f"{fmt(meta.get('score_lighting'))} | "
                f"{fmt(meta.get('score_color'))} | "
                f"{fmt(meta.get('score_dof'))} | "
                f"{fmt(meta.get('score_content'))} | "
                f"{clean_cap} |\n"
            )
        md_content += "\n"

    md_content += "---\n\n## 🛠 Suggested Editing Guidelines\n\n"
    md_content += (
        "- **High Rating (4★ – 5★):** Target absolute visual excellence. "
        "Recover highlights/shadows, match white balance to camera intent, "
        "and maintain full dynamic range.\n"
        "- **Medium Rating (2★ – 3★):** Basic corrections needed. Stabilize "
        "lighting, eliminate color casts, and enhance sharpness or "
        "depth-of-field separation.\n"
        "- **Low Rating (1★):** Basic grading and verification. Ensure clean "
        "output rendering.\n\n"
        f"Report Generated At: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    try:
        md_path.write_text(md_content, encoding="utf-8")
        log_ok(f"task.md written: {md_path}  ({len(items)} entries)")
        return True
    except Exception as exc:
        log_err(f"Failed to write task.md: {exc}")
        return False


# =============================================================================
# edit.db — NEW database created per tasker run (NOT ingest.db)
# =============================================================================
#
# This is a FRESH database with ONLY the culled photos. It is the handoff
# to the downstream agentic editor. The agent populates scores/captions/edits
# itself; the tasker writes ratings + names only.
#
# Deliberately:
#   - NO sha256
#   - NO hdd_diagnostics / drive_info / smart deltas
#   - NO file integrity columns
#   - NO ingest tables (no sessions, files, previews)
#
# Just the picks. id is the local PK, unrelated to ingest.db.file_id.

SQL_CREATE_EDIT_DB = """
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
"""


def init_edit_db(db_path: Path) -> bool:
    """Create edit.db with the `edits` table if missing. Returns True on success."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=15)
        try:
            conn.executescript(SQL_CREATE_EDIT_DB)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        log_err(f"Could not create edit.db at {db_path}: {exc}")
        return False


def populate_edit_db(
    items: List[dict],
    meta_cache: Dict[str, dict],
    db_path: Path,
) -> int:
    """
    Insert one row per culled item into edit.db.

    `clean_name` is the camera filename WITHOUT extension and WITHOUT hash
    suffix (e.g. "DSC_7957", "DSC03997"). The agent will write its own
    output to output_path later.

    Returns number of rows written.
    """
    if not init_edit_db(db_path):
        return 0

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    written = 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=15)
        try:
            for it in items:
                stem = it.get("stem", "")
                # Strip any extension or hash suffix from clean_name field
                bare_clean = stem  # already bare in paste mode
                rating = int(it.get("rating", 0))
                if rating < 1 or rating > 5:
                    continue
                meta = meta_cache.get(stem.upper(), {})
                conn.execute(
                    "INSERT INTO edits ("
                    "  clean_name, rating, "
                    "  score_overall, score_quality, score_composition, "
                    "  score_lighting, score_color, score_dof, score_content, "
                    "  caption, edit_status, created_at "
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bare_clean,
                        rating,
                        meta.get("score_overall"),
                        meta.get("score_quality"),
                        meta.get("score_composition"),
                        meta.get("score_lighting"),
                        meta.get("score_color"),
                        meta.get("score_dof"),
                        meta.get("score_content"),
                        meta.get("caption"),
                        "pending",
                        now,
                    ),
                )
                written += 1
            conn.commit()
        finally:
            conn.close()
        log_ok(f"edit.db populated: {written} rows -> {db_path}")
    except Exception as exc:
        log_err(f"Failed to populate edit.db: {exc}")
    return written


# =============================================================================
# Copy-RAWs-while-rating helpers (used by both GUI and CLI)
# =============================================================================
#
# Use case: the user keeps RAWs on an external HDD but wants a local working
# copy in workspace/raws/ that also gets the EXIF Rating. This way the
# rated subset survives even if the external drive is unplugged, and the
# external originals also get the rating so it appears in any cataloger.

def is_external_dir(target: Path, workspace: Path) -> bool:
    """
    True if `target` is NOT a subdirectory of `workspace`.

    We resolve symlinks first so that a symlink in workspace pointing to an
    external mount is still considered external (it physically is).
    """
    try:
        t = target.resolve()
        w = workspace.resolve()
    except Exception:
        return True  # unresolvable paths default to "external/foreign"
    try:
        t.relative_to(w)
        return False  # target IS under workspace
    except ValueError:
        return True   # target is somewhere else on the filesystem


def local_raws_has_any_raw(local_raws_dir: Path) -> bool:
    """
    True if local_raws_dir contains AT LEAST ONE file with a known RAW
    extension. Non-RAW files (e.g. .xmp sidecars) do NOT count.

    If even one .NEF/.ARW/etc is already there, we assume the user has
    already populated this folder intentionally — do not prompt, do not copy.
    """
    if not local_raws_dir.is_dir():
        return False
    try:
        for f in local_raws_dir.iterdir():
            if f.is_file() and f.suffix.lower() in RAW_EXTS:
                return True
    except (OSError, PermissionError):
        pass
    return False


def should_offer_copy_raws(workspace: Path, external_raw_dir: Optional[Path]) -> bool:
    """
    Returns True iff:
      - An external RAW dir was given (not None, exists)
      - It is NOT inside workspace (i.e. external drive / different tree)
      - The local workspace/raws/ either doesn't exist OR contains NO RAW files
    """
    if external_raw_dir is None:
        return False
    if not external_raw_dir.is_dir():
        return False
    if not is_external_dir(external_raw_dir, workspace):
        return False
    local_raws = workspace / "raws"
    return not local_raws_has_any_raw(local_raws)


def copy_raw_to_local(src_raw: Path, local_raws_dir: Path, expected_hash: Optional[str] = None) -> Optional[Path]:
    """
    Copy a single RAW from external to local raws/, preserving filename
    and metadata. Returns the new local Path, or None on failure.

    If a file with the same name already exists at the destination and the hash differs,
    we rename it by appending '0' to the stem (e.g. DSC_12340.NEF).
    """
    try:
        local_raws_dir.mkdir(parents=True, exist_ok=True)
        
        stem = src_raw.stem
        ext = src_raw.suffix
        dest = local_raws_dir / f"{stem}{ext}"
        
        if expected_hash:
            # Loop to find a non-conflicting filename or one that already has our exact hash
            while dest.exists():
                if file_sha256(dest) == expected_hash:
                    # Already copied exactly!
                    return dest
                # Conflict: append '0'
                stem = f"{stem}0"
                dest = local_raws_dir / f"{stem}{ext}"
        else:
            # If no expected_hash is provided, default overwrite fallback
            pass
            
        shutil.copy2(str(src_raw), str(dest))
        return dest
    except Exception as exc:
        log_warn(f"Could not copy {src_raw.name} -> {local_raws_dir}: {exc}")
        return None


# =============================================================================
# Tasker Core
# =============================================================================

class LVSTasker:
    """
    LVS Tasker — First touchpoint of the ingest → edit pipeline.
    
    Reads user ratings from select1-5 cull folders, resolves each rated
    file to its RAW counterpart, writes EXIF Rating to both, commits to
    ingest.db, and generates task.md.
    
    Design principle: this script is STRICT about sequence and verification.
    Every step is logged. Every EXIF write is verified. Every DB write is
    committed before moving on. The downstream editing agent (photoedit.py)
    trusts the data this script produces — so this script must produce
    truthful data.
    
    Pipeline phases:
      0. Initialize — validate workspace, build RAW index, init DB tables
      1. Scan — read select1-5 folders, parse filenames
      2. Verify — check file hashes against ingest.db
      3. Resolve — match each select to its RAW file
      4. EXIF — write Rating tag to select + RAW
      5. Organize — copy clean-named files to combined select/ folder
      6. Database — commit rating records to ingest.db
      7. Report — generate task.md
    """

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace = workspace_dir.resolve()

        # Core directories
        self.raws_dir = self.workspace / "raws"
        self.select_dir = self.workspace / "select"
        self.db_path = self.workspace / "ingest.db"

        # State
        self.raw_index: Optional[RawIndex] = None
        self.exiftool_bin: Optional[str] = None
        self.processed_items: List[dict] = []
        self.run_id: Optional[int] = None

        # Copy-while-rating decision (set by execute() prompt or GUI)
        self.copy_raws_while_rating: bool = False
        self.copies_made: List[Path] = []   # local copies created this run

        # RAW source (external drive, SD card, etc.) — when set, the RawIndex
        # builds from this path instead of the local workspace/raws/.  The
        # local workspace/raws/ is ALWAYS the copy destination.
        self.raws_source: Optional[Path] = None

        # Decision callback for interactive prompts (GUI mode).
        # Signature: (filename: str, reasons: list[str]) -> bool
        # When None, uses CLI input(); in GUI mode set by TaskerExecuteWorker.
        self.decision_callback: Optional[Callable[[str, list], bool]] = None

        # Stats for run summary
        self.stats = {
            "total_scanned": 0,
            "total_verified": 0,
            "raws_resolved": 0,
            "raws_missing": 0,
            "exif_select_ok": 0,
            "exif_select_fail": 0,
            "exif_raw_ok": 0,
            "exif_raw_fail": 0,
            "db_rows_written": 0,
            "select_copied": 0,
            "raws_copied_local": 0,
            "raws_copied_failed": 0,
        }

    # ---- Phase 0: Initialize ----

    def phase0_initialize(self) -> bool:
        """
        Validate workspace structure, locate tools, build indices.
        Returns False if critical initialization fails.
        """
        log_info("Phase 0: Initialization")

        # Validate workspace directory exists
        if not self.workspace.is_dir():
            log_err(f"Workspace directory does not exist: {self.workspace}")
            return False

        # Check for rated folders (new schema: select1-5, legacy: edit1-5)
        found_folders = []
        for prefix in (RATED_FOLDER_PREFIX, LEGACY_FOLDER_PREFIX):
            for r in range(1, 6):
                d = self.workspace / f"{prefix}{r}"
                if d.is_dir():
                    found_folders.append(d)
        if not found_folders:
            log_err("No select1-5 or edit1-5 folders found in workspace")
            return False

        # Locate exiftool
        self.exiftool_bin = find_exiftool()
        if not self.exiftool_bin:
            log_warn("exiftool not found — EXIF rating writes will be skipped")
            log_warn("Install exiftool for full functionality:")
            if os.name == "nt":
                log_warn("  choco install exiftool")
            else:
                log_warn("  apt install libimage-exiftool-perl")

        # Build RAW index from raws_source (external drive) if set,
        # otherwise from the local workspace/raws/.
        if self.raws_source and self.raws_source.is_dir():
            self.raw_index = RawIndex(self.raws_source)
            log_step(f"RAW index: {len(self.raw_index.all_raws)} files indexed from {self.raws_source}")
        elif self.raws_dir.is_dir():
            self.raw_index = RawIndex(self.raws_dir)
            log_step(f"RAW index: {len(self.raw_index.all_raws)} files indexed")
        else:
            log_warn(f"raws/ directory not found at {self.raws_dir}")
            log_warn("RAW resolution will be unavailable — EXIF on RAWs skipped")

        # Initialize database tables
        try:
            if self.db_path.exists():
                init_tasker_tables(self.db_path)
                log_step(f"Tasker rating tables initialized: {self.db_path}")
            edit_db_path = self.workspace / "edit.db"
            # Rotate stale edit.db → edit_stale1.db → edit_stale2.db …
            if edit_db_path.exists():
                stale_n = 1
                while True:
                    stale_path = self.workspace / f"edit_stale{stale_n}.db"
                    if not stale_path.exists():
                        break
                    stale_n += 1
                try:
                    shutil.move(str(edit_db_path), str(stale_path))
                    log_step(f"Previous edit.db renamed → {stale_path.name}")
                except Exception as e:
                    log_warn(f"Could not rename old edit.db: {e}")
            init_edit_db(edit_db_path)
            log_step(f"Database initialized: {edit_db_path}")
        except Exception as exc:
            log_err(f"Database initialization failed: {exc}")
            return False

        # Create output directory
        self.select_dir.mkdir(parents=True, exist_ok=True)

        # Create run record in DB
        self._create_run_record()

        return True

    def _create_run_record(self) -> None:
        """Insert a tasker_run row to track this execution."""
        # ingest.db is untouched.
        pass

    # ---- Phase 1: Scan ----

    def phase1_scan(self) -> List[dict]:
        """
        Scan select1-5 (and legacy edit1-5) folders for rated files.
        
        Each file becomes an item dict with:
        - original_path: Path to the file in the rated folder
        - original_name: Filename as found (may include hash suffix)
        - clean_name: Filename without hash suffix
        - stem: Filename stem (no extension, no hash)
        - hash_prefix: 16-char hex hash if present, else ""
        - rating: Integer 1-5 from folder name
        - camera_number: Extracted numeric ID
        - raw_path: Resolved RAW path (populated in phase 3)
        - raw_sha256: SHA-256 of RAW file (populated in phase 3)
        """
        log_info("Phase 1: Scanning rated folders")
        items = []

        # Try new schema first, fall back to legacy
        folder_prefix = RATED_FOLDER_PREFIX
        found_any = False
        for r in range(1, 6):
            d = self.workspace / f"{folder_prefix}{r}"
            if d.is_dir():
                found_any = True
                break
        if not found_any:
            folder_prefix = LEGACY_FOLDER_PREFIX
            log_info("  Using legacy edit1-5 folder naming")

        for r in range(1, 6):
            folder = self.workspace / f"{folder_prefix}{r}"
            if not folder.is_dir():
                continue

            try:
                entries = list(os.scandir(folder))
            except Exception as exc:
                log_err(f"Cannot read {folder.name}: {exc}")
                continue

            folder_count = 0
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name
                # Skip system files and task.md
                if name.startswith(".") or name.lower() in ("task.md", "thumbs.db", "desktop.ini"):
                    continue

                filepath = Path(entry.path)
                parsed = parse_lvs_filename(name)
                cam_num = extract_camera_number(parsed["stem"])

                items.append({
                    "original_path": filepath,
                    "original_name": name,
                    "clean_name": parsed["clean_name"],
                    "stem": parsed["stem"],
                    "hash_prefix": parsed["hash_prefix"],
                    "ext": parsed["ext"],
                    "rating": r,
                    "camera_number": cam_num,
                    "raw_path": None,
                    "raw_sha256": None,
                })
                folder_count += 1

            if folder_count > 0:
                log_step(f"{folder.name}/: {folder_count} files (rating {r}★)")

        self.stats["total_scanned"] = len(items)
        log_ok(f"Found {len(items)} rated files total")
        return items

    # ---- Phase 2: Verify ----

    def phase2_verify(self, items: List[dict],
                      decision_callback: Optional[Callable[[str, list], bool]] = None) -> List[dict]:
        """
        Verify file integrity against ingest.db hashes.
        
        Non-blocking: files that can't be verified still proceed.
        Files with hash mismatches prompt the user (interactive mode)
        via decision_callback when provided, otherwise via CLI input().
        In non-interactive mode without a callback, mismatches are auto-skipped.
        """
        log_info("Phase 2: Hash verification against ingest.db")

        if not self.db_path.exists():
            log_warn("ingest.db not found — verification skipped")
            return items

        verified = verify_against_ingest_db(items, self.db_path, decision_callback)
        self.stats["total_verified"] = len(verified)

        removed = len(items) - len(verified)
        if removed > 0:
            log_warn(f"{removed} file(s) removed by verification")
        else:
            log_ok(f"All {len(verified)} files verified")

        return verified

    # ---- Phase 3: Resolve RAWs ----

    def phase3_resolve_raws(self, items: List[dict]) -> List[dict]:
        """
        Match each rated select file to its RAW counterpart.
        
        Uses the RawIndex built in phase 0. Resolution strategy:
        1. Exact stem match (DSC01116 → DSC01116.NEF)
        2. Camera number match (01116 → any RAW with that number)
        3. Substring match (last resort)
        
        RAW resolution failure is NOT fatal — the select file still gets
        its EXIF rating and DB record, but raw_path remains None and
        exif_raw_ok stays 0. The downstream editor will handle missing RAWs.
        
        WHY RAWs MATTER: The photoedit.py pipeline develops RAWs, not JPEGs.
        The select JPEG is just a preview — the actual edit target is the RAW.
        Without RAW resolution, the editor can't process the image.
        """
        log_info("Phase 3: Resolving RAW files")

        if not self.raw_index:
            log_warn("No RAW index available — all RAW resolution skipped")
            self.stats["raws_missing"] = len(items)
            return items

        resolved = 0
        missing = 0

        # Query source_hash from ingest.db copy to match by hash
        db_hashes = {}
        if self.db_path.exists():
            try:
                conn, temp_dir = open_db_copy(self.db_path)
                for item in items:
                    stem = item["stem"]
                    prefix = item["hash_prefix"]
                    row = None
                    if prefix:
                        try:
                            row = conn.execute(
                                "SELECT source_hash FROM files WHERE source_hash LIKE ? LIMIT 1",
                                (f"{prefix}%",)
                            ).fetchone()
                        except sqlite3.OperationalError:
                            pass
                    if not row and stem:
                        try:
                            row = conn.execute(
                                "SELECT source_hash FROM files WHERE file_name LIKE ? LIMIT 1",
                                (f"{stem}.%",)
                            ).fetchone()
                        except sqlite3.OperationalError:
                            pass
                    if row and row[0]:
                        db_hashes[stem.upper()] = row[0]
            except Exception as exc:
                log_warn(f"Failed to read source hashes from ingest.db: {exc}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

        for item in items:
            expected_hash = db_hashes.get(item["stem"].upper())
            # Try resolution by stem first, then by camera number
            raw_path = self.raw_index.resolve(item["stem"], expected_hash=expected_hash)

            if raw_path is None and item["camera_number"]:
                raw_path = self.raw_index.resolve(item["camera_number"], expected_hash=expected_hash)

            if raw_path:
                item["raw_path"] = raw_path
                item["raw_sha256"] = file_sha256(raw_path)
                resolved += 1
                log_step(f"  {item['stem']} → {raw_path.name}")
            else:
                missing += 1
                log_warn(f"  RAW not found for {item['original_name']} (stem={item['stem']}, "
                         f"number={item['camera_number']})")

        self.stats["raws_resolved"] = resolved
        self.stats["raws_missing"] = missing
        log_ok(f"RAW resolution: {resolved} found, {missing} missing")
        return items

    # ---- Phase 4: EXIF Ratings ----

    def phase4_exif(self, items: List[dict]) -> None:
        """
        Write EXIF Rating tags to select files and RAW files.
        
        For each item:
        1. Write Rating to the select file (JPEG/PNG in the rated folder)
        2. If RAW was resolved, write Rating to the RAW file (NEF/ARW)
        3. Verify each write by reading back
        
        The Rating tag is the universal standard read by all DAM software.
        For RAW files, exiftool maps it to the manufacturer-specific field.
        
        CRITICAL: We write to the file IN THE RATED FOLDER, not the combined
        select/ folder. The select/ folder gets copies later. This ensures
        the original culled files have the correct rating before they're
        organized.
        """
        log_info("Phase 4: Writing EXIF Rating tags")

        if not self.exiftool_bin:
            log_warn("exiftool not found — skipping all EXIF writes")
            return

        for item in items:
            rating = item["rating"]

            # --- Write to select file ---
            ok_select, msg = exiftool_set_rating(
                item["original_path"], rating, self.exiftool_bin
            )
            if ok_select:
                item["exif_select_ok"] = 1
                self.stats["exif_select_ok"] += 1
                log_step(f"Select {rating}★: {item['original_name']}")
            else:
                item["exif_select_ok"] = 0
                self.stats["exif_select_fail"] += 1
                log_warn(f"Select EXIF failed: {msg}")

            # --- Write to RAW file ---
            if item["raw_path"]:
                external_raw = item["raw_path"]
                local_copy: Optional[Path] = None

                # If user opted to copy-while-rating, and this RAW lives outside
                # the workspace, copy it into workspace/raws/ first.
                if self.copy_raws_while_rating and is_external_dir(external_raw, self.workspace):
                    local_copy = copy_raw_to_local(external_raw, self.raws_dir, expected_hash=item.get("raw_sha256"))
                    if local_copy is not None:
                        self.copies_made.append(local_copy)
                        self.stats["raws_copied_local"] += 1
                        log_step(f"Copied -> raws/: {local_copy.name}")
                    else:
                        self.stats["raws_copied_failed"] += 1

                # EXIF the LOCAL copy if we made one
                local_ok = True
                if local_copy is not None:
                    local_ok, msg_local = exiftool_set_rating(
                        local_copy, rating, self.exiftool_bin
                    )
                    if local_ok:
                        log_step(f"Local copy {rating}★: {local_copy.name}")
                    else:
                        log_warn(f"Local copy EXIF failed: {msg_local}")

                # EXIF the EXTERNAL ORIGINAL too (so external catalogers see the rating)
                ok_raw, msg_raw = exiftool_set_rating(
                    external_raw, rating, self.exiftool_bin
                )
                if ok_raw:
                    item["exif_raw_ok"] = 1
                    self.stats["exif_raw_ok"] += 1
                    log_step(f"RAW   {rating}★: {external_raw.name}")
                else:
                    item["exif_raw_ok"] = 0
                    self.stats["exif_raw_fail"] += 1
                    log_warn(f"RAW EXIF failed: {msg_raw}")

                # If we made a local copy, the DB row should point at it
                # (the agentic editor will look in workspace/raws/ first).
                if local_copy is not None and local_ok:
                    item["raw_path"] = local_copy
                    item["raw_external_original"] = str(external_raw)
            else:
                item["exif_raw_ok"] = 0

    # ---- Phase 5: Organize ----

    def phase5_organize(self, items: List[dict]) -> None:
        """
        MOVE rated files to the combined select/ folder with clean names.
        
        Clean name = filename without the hash suffix.
        E.g., DSC01116_cf479ea38d56610d.jpg → DSC01116.jpg
        
        If a file with the same clean name already exists in select/,
        it is overwritten. This is intentional — the tasker is the authority
        on what goes in select/.
        
        This is a DESTRUCTIVE operation — files are moved out of select1-5.
        The source folders are deleted after the database commit (phase 6).
        """
        log_info("Phase 5: Moving files to combined select/ folder")

        self.select_dir.mkdir(parents=True, exist_ok=True)
        moved = 0

        for item in items:
            src = item["original_path"]
            dest = self.select_dir / item["clean_name"]

            try:
                # Remove existing if present (clean overwrite)
                if dest.exists():
                    dest.unlink()

                shutil.move(str(src), str(dest))
                item["select_final_path"] = dest
                moved += 1
                log_step(f"Moved: {item['original_name']} → select/{item['clean_name']}")
            except Exception as exc:
                log_err(f"Failed to move {item['original_name']}: {exc}")
                item["select_final_path"] = None

        self.stats["select_copied"] = moved
        log_ok(f"{moved} files moved to select/")

    def phase5b_cleanup_source_folders(self) -> None:
        """
        Delete the now-empty select1-5 (or edit1-5) source folders.
        
        Called AFTER database commit succeeds. Only deletes folders that
        are empty or contain only non-file entries (system files, etc.).
        Never deletes raws/ or select/.
        """
        log_info("Phase 5b: Cleaning up source folders")

        for prefix in (RATED_FOLDER_PREFIX, LEGACY_FOLDER_PREFIX):
            for r in range(1, 6):
                folder = self.workspace / f"{prefix}{r}"
                if not folder.is_dir():
                    continue
                try:
                    remaining = list(folder.iterdir())
                    if not remaining:
                        folder.rmdir()
                        log_step(f"Removed empty {folder.name}/")
                    else:
                        # Check if only junk remains (desktop.ini, thumbs.db, .DS_Store)
                        junk = {".ds_store", "thumbs.db", "desktop.ini"}
                        real_files = [f for f in remaining if f.name.lower() not in junk]
                        if not real_files:
                            # Safe to remove — only junk files left
                            for f in remaining:
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                            folder.rmdir()
                            log_step(f"Removed {folder.name}/ (junk cleaned)")
                        else:
                            log_warn(f"Keeping {folder.name}/ — contains {len(real_files)} unprocessed files")
                except Exception as exc:
                    log_warn(f"Could not remove {folder.name}/: {exc}")

    # ---- Phase 6: Database Commit ----

    def phase6_database(self, items: List[dict]) -> None:
        """
        Write rating records to edit.db edits table.
        Since ingest.db is untouched and read-only, we do not write tasker_ratings to ingest.db.
        Instead, we rely on edit.db for the downstream editing agent.
        """
        log_info("Phase 6: Writing to edits table in edit.db (ingest.db is untouched)")
        edit_db_path = self.workspace / "edit.db"
        # Rotate stale edit.db → edit_stale1.db → edit_stale2.db …
        if edit_db_path.exists():
            stale_n = 1
            while True:
                stale_path = self.workspace / f"edit_stale{stale_n}.db"
                if not stale_path.exists():
                    break
                stale_n += 1
            try:
                shutil.move(str(edit_db_path), str(stale_path))
                log_step(f"Previous edit.db renamed → {stale_path.name}")
            except Exception as e:
                log_warn(f"Could not rename old edit.db: {e}")
        meta_cache = query_ingest_metadata(items, self.db_path)
        written = populate_edit_db(items, meta_cache, edit_db_path)
        self.stats["db_rows_written"] = written

    def _find_ingest_file_id(self, conn: sqlite3.Connection, item: dict) -> Optional[int]:
        """
        Try to link this item to an existing record in the ingest.db files table.
        
        This cross-reference lets the downstream pipeline join tasker_ratings
        with the original ingest metadata (hashes, timestamps, etc.).
        
        Returns the file_id or None.
        """
        # Try by hash prefix first
        if item["hash_prefix"]:
            try:
                row = conn.execute(
                    "SELECT file_id FROM files WHERE source_hash LIKE ? LIMIT 1",
                    (f"{item['hash_prefix']}%",),
                ).fetchone()
                if row:
                    return row[0]
            except sqlite3.OperationalError:
                pass

        # Try by filename stem
        try:
            row = conn.execute(
                "SELECT file_id FROM files WHERE file_name LIKE ? LIMIT 1",
                (f"{item['stem']}.%",),
            ).fetchone()
            if row:
                return row[0]
        except sqlite3.OperationalError:
            pass

        return None

    # ---- Phase 7: Report ----

    def phase7_report(self, items: List[dict], meta_cache: Dict[str, dict]) -> None:
        """
        Generate task.md — the editing manifest for the downstream agent.
        
        task.md is the primary interface between the tasker and the editing
        agent. It contains:
        1. Files grouped by rating (5★ first, 1★ last)
        2. Aesthetic scores from ingest.db (if available)
        3. RAW file paths (if resolved)
        4. Editing guidelines based on rating tier
        5. Pipeline statistics
        
        The editing agent reads this file to decide what to process and how.
        """
        log_info("Phase 7: Generating task.md")

        md_path = self.workspace / "task.md"
        lines: List[str] = []

        lines.append("# 📷 LVS Master Agentic Edit Task\n")
        lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append(f"Workspace: `{self.workspace}`\n")
        lines.append(f"RAWs directory: `{self.raws_dir}`\n\n")

        # Summary statistics
        lines.append("## Pipeline Summary\n")
        lines.append(f"| Metric | Count |")
        lines.append(f"|:---|---:|")
        lines.append(f"| Files scanned | {self.stats['total_scanned']} |")
        lines.append(f"| Files verified | {self.stats['total_verified']} |")
        lines.append(f"| RAWs resolved | {self.stats['raws_resolved']} |")
        lines.append(f"| RAWs missing | {self.stats['raws_missing']} |")
        lines.append(f"| EXIF select OK | {self.stats['exif_select_ok']} |")
        lines.append(f"| EXIF RAW OK | {self.stats['exif_raw_ok']} |")
        lines.append(f"| DB records | {self.stats['db_rows_written']} |")
        lines.append(f"| Select/ folder | {self.stats['select_copied']} |")
        lines.append("\n")

        # Per-rating sections (5★ first for priority)
        fmt_score = lambda x: f"{x:+.2f}" if isinstance(x, (int, float)) else "—"

        for rating in range(5, 0, -1):
            rating_items = [x for x in items if x["rating"] == rating]
            if not rating_items:
                continue

            lines.append(f"---\n\n")
            lines.append(f"## ★{rating} Selection ({len(rating_items)} photo(s))\n\n")
            lines.append(
                "| File | RAW | Overall | Qual | Comp | Light | Color | DoF | Content | Caption |\n"
            )
            lines.append(
                "|:---|:---|---:|---:|---:|---:|---:|---:|---:|:---|\n"
            )

            for item in sorted(rating_items, key=lambda x: x["clean_name"]):
                meta = meta_cache.get(item["clean_name"], {})
                raw_name = item["raw_path"].name if item["raw_path"] else "❌ NOT FOUND"
                raw_cap = meta.get('caption') or ''
                clean_cap = _hesitancy_clean(raw_cap, str(self.workspace)) if raw_cap else '—'

                lines.append(
                    f"| `{item['clean_name']}` "
                    f"| `{raw_name}` "
                    f"| {fmt_score(meta.get('score_overall'))} "
                    f"| {fmt_score(meta.get('score_quality'))} "
                    f"| {fmt_score(meta.get('score_composition'))} "
                    f"| {fmt_score(meta.get('score_lighting'))} "
                    f"| {fmt_score(meta.get('score_color'))} "
                    f"| {fmt_score(meta.get('score_dof'))} "
                    f"| {fmt_score(meta.get('score_content'))} "
                    f"| {clean_cap} |\n"
                )

            lines.append("\n")



        try:
            with md_path.open("w", encoding="utf-8") as f:
                f.writelines(lines)
            log_ok(f"task.md written to {md_path}")
        except Exception as exc:
            log_err(f"Failed to write task.md: {exc}")

    # ---- Run Summary ----

    def _update_run_record(self, status: str) -> None:
        """Update the tasker_run record with final statistics."""
        # ingest.db is untouched.
        pass

    # ---- Main Execute ----

    def execute(self) -> int:
        """
        Run the complete tasker pipeline. Returns exit code (0 = success).
        
        This method orchestrates all phases in strict sequence. Each phase
        must complete before the next begins. There is no parallelism —
        this is intentional. The agent must not be allowed to skip phases
        or run them concurrently.
        """
        print(f"\n{C_BLUE}{C_BOLD}{'=' * 60}{C_RESET}")
        print(f"{C_GREEN}{C_BOLD}{'LVS TASKER — INGEST PIPELINE FIRST TOUCHPOINT':^60}{C_RESET}")
        print(f"{C_BLUE}{C_BOLD}{'=' * 60}{C_RESET}")
        print(f"  Workspace  : {C_BOLD}{self.workspace}{C_RESET}")
        print(f"  Database   : {C_BOLD}{self.db_path}{C_RESET}")
        print(f"  RAWs source : {C_BOLD}{self.raws_source or self.raws_dir}{C_RESET}")
        print(f"  RAWs dst    : {C_BOLD}{self.raws_dir}{C_RESET}")
        print(f"  Output     : {C_BOLD}{self.select_dir}{C_RESET}")
        print(f"{C_BLUE}{C_BOLD}{'─' * 60}{C_RESET}\n")

        # Phase 0: Initialize
        if not self.phase0_initialize():
            self._update_run_record("init_failed")
            return 1

        # Phase 1: Scan
        items = self.phase1_scan()
        if not items:
            log_warn("No rated files found. Nothing to do.")
            self._update_run_record("empty")
            return 0

        # Phase 2: Verify
        items = self.phase2_verify(items, self.decision_callback)
        if not items:
            log_warn("No files remaining after verification.")
            self._update_run_record("all_verification_failed")
            return 1

        # Phase 3: Resolve RAWs
        items = self.phase3_resolve_raws(items)
        # Gate: if we resolved < 80% of RAWs and there was an external source
        # configured, the RAW path is probably wrong — don't proceed.
        resolved = self.stats.get("raws_resolved", 0)
        total = self.stats.get("total_verified", self.stats.get("total_scanned", 0))
        if self.raws_source and self.raws_source.is_dir() and total > 0 and (resolved / total) < 0.8:
            log_err(
                f"RAW resolution rate too low: {resolved}/{total} "

                f"({resolved / total:.0%}) — less than 80% threshold."

            )
            log_err(
                "The RAW source directory does not appear to contain matching "

                "RAW files.  Check the RAWs path or set it to the correct "

                "SD card / external drive root."

            )
            log_err("Aborting cull — no files were moved or written.")

            self._update_run_record("raw_resolution_failed")

            return 1

        # Pre-Phase 4: copy-while-rating decision.
        # GUI mode: honour the inline toggle the user already set (no prompt).
        # CLI mode: prompt interactively, defaulting YES.
        # Non-interactive without a callback: default YES (external RAWs dir
        # was deliberately configured — the user wants those copies).
        ext_raws = self.raws_source if (self.raws_source and self.raws_source.is_dir()) else None
        if should_offer_copy_raws(self.workspace, ext_raws):
            log_info(
                f"RAWs dir '{ext_raws}' is external and local workspace/raws/ is empty."
            )
            if self.decision_callback is not None:
                # GUI mode — user toggled the checkbox, already decided.
                # copy_raws_while_rating was set by the worker before execute().
                pass
            elif sys.stdin.isatty():
                try:
                    ans = input(
                        f"  Copy RAWs to workspace/raws/ while rating? [Y/n]: "
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    ans = ""
                self.copy_raws_while_rating = ans in ("", "y", "yes")
            else:
                # Non-interactive pipe — default YES. The user deliberately
                # pointed at an external RAWs dir; copying is the expected
                # behaviour for a non-interactive run.
                self.copy_raws_while_rating = True
            log_info(
                f"Copy-while-rating: {'ON' if self.copy_raws_while_rating else 'OFF'}"
            )

        # Phase 4: Write EXIF
        self.phase4_exif(items)

        # Phase 5: Organize
        self.phase5_organize(items)
        self.processed_items = items

        # Phase 6: Database
        self.phase6_database(items)

        # Phase 5b: Cleanup source folders (only after DB commit succeeds)
        self.phase5b_cleanup_source_folders()

        # Phase 7: Report
        meta_cache = query_ingest_metadata(items, self.db_path)
        self.phase7_report(items, meta_cache)

        # Final summary
        self._update_run_record("ok")
        print(f"\n{C_BLUE}{C_BOLD}{'=' * 60}{C_RESET}")
        print(f"{C_GREEN}{C_BOLD}{'LVS TASKER COMPLETE':^60}{C_RESET}")
        print(f"{C_BLUE}{C_BOLD}{'=' * 60}{C_RESET}")
        print(f"  Scanned    : {self.stats['total_scanned']}")
        print(f"  RAWs found : {self.stats['raws_resolved']} / {self.stats['total_scanned']}")
        print(f"  EXIF OK    : {self.stats['exif_select_ok']} select, {self.stats['exif_raw_ok']} RAW")
        print(f"  DB rows    : {self.stats['db_rows_written']}")
        print(f"  select/    : {self.stats['select_copied']} files")
        if self.stats["raws_missing"] > 0:
            print(f"  {C_YELLOW}RAWs missing: {self.stats['raws_missing']}{C_RESET}")
        print(f"{C_BLUE}{C_BOLD}{'─' * 60}{C_RESET}\n")

        return 0


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    # Check for --gui flag
    if "--gui" in sys.argv:
        try:
            from lvs_tasker_gui import launch_gui
            launch_gui()
        except ImportError as exc:
            print(f"Cannot launch GUI: {exc}", file=sys.stderr)
            print("Ensure lvs_tasker_gui.py is in the same directory.", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # Determine workspace directory
    watch_path = Path.cwd()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        arg = Path(args[0])
        if arg.is_dir():
            watch_path = arg
        else:
            print(f"Error: {arg} is not a directory", file=sys.stderr)
            sys.exit(1)

    # Parse copy-raws flags
    force_copy: Optional[bool] = None
    if "--copy-raws" in sys.argv:
        force_copy = True
    elif "--no-copy-raws" in sys.argv:
        force_copy = False

    tasker = LVSTasker(watch_path)
    if force_copy is not None:
        tasker.copy_raws_while_rating = force_copy
    try:
        exit_code = tasker.execute()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print(f"\n{C_YELLOW}Interrupted by user.{C_RESET}", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(
            f"\n{C_RED}{C_BOLD}HALT — Unhandled exception in LVS Tasker{C_RESET}\n"
            f"{C_RED}{exc}{C_RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

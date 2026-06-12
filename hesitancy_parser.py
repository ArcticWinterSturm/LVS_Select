#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#  LVS Selection Assist  —  Hesitancy Parser  (caption clean + score nudges)
#  Version:    1.0.8
#  License:    AGPL-3.0-or-later
#  Developer:  ArcticWinter
#
#  WHAT THIS MODULE IS
#  -------------------
#  A single, self-contained module that the SELECTION side (this app) uses to:
#
#    1. CLEAN Florence captions for *display* using the "conservative mode"
#       rules in an optional `hesitancy.txt` sitting in the workspace.
#    2. Apply PHRASE-BASED score nudges (e.g. "blurry" penalises DoF/overall,
#       "background is blurred" boosts DoF) so the overlay shows discriminative
#       numbers and little green/red arrows.
#    3. Tell the overlay which leading phrases to render BOLD + slightly larger
#       + red, because Florence puts these tells at the very start of a caption.
#
#  This is EPHEMERAL on the selection side: it changes only what the overlay
#  shows for the current image. It NEVER writes anything back, never touches a
#  DB, never hot-reloads. (`hesitancy.txt` is read once, lazily, and cached.)
#
#  DOWNSTREAM RE-USE
#  -----------------
#  This module will become part of the originating ingest pipeline, where the
#  *same* cleaning is applied before the Florence caption is written to the DB.
#  Because of that dual life, EVERYTHING here degrades gracefully:
#    * Missing `hesitancy.txt`            -> caption cleaning is a near no-op.
#    * Missing the phrase-score table     -> scores pass through untouched.
#    * Any individual rule failing        -> that rule is skipped, not fatal.
#  i.e. "not all at once — all must load from that .py whatever is applicable".
#
#  hesitancy.txt format (conservative mode):
#      phrase alone on a line       -> DELETE the phrase
#      verbose phrase | shorter     -> REPLACE the phrase with `shorter`
#      |phrase                      -> DELETE the phrase + capitalise next word
#      lines starting with '#'      -> comments (ignored), inline '#' ignored
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional, Tuple, Any

__version__ = "1.0.8"

# Default filename looked for in the workspace (always this name, optional).
HESITANCY_FILENAME = "hesitancy.txt"


# =============================================================================
# §1  PHRASE SCORE TABLE  (hardcoded — easiest to manage here, per request)
# =============================================================================
#
# Scores in this pipeline are 0.0–1.0 floats (see ingest `previews` table).
# Each rule nudges one or more score channels by a delta on that 0–1 scale and
# is clamped to [0, 1].  Deltas are intentionally modest; phrases are additive
# but each *channel* is touched at most once per caption (strongest |delta|
# wins) so stacking "blurry" + "out of focus" doesn't nuke a score twice.
#
# direction is purely for the overlay arrow:
#     "up"   -> green arrow on the RIGHT of the number  (boost)
#     "down" -> red arrow on the LEFT of the number     (penalty)
#
# `display` rules also make the leading phrase render BOLD + larger + red in
# the overlay caption (these are the Florence "tells").
#
# Each entry: phrase -> {deltas: {channel: float}, display: bool}
# channels: score_overall score_quality score_composition score_lighting
#           score_color score_dof score_content
PHRASE_SCORE_RULES: List[Tuple[str, Dict[str, Any]]] = [
    # --- DoF boosters (intentional shallow-DoF look = good separation) ------
    ("background is blurred",   {"deltas": {"score_dof": +0.22}, "display": True}),
    ("blurred background",      {"deltas": {"score_dof": +0.18}, "display": True}),
    ("shallow depth of field",  {"deltas": {"score_dof": +0.20}, "display": True}),
    ("depth of field",          {"deltas": {"score_dof": +0.12}, "display": True}),
    # --- Focus failures (penalise hard: a blurry frame is usually a reject) -
    ("out of focus",            {"deltas": {"score_dof": -0.30,
                                            "score_overall": -0.25}, "display": True}),
    ("blurry",                  {"deltas": {"score_dof": -0.35,
                                            "score_overall": -0.30}, "display": True}),
    # --- Quality / resolution tells (lift content + overall) ---------------
    ("high quality",            {"deltas": {"score_content": +0.12,
                                            "score_overall": +0.08}, "display": True}),
    ("high-resolution",         {"deltas": {"score_content": +0.06,
                                            "score_overall": +0.04}, "display": True}),
    ("high resolution",         {"deltas": {"score_content": +0.06,
                                            "score_overall": +0.04}, "display": True}),
]

# Longest phrases first so "shallow depth of field" matches before "depth of
# field" and "background is blurred" before "blurred background".
PHRASE_SCORE_RULES.sort(key=lambda kv: len(kv[0]), reverse=True)


# =============================================================================
# §2  CONSERVATIVE CAPTION CLEANER  (driven by optional hesitancy.txt)
# =============================================================================

class _Rule:
    __slots__ = ("kind", "src", "dst")

    def __init__(self, kind: str, src: str, dst: str = ""):
        self.kind = kind      # "delete" | "replace" | "delete_cap"
        self.src = src
        self.dst = dst


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing ' # ...' inline comment but keep '#' inside the rule
    text if it is not preceded by whitespace (rare). Conservative: only strip
    when '#' is preceded by whitespace or starts the line."""
    # full-line comment
    if line.lstrip().startswith("#"):
        return ""
    # inline comment: split on first ' #'
    m = re.search(r"\s#", line)
    if m:
        return line[: m.start()]
    return line


def _parse_hesitancy_text(text: str) -> List[_Rule]:
    rules: List[_Rule] = []
    for raw in text.splitlines():
        line = _strip_inline_comment(raw)
        if not line.strip():
            continue
        if "|" in line:
            left, right = line.split("|", 1)
            left_s = left.strip()
            right_s = right.strip()
            if left_s == "" and right_s != "":
                # "|phrase"  -> delete + capitalise next
                rules.append(_Rule("delete_cap", right_s))
            else:
                # "verbose | shorter"  (shorter may be empty -> delete)
                if right_s == "":
                    rules.append(_Rule("delete", left_s))
                else:
                    rules.append(_Rule("replace", left_s, right_s))
        else:
            rules.append(_Rule("delete", line.strip()))
    # Drop rules whose source is too short or pure punctuation — they
    # would match hundreds of unintended occurrences (a lone "." rule
    # deletes every period in every caption).
    MIN_SRC_LEN = 3
    rules = [r for r in rules if len(r.src) >= MIN_SRC_LEN
             and any(ch.isalpha() for ch in r.src)]
    # longest source first so specific phrases win over generic ones
    rules.sort(key=lambda r: len(r.src), reverse=True)
    return rules


def _capitalize_first_alpha(s: str) -> str:
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:]
    return s


def _apply_rules(caption: str, rules: List[_Rule]) -> str:
    out = caption
    for r in rules:
        if not r.src:
            continue
        pat = re.compile(re.escape(r.src), re.IGNORECASE)
        if r.kind == "replace":
            out = pat.sub(r.dst, out)
        elif r.kind == "delete":
            out = pat.sub("", out)
        elif r.kind == "delete_cap":
            # delete the phrase, then capitalise the next alpha char
            def _del_cap(m: re.Match) -> str:
                return "\x00"  # sentinel marks where to capitalise after
            out = pat.sub(_del_cap, out)
            # capitalise the first alpha following each sentinel
            parts = out.split("\x00")
            rebuilt = parts[0]
            for tail in parts[1:]:
                rebuilt += _capitalize_first_alpha(tail.lstrip())
            out = rebuilt
    # tidy whitespace + leftover leading punctuation/space
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = out.strip(" ,;:-")
    out = _capitalize_first_alpha(out)
    return out


# =============================================================================
# §3  PUBLIC FACADE  (overlay calls this)
# =============================================================================

class HesitancyParser:
    """
    Lazy, cached facade. Construct with the workspace dir; the first call that
    needs `hesitancy.txt` reads it once. Everything is best-effort.
    """

    def __init__(self, workspace_dir: Optional[str] = None,
                 hesitancy_path: Optional[str] = None):
        self._workspace = workspace_dir
        self._explicit_path = hesitancy_path
        self._rules: Optional[List[_Rule]] = None
        self._rules_loaded = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- loading
    def _resolve_path(self) -> Optional[str]:
        if self._explicit_path:
            return self._explicit_path
        env = os.environ.get("LVS_HESITANCY_PATH")
        if env:
            return env
        if self._workspace:
            return os.path.join(self._workspace, HESITANCY_FILENAME)
        return None

    def _ensure_rules(self) -> List[_Rule]:
        if self._rules_loaded:
            return self._rules or []
        with self._lock:
            if self._rules_loaded:
                return self._rules or []
            rules: List[_Rule] = []
            path = self._resolve_path()
            if path and os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        rules = _parse_hesitancy_text(f.read())
                    print(f"[Hesitancy] loaded {len(rules)} caption rules from {path}")
                except Exception as e:
                    print(f"[Hesitancy] failed to read {path}: {e} — captions pass through")
                    rules = []
            # No file is fine: cleaning becomes a near no-op.
            self._rules = rules
            self._rules_loaded = True
            return rules

    @property
    def has_caption_rules(self) -> bool:
        return bool(self._ensure_rules())

    # -------------------------------------------------------- caption cleaning
    def clean_caption(self, caption: Optional[str]) -> str:
        if not caption:
            return ""
        rules = self._ensure_rules()
        if not rules:
            # still do a light tidy (trim + collapse spaces) so display is sane
            try:
                out = re.sub(r"\s{2,}", " ", caption).strip()
                return _capitalize_first_alpha(out)
            except Exception:
                return caption
        try:
            return _apply_rules(caption, rules)
        except Exception as e:
            print(f"[Hesitancy] caption clean error (passing raw): {e}")
            return caption

    # -------------------------------------------------------- score modifiers
    def matched_phrases(self, caption: Optional[str]) -> List[str]:
        """Phrases present in the (raw) caption, longest-first, de-duplicated."""
        if not caption:
            return []
        low = caption.lower()
        found: List[str] = []
        for phrase, _meta in PHRASE_SCORE_RULES:
            if phrase in low:
                # avoid double-counting a substring already covered by a longer
                # matched phrase (e.g. "depth of field" inside "shallow depth…")
                if not any(phrase in f for f in found):
                    found.append(phrase)
        return found

    def display_phrases(self, caption: Optional[str]) -> List[str]:
        """Subset of matched phrases that should be rendered BOLD/red/larger."""
        return [p for p in self.matched_phrases(caption)
                if dict(PHRASE_SCORE_RULES).get(p, {}).get("display")]

    def phrase_directions(self, caption: Optional[str]) -> Dict[str, str]:
        """
        For each displayable matched phrase, return 'up' (net boost → green) or
        'down' (net penalty → red), based on the sign of its largest |delta|.
        Used by the overlay to colour the bold caption tell.
        """
        rules = dict(PHRASE_SCORE_RULES)
        out: Dict[str, str] = {}
        for p in self.display_phrases(caption):
            deltas = rules.get(p, {}).get("deltas", {})
            if not deltas:
                continue
            strongest = max(deltas.values(), key=abs)
            out[p] = "up" if strongest > 0 else "down"
        return out

    def apply_score_modifiers(
        self,
        caption: Optional[str],
        scores: Dict[str, Optional[float]],
    ) -> Tuple[Dict[str, Optional[float]], Dict[str, str], List[str]]:
        """
        Given the raw caption and the per-channel scores (0–1 floats or None),
        return:
            (new_scores, arrows, matched_phrases)
        where:
            new_scores   - clamped, phrase-nudged copy of `scores`
            arrows       - {channel: 'up'|'down'} for channels that were nudged
            matched_phrases - the phrases that triggered (for logging/UI)

        Each channel is nudged at most once (the strongest |delta| applied by
        any matched phrase wins) so overlapping phrases don't double-hit.
        """
        new_scores: Dict[str, Optional[float]] = dict(scores)
        arrows: Dict[str, str] = {}
        matched = self.matched_phrases(caption)
        if not matched:
            return new_scores, arrows, matched

        # collect the winning delta per channel
        best: Dict[str, float] = {}
        rules = dict(PHRASE_SCORE_RULES)
        for phrase in matched:
            for ch, delta in rules.get(phrase, {}).get("deltas", {}).items():
                if ch not in best or abs(delta) > abs(best[ch]):
                    best[ch] = delta

        for ch, delta in best.items():
            base = new_scores.get(ch)
            if base is None:
                # If the channel had no score, seed from a neutral midpoint so a
                # nudge still produces a visible, sensible value + arrow.
                base = 0.5
            val = max(0.0, min(1.0, base + delta))
            new_scores[ch] = val
            arrows[ch] = "up" if delta > 0 else "down"
        return new_scores, arrows, matched


# Convenience module-level singleton helpers (optional) ------------------------
_default_parser: Optional[HesitancyParser] = None


def get_parser(workspace_dir: Optional[str] = None) -> HesitancyParser:
    """Return a cached parser for `workspace_dir` (one per process by path)."""
    global _default_parser
    if _default_parser is None or _default_parser._workspace != workspace_dir:
        _default_parser = HesitancyParser(workspace_dir)
    return _default_parser


# Smoke test ------------------------------------------------------------------
if __name__ == "__main__":
    demo = HesitancyParser(hesitancy_path=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "hesitancy.txt"))
    cap = ("A photo-realistic shoot from a side camera angle about a woman, "
           "who appears to be smiling; the background is blurred and the image "
           "also shows high quality detail.")
    print("RAW   :", cap)
    print("CLEAN :", demo.clean_caption(cap))
    sc = {"score_overall": 0.62, "score_dof": 0.40, "score_content": 0.55}
    ns, ar, mp = demo.apply_score_modifiers(cap, sc)
    print("MATCH :", mp)
    print("SCORES:", sc, "->", ns)
    print("ARROWS:", ar)
    print("BOLD  :", demo.display_phrases(cap))

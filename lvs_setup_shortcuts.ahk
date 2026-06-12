; =============================================================================
;  LVS Selection Assist  —  FastStone Hotkey & Setup Helper
;  Version:    1.0.8
;  License:    AGPL-3.0-or-later
;  Developer:  ArcticWinter
;
;  v1.0.3 fixes
;  ------------
;   * JSON formatting: removed mistaken `{{ ... }}` double-brace escapes
;     in AHK v2 Format() — they were being emitted literally and breaking
;     the Python pipe parser.  Now uses straight string concatenation.
;   * Copy verification: extended polling window to 500 ms with 25 ms
;     ticks (was 200/20) and added a second post-window re-scan @ +250 ms
;     so a successful copy that completes just after the loop still
;     registers correctly.  This eliminates the "Copy did NOT register"
;     false positives the user observed in real cull sessions where the
;     files were on disk.
;   * Tooltip now uses ASCII "stars" (the Unicode ★ wasn't rendering in
;     the AHK ToolTip font).  Reads: "Now 54 photos rated 5 stars".
;   * Setup wording: replaced "close briefly" → "close" so it no longer
;     implies an automatic restart of FastStone (none happens).
;   * Reports both Registry and FSSettings.db status independently in
;     the completion MsgBox.
; =============================================================================

#Requires AutoHotkey v2.0
#SingleInstance Force
#MaxThreadsPerHotkey 1

global APP_NAME       := "LVS Selection Assist"
global APP_VERSION    := "1.0.8"
global SELECT_PREFIX  := "select"
global SELECT_COUNT   := 5
global C_DELAY        := 90     ; ms after `C` before sending the digit — the
                                ; Copy dialog needs time to paint + grab focus.
                                ; At 100 % zoom in fullscreen the dialog is
                                ; slower; 35 ms was too short → digit lost.
global DIGIT_DELAY    := 30     ; ms between digit and Enter
global VERIFY_TIMEOUT := 600    ; ms primary polling window
global VERIFY_TICK    := 25     ; ms between polls
global VERIFY_GRACE   := 300    ; ms additional re-scan after primary window
global PIPE_NAME      := "\\.\pipe\LVS_AHK_IPC"

; ============================================================================
;  Setup
; ============================================================================
RunSetup()

RunSetup() {
    global SELECT_PREFIX, SELECT_COUNT, APP_NAME, APP_VERSION

    baseDir := A_ScriptDir

    existing := []
    missing  := []
    Loop SELECT_COUNT {
        folder := baseDir . "\" . SELECT_PREFIX . A_Index
        if DirExist(folder)
            existing.Push(SELECT_PREFIX . A_Index)
        else
            missing.Push(SELECT_PREFIX . A_Index)
    }

    ; --- v1.0.6: suppress the setup wizard when the workspace is already
    ;     configured.  The MsgBox only appears if EITHER condition fails:
    ;       (a) all 5 select1..select5 folders exist, AND
    ;       (b) ingest.db is present (the DB is "set / pointing").
    ;     If both are satisfied the cull is ready to go, so we just (re)apply
    ;     the registry + FSSettings.db slots silently and exit — no prompt.
    dbPath := baseDir . "\ingest.db"
    allFolders := (missing.Length = 0)
    dbPresent := FileExist(dbPath) != ""
    if (allFolders && dbPresent) {
        ; Silent re-sync of FastStone Favorite Folders (registry only — this
        ; never needs FastStone closed).  We deliberately do NOT touch
        ; FSSettings.db here: patching it requires closing FastStone, which
        ; would be a surprising side effect for an already-configured user.
        ; (The full wizard still patches it when the user opts in.)
        regPaths := [
            "HKEY_CURRENT_USER\Software\FastStone\FSViewer",
            "HKEY_CURRENT_USER\Software\FastStone\FastStone Image Viewer"
        ]
        Loop SELECT_COUNT {
            idx := A_Index
            fullPath := baseDir . "\" . SELECT_PREFIX . idx
            for _, base in regPaths {
                try RegWrite(fullPath, "REG_SZ", base . "\FavoriteFolder" . idx)
            }
        }
        ; Only patch the Delphi FSSettings.db silently if FastStone is closed.
        if (WinExist("ahk_exe FSViewer.exe") = 0)
            PatchFSSettings(baseDir)
        return
    }

    statusLines := ""
    if (existing.Length > 0)
        statusLines .= "`n  Already present : " . StrJoin(existing, ", ")
    if (missing.Length > 0)
        statusLines .= "`n  Will be created : " . StrJoin(missing, ", ")
    else
        statusLines .= "`n  (all 5 select folders already exist)"

    fsRunning := WinExist("ahk_exe FSViewer.exe") != 0
    fsWarn := ""
    if (fsRunning)
        fsWarn := "`n`n*** FastStone is currently RUNNING. ***`n"
                . "Setup will close it before writing the Copy/Move slot file`n"
                . "(FSSettings.db).  Please save any pending FastStone work first."

    prompt := APP_NAME . "  Setup Helper  v" . APP_VERSION . "`n`n" .
              "Override FastStone's:`n" .
              "  * Favorite Folders 1..5  (Ctrl+1..5)`n" .
              "  * Copy/Move slots 1..5   (C/M dialog, 1..9 tab)`n`n" .
              "so they point at:`n" .
              "  " . baseDir . "\" . SELECT_PREFIX . "1 .. " . SELECT_PREFIX . SELECT_COUNT . "`n" .
              statusLines . fsWarn

    result := MsgBox(prompt, APP_NAME . " — Setup", "YesNo Icon?")
    if (result != "Yes")
        return

    ; --- 1. Create missing select folders ---------------------------------
    createdNow := []
    for _, name in missing {
        path := baseDir . "\" . name
        try {
            DirCreate(path)
            createdNow.Push(name)
        } catch as e {
            MsgBox("Could not create folder:`n" . path . "`n`n" . e.Message,
                   APP_NAME . " — Folder Error", "Icon!")
            return
        }
    }

    ; --- 2. Registry: BOTH known FastStone hives --------------------------
    regPaths := [
        "HKEY_CURRENT_USER\Software\FastStone\FSViewer",
        "HKEY_CURRENT_USER\Software\FastStone\FastStone Image Viewer"
    ]
    regErr := ""
    Loop SELECT_COUNT {
        idx := A_Index
        fullPath := baseDir . "\" . SELECT_PREFIX . idx
        for _, base in regPaths {
            try {
                RegWrite(fullPath, "REG_SZ", base . "\FavoriteFolder" . idx)
            } catch as e {
                regErr := e.Message
            }
        }
    }

    ; --- 3. Patch FSSettings.db via Python helper ------------------------
    if (fsRunning) {
        try {
            WinClose("ahk_exe FSViewer.exe")
            WinWaitClose("ahk_exe FSViewer.exe", , 3)
        } catch {
            ; ignore — patcher will warn if the file is locked
        }
    }

    fsdbErr := PatchFSSettings(baseDir)

    ; --- 4. Honest completion report -------------------------------------
    summary := APP_NAME . "  Setup Complete!`n`n" .
               "Base directory:`n  " . baseDir . "`n`n"
    if (createdNow.Length > 0)
        summary .= "Folders created : " . StrJoin(createdNow, ", ") . "`n"
    if (existing.Length > 0)
        summary .= "Already present : " . StrJoin(existing, ", ") . "`n"
    summary .= "`nRegistry favourites (Ctrl+1..5) : " . (regErr = "" ? "OK" : "FAILED — " . regErr) . "`n"
    summary .= "FSSettings.db slots (C + 1..5)  : " . (fsdbErr = "" ? "OK" : "FAILED — " . fsdbErr) . "`n"
    summary .= "`nIMPORTANT — in FastStone press F12 -> Viewer ->`n"
             . "enable 'Show full path in title bar' so the overlay can read it."

    MsgBox(summary, APP_NAME . " — Setup Result", "Iconi")
}

PatchFSSettings(baseDir) {
    ; Prefer the new split backend; fall back to the legacy single-file build.
    helper := baseDir . "\lvs_backend.py"
    if !FileExist(helper)
        helper := baseDir . "\lvs_overlay.py"
    if !FileExist(helper)
        return "lvs_backend.py / lvs_overlay.py not found"

    pyExe := "python"
    try {
        RunWait('cmd /c python --version >nul 2>&1', , "Hide")
    } catch {
        pyExe := "py -3"
    }

    tmpOut := A_Temp . "\lvs_fsdb_result.txt"
    try FileDelete(tmpOut)
    cmd := 'cmd /c ' . pyExe . ' "' . helper . '" --patch-fsdb > "' . tmpOut . '" 2>&1'
    try {
        RunWait(cmd, baseDir, "Hide")
    } catch as e {
        return "could not launch python: " . e.Message
    }

    if !FileExist(tmpOut)
        return "no output from python helper"
    out := FileRead(tmpOut)
    if InStr(out, "FSDB_OK")
        return ""
    return Trim(out)
}

; ============================================================================
;  Hotkeys
; ============================================================================
#HotIf WinActive("ahk_class FSViewer")
       or WinActive("ahk_class FastStoneImageViewerMainForm.UnicodeClass")
       or WinActive("ahk_class TFullScreenWindow")
       or WinActive("FastStone Image Viewer")

$1::CopyToSlot(1)
$2::CopyToSlot(2)
$3::CopyToSlot(3)
$4::CopyToSlot(4)
$5::CopyToSlot(5)

#HotIf

^+r::Reload     ; global reload

; ============================================================================
;  Functions
; ============================================================================
CountFiles(folder) {
    n := 0
    if !DirExist(folder)
        return 0
    Loop Files, folder . "\*.*", "F"
        n += 1
    return n
}

CopyToSlot(idx) {
    ; Block re-entry so a second hotkey press while the Copy dialog is
    ; painting cannot steal the digit and land both files in the wrong
    ; (last-used) slot.  With 100 % zoom in fullscreen the dialog paints
    ; slower; without this guard the digit got eaten by the viewer pane.
    Critical
    global SELECT_PREFIX, C_DELAY, DIGIT_DELAY, VERIFY_TIMEOUT, VERIFY_TICK, VERIFY_GRACE

    destFolder := A_ScriptDir . "\" . SELECT_PREFIX . idx

    fname := ""
    try {
        title := WinGetTitle("A")
        if RegExMatch(title, "(.+?)\s+-\s+FastStone Image Viewer", &m) {
            SplitPath(m[1], &fname)
        }
    } catch {
    }

    preCount := CountFiles(destFolder)

    ; Open the Copy/Move dialog and WAIT for it to actually take focus
    ; before sending the digit.  At fullscreen zoom the dialog can take
    ; 200+ ms to paint; sending the digit into void means it inherits the
    ; last-used slot (slot 4 for all subsequent copies → the user's bug).
    activeBefore := WinExist("A")
    SendInput("c")
    Loop 25 {
        Sleep(20)
        try if (WinExist("A") != activeBefore)
            break
    }
    Sleep(C_DELAY)

    SendInput("{" . idx . "}")
    Sleep(DIGIT_DELAY)
    SendInput("{Enter}")

    NotifyPipe('{"event":"hotkey_fired","slot":' . idx . '}')

    ; --- Primary polling window ----------------------------------------
    elapsed := 0
    postCount := preCount
    while (elapsed < VERIFY_TIMEOUT) {
        Sleep(VERIFY_TICK)
        elapsed += VERIFY_TICK
        postCount := CountFiles(destFolder)
        if (postCount > preCount)
            break
    }

    ; --- Grace re-scan -------------------------------------------------
    ; v1.0.3: in real-world cull sessions FastStone sometimes finalises
    ; the copy a hair after the primary window expires (large RAWs over
    ; slow USB, AV-scanner interception, etc.).  One additional grace
    ; check eliminates the false "copy failed" tooltips.
    if (postCount <= preCount && VERIFY_GRACE > 0) {
        Sleep(VERIFY_GRACE)
        postCount := CountFiles(destFolder)
    }

    if (postCount > preCount) {
        ToolTip("LVS  ->  Copied to " . SELECT_PREFIX . idx
              . "`nNow " . postCount . " photos rated " . idx . " stars")
        SetTimer(() => ToolTip(), -1800)
        NotifyPipe(BuildCopiedJson(idx, fname, preCount, postCount, elapsed))
    } else {
        ToolTip("LVS  WARN  Copy to " . SELECT_PREFIX . idx . " did NOT register"
              . "`n(check FastStone Copy/Move slot " . idx . ")")
        SetTimer(() => ToolTip(), -2400)
        NotifyPipe(BuildFailedJson(idx, fname, preCount, postCount))
    }
}

; --- JSON builders ---------------------------------------------------------
; v1.0.3: Hand-rolled string concatenation instead of Format("{{...}}")
; because AHK v2 doesn't strip braces from format strings the way some
; users expect.  This guarantees clean single-brace JSON to the pipe.
BuildCopiedJson(idx, fname, pre, post, ms) {
    return '{"event":"copied"'
         . ',"slot":' . idx
         . ',"filename":"' . JsonEscape(fname) . '"'
         . ',"pre":' . pre
         . ',"post":' . post
         . ',"delta":' . (post - pre)
         . ',"ms":' . ms
         . '}'
}

BuildFailedJson(idx, fname, pre, post) {
    return '{"event":"copy_failed"'
         . ',"slot":' . idx
         . ',"filename":"' . JsonEscape(fname) . '"'
         . ',"pre":' . pre
         . ',"post":' . post
         . '}'
}

NotifyPipe(jsonLine) {
    global PIPE_NAME
    GENERIC_WRITE := 0x40000000
    OPEN_EXISTING := 3
    INVALID_HANDLE_VALUE := -1
    hPipe := DllCall("CreateFileW",
        "WStr", PIPE_NAME,
        "UInt", GENERIC_WRITE,
        "UInt", 0,
        "Ptr", 0,
        "UInt", OPEN_EXISTING,
        "UInt", 0,
        "Ptr", 0,
        "Ptr")
    if (hPipe = INVALID_HANDLE_VALUE || hPipe = 0)
        return  ; LVS not running — silent ignore
    payload := jsonLine . "`n"
    bufSize := StrPut(payload, "UTF-8") - 1
    buf := Buffer(bufSize, 0)
    StrPut(payload, buf, "UTF-8")
    written := 0
    DllCall("WriteFile", "Ptr", hPipe, "Ptr", buf.Ptr, "UInt", bufSize,
                          "UInt*", &written, "Ptr", 0)
    DllCall("CloseHandle", "Ptr", hPipe)
}

JsonEscape(s) {
    if (s = "")
        return ""
    s := StrReplace(s, "\", "\\")
    s := StrReplace(s, '"', '\"')
    s := StrReplace(s, "`r", "\r")
    s := StrReplace(s, "`n", "\n")
    s := StrReplace(s, "`t", "\t")
    return s
}

StrJoin(arr, sep) {
    out := ""
    for i, v in arr
        out .= (i = 1 ? "" : sep) . v
    return out
}

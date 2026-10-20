"""The AppleScript a composition runs: the compose window's handlers, the
creation, paste, send and close blocks, and the read-back and
attachment checks.
"""

import re
from pathlib import Path
from typing import Literal

from .utils import escape_applescript_string, sanitize_input

# One-character body seed for a compose window made by `make new
# outgoing message`. Such a window with an EMPTY body had a WebArea that
# refused keyboard focus by every route tried (observed 2026-09-05; not
# reproduced since, see docs/research/paste-focus-failed.md, so it
# stays). The seed reaches neither a sent message nor a saved draft: the
# composition selects the whole body of a fresh message and deletes it
# before pasting (``PASTE_CARET_KEYS["replace"]``), empty body or not,
# because Mail keeps anything set through ``content`` inside a
# <blockquote type="cite">.
BODY_SEED = " "


# Where a clipboard paste lands in a compose window's body, as the keys
# pressed just before cmd+v. Measured through the loopback read-back on
# 2026-09-27 (docs/research/icloud-draft-resync.md, Observation 10):
#   above   - cmd+up: above what Mail wrote (a reply's quote, a forward's
#             header block and attachments), which stays as Mail made it.
#   replace - cmd+a, delete: the whole body. On a fresh message that is
#             the seed inside Mail's URLShare wrapper; pasting above it
#             instead sent an empty <blockquote type="cite">, which iOS
#             Mail draws as a purple quoted-reply bar.
#   end     - cmd+down: after everything, where the composition pastes
#             files once the body is in.
PastePlacement = Literal["above", "replace", "end"]


PASTE_CARET_KEYS: dict[PastePlacement, str] = {
    "above": "key code 126 using command down\n            delay 0.2",
    "replace": (
        'keystroke "a" using command down\n            delay 0.3\n'
        "            key code 51\n            delay 0.3"
    ),
    "end": "key code 125 using command down\n            delay 0.2",
}


CloseMode = Literal["save", "discard"]


CLOSE_FAILED: dict[CloseMode, str] = {
    "save": "SALVAGE_FAILED:",
    "discard": "DISCARD_FAILED:",
}


# How deep a compose body is read below its WebArea. A deeper element is
# not read, and the body counts as content (not provably empty).
BODY_LEVELS = 8


BODY_PATH = "UI element k of scroll area 1 of group 1 of group 1 of window i"


def body_level_lines() -> str:
    """One bulk read of roles and of values per level of the body, each
    written out from ``window i``, stopping at the first level with no
    element."""
    lines = []
    for level in range(1, BODY_LEVELS + 1):
        path = "UI elements of " * level + BODY_PATH
        lines.append(
            f"                set r to role of {path}\n"
            "                set end of levelRoles to r\n"
            f"                set end of levelValues to (value of {path})\n"
            "                if my tendLeafCount(r) is 0 then "
            "return my tendBodyResult(levelRoles, levelValues, false)"
        )
    return "\n".join(lines)


# Handlers the scripts that read or close compose windows share.
# Top-level AppleScript, so each script carries them after its own code.
WINDOW_HANDLERS = (
    """
on tendIsBlank(t)
    repeat with ch in (characters of t)
        if (id of ch) is not in {32, 9, 10, 13, 160} then return false
    end repeat
    return true
end tendIsBlank

-- Whether System Events' window i of Mail is a compose window: its
-- toolbar has a Send button.
on tendIsCompose(i)
    tell application "System Events"
        tell application process "Mail"
            try
                return exists (first button of (first toolbar of window i) whose description is "Send")
            end try
        end tell
    end tell
    return false
end tendIsCompose

-- The text-field values (To, Cc, any other header shown, Subject) of
-- window i, each read as text.
on tendFieldValues(i)
    tell application "System Events"
        tell application process "Mail"
            set roles to role of UI elements of window i
            set vals to value of UI elements of window i
        end tell
    end tell
    set fieldValues to {}
    repeat with j from 1 to (count of roles)
        if item j of roles is "AXTextField" then
            set v to item j of vals
            if v is missing value then set v to ""
            set end of fieldValues to (v as text)
        end if
    end repeat
    return fieldValues
end tendFieldValues

-- How many elements a nested read holds.
on tendLeafCount(x)
    if class of x is not list then return 1
    set n to 0
    repeat with y in x
        set n to n + (my tendLeafCount(contents of y))
    end repeat
    return n
end tendLeafCount

-- Whether one level of a body read holds anything but blank text and
-- groups: text, an attachment's button or image, or any other element.
on tendHasContent(rs, vs)
    if class of rs is list then
        repeat with j from 1 to (count of rs)
            set v to missing value
            if class of vs is list then
                if j is less than or equal to (count of vs) then set v to item j of vs
            end if
            if my tendHasContent(item j of rs, v) then return true
        end repeat
        return false
    end if
    if rs is "AXGroup" then return false
    if rs is "AXStaticText" then
        if vs is missing value then return false
        return not my tendIsBlank(vs as text)
    end if
    return true
end tendHasContent

-- "empty" when the levels read hold nothing but blank text and groups,
-- and the last held no element; otherwise "content". With the roles and
-- values read, level by level, for the caller's fingerprint.
on tendBodyResult(levelRoles, levelValues, truncated)
    set verdict to "empty"
    if truncated then set verdict to "content"
    repeat with L from 1 to (count of levelRoles)
        if my tendHasContent(item L of levelRoles, item L of levelValues) then set verdict to "content"
    end repeat
    return {bodyState:verdict, bodyRoles:levelRoles, bodyValues:levelValues}
end tendBodyResult

-- Window i's body, read below its WebArea; "unreadable" when the
-- WebArea cannot be found.
on tendBodyRead(i)
    set levelRoles to {}
    set levelValues to {}
    try
        tell application "System Events"
            tell application process "Mail"
                set saRoles to role of UI elements of scroll area 1 of group 1 of group 1 of window i
                set k to 0
                repeat with j from 1 to (count of saRoles)
                    if item j of saRoles is "AXWebArea" then
                        set k to j
                        exit repeat
                    end if
                end repeat
                if k is 0 then return {bodyState:"unreadable", bodyRoles:{}, bodyValues:{}}
"""
    + body_level_lines()
    + """
            end tell
        end tell
    on error
        return {bodyState:"unreadable", bodyRoles:{}, bodyValues:{}}
    end try
    return my tendBodyResult(levelRoles, levelValues, true)
end tendBodyRead

-- The index of the one window System Events lists with this name at
-- this position; 0 when none is, -1 when more than one is.
on tendIndexOf(nm, pos)
    tell application "System Events"
        tell application process "Mail"
            set allNames to name of windows
            set allPos to position of windows
        end tell
    end tell
    set found to 0
    repeat with j from 1 to (count of allNames)
        if (item j of allNames) is nm and (item j of allPos) is pos then
            if found is not 0 then return -1
            set found to j
        end if
    end repeat
    return found
end tendIndexOf

-- Click window i's close button, found by its subrole (`button 1` is
-- "add contacts" on a compose window). False when it has none.
on tendClickClose(i)
    tell application "System Events"
        tell application process "Mail"
            set subroles to subrole of buttons of window i
            repeat with k from 1 to (count of subroles)
                if item k of subroles is "AXCloseButton" then
                    click button k of window i
                    return true
                end if
            end repeat
        end tell
    end tell
    return false
end tendClickClose

-- Whether the window is gone: Mail's window wid, or, with no id, the
-- one window named nm.
on tendGone(wid, nm, pos)
    -- Mail can keep a window it closed in its list, neither visible nor
    -- miniaturized (a minimized one is miniaturized), long after System
    -- Events stops listing it (Observation 10). Closed: not listed, or
    -- listed so and no longer in System Events at its place.
    if wid is not 0 then
        tell application "Mail"
            if not (exists window id wid) then return true
            if (visible of window id wid) or (miniaturized of window id wid) then return false
        end tell
        return (my tendIndexOf(nm, pos)) is 0
    end if
    tell application "System Events"
        tell application process "Mail"
            return not (exists window nm)
        end tell
    end tell
end tendGone
"""
)


def build_creation_block(
    seed: str,
    seed_id_safe: str | None,
    reply_all: bool,
    subject_safe: str | None,
) -> str:
    """Per-seed AppleScript fragment that produces ``theMessage`` in a
    visible compose window, which is where every message the
    connector composes is written (``_compose``).

    A fresh message is ``make new outgoing message`` with
    ``visible:true``, its subject, and the one-space body seed
    (``BODY_SEED``), which the composition pastes over.

    A reply or forward is Mail's own verb (``reply`` / ``reply to
    all`` / ``forward``) with ``opening window true``, after the
    cross-account ``whose id is "{seed_id_safe}"`` lookup.
    ``seed_id_safe`` is expected to be Mail's internal id — callers
    route RFC 5322 ids through ``_maybe_resolve_rfc_seed_id`` first
    (#205). Opened without a window, what Mail wrote is not readable
    before send, and any edit of it through the dictionary replaced
    the quote or forwarded message and dropped a forward's
    attachments (measured 2026-09-26, see ``_compose``).
    """
    if seed == "new":
        return (
            f"set theMessage to make new outgoing message with properties "
            f'{{subject:"{subject_safe}", visible:true, content:"{BODY_SEED}"}}'
        )
    if seed == "reply":
        verb = "reply to all" if reply_all else "reply"
    else:  # forward
        verb = "forward"
    return f"""
            set origMsg to missing value
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        try
                            set origMsg to first message of mb whose id is "{seed_id_safe}"
                            exit repeat
                        end try
                    end repeat
                end try
                if origMsg is not missing value then exit repeat
            end repeat
            if origMsg is missing value then error "SEED_NOT_FOUND"
            set theMessage to {verb} origMsg opening window true
        """


def as_verified_send_block() -> str:
    """AppleScript fragment: verified Send click with mechanical
    read-back (Phase 0 of PLAN-html-reply-send, grounded in
    docs/reference/UI_GROUNDING_MAIL_SEND.md).

    Caller must set ``composeName``, the compose window's exact AX
    name, beforehand.

    The fragment sets ``sendOutcome`` to one of:
      "SENT" | "WINDOW_NOT_FOUND:…" | "NO_SEND_BUTTON:…" |
      "SEND_DISABLED:…" | "SHEET:…" | "WINDOW_STILL_OPEN:…"
    After the click it polls the window, once a second for 15 s: gone
    is SENT, since Mail closes the window when it takes the message;
    a sheet on it is SHEET:… with the sheet's text; still open with no
    sheet at the end is WINDOW_STILL_OPEN:…, not sent, which the
    caller's salvage saves to Drafts. It never returns early, so
    callers can run cleanup (e.g. clipboard restore) before returning
    ``sendOutcome``.

    Why each check exists (observed live, 2026-07-20, unless noted):
      - window resolved BY NAME — ``window 1`` may be the viewer;
      - ``enabled`` gate — clicking a disabled Send button is a silent
        no-op (the vanished-send mechanism);
      - sheet surfacing — a mid-send sheet means NOT dispatched; its
        static texts go into the outcome instead of a blind Cancel;
      - the window gone, and nothing about Sent. This block once also
        waited for a message with the subject in Sent. For a subject
        Sent already held, any earlier message satisfied that; for a
        new one, a copy slower than 15 s would make a message that
        went read as not sent, inviting a second send. That is read
        from the code (2026-09-27), not seen happen. Which copy the
        send filed is looked for afterwards, by identity
        (``_find_sent_copy``).
    """
    return """
        set sendOutcome to missing value
        set sendClicked to false
        set readyState to ""
        -- Precondition with bounded wait: after a reopen, Mail populates
        -- recipients asynchronously and Send stays disabled until then —
        -- clicking early is a silent no-op (the vanished-send mechanism).
        repeat 10 times
            set readyState to ""
            tell application "System Events"
                tell application process "Mail"
                    if not (exists window composeName) then
                        set readyState to "WINDOW_NOT_FOUND:" & ((name of windows) as text)
                    else
                        set tbBtns to (buttons of (first toolbar of window composeName) whose description is "Send")
                        if (count of tbBtns) is 0 then
                            set readyState to "NO_SEND_BUTTON:" & composeName
                        else
                            set sendBtn to item 1 of tbBtns
                            if not (enabled of sendBtn) then
                                set readyState to "SEND_DISABLED"
                            else
                                click sendBtn
                                set sendClicked to true
                            end if
                        end if
                    end if
                end tell
            end tell
            if sendClicked then exit repeat
            delay 1
        end repeat
        if not sendClicked then
            if readyState is "SEND_DISABLED" then
                set sendOutcome to "SEND_DISABLED:Send never became enabled within 10s on " & composeName & " (recipients missing or not yet populated)"
            else
                set sendOutcome to readyState
            end if
        end if
        if sendOutcome is missing value then
            repeat 15 times
                delay 1
                set winOpen to false
                set sheetInfo to ""
                tell application "System Events"
                    tell application process "Mail"
                        if exists window composeName then
                            set winOpen to true
                            if exists (first sheet of window composeName) then
                                try
                                    -- NB: "st" is a reserved AppleScript token; do not shorten this name.
                                    repeat with sheetTextEl in static texts of (first sheet of window composeName)
                                        set sheetInfo to sheetInfo & (value of sheetTextEl) & " | "
                                    end repeat
                                end try
                                set sheetInfo to "SHEET:" & sheetInfo
                            end if
                        end if
                    end tell
                end tell
                if sheetInfo is not "" then
                    set sendOutcome to sheetInfo
                    exit repeat
                end if
                if not winOpen then
                    set sendOutcome to "SENT"
                    exit repeat
                end if
            end repeat
            if sendOutcome is missing value then
                set sendOutcome to "WINDOW_STILL_OPEN:no sheet, and still open 15s after Send was clicked on " & composeName
            end if
        end if
        """


def as_new_compose_window_block() -> str:
    """AppleScript fragment: name the compose window just opened (by
    a reply or forward verb, or by ``make new outgoing message``),
    by comparing Mail's window names before and after, counting each
    name. A name already open does not hide a new window of the same
    name: a draft saved through the dictionary, as this connector's
    once were, leaves its window behind
    (docs/research/icloud-draft-resync.md, Observation 5), and a
    plain set difference then saw no new window at all (2026-09-26).

    Needs ``beforeNames`` (System Events' ``name of windows`` of Mail)
    set before the window is opened; sets ``newName``. Errors NO_COMPOSE_WINDOW
    when no window appeared within 5 s, and
    COMPOSE_WINDOW_NOT_UNIQUE when the new window shares its name
    with one already open: every later step (paste, read-back,
    verified send, discard, save) addresses the window by name, so it
    could act on the other one. That window is left open, not closed,
    since which of the two is the new one cannot be told by name.
    """
    return """
-- Identify the new compose window by comparing counted names (bounded poll).
set newName to ""
set sameNamed to 0
repeat 10 times
    delay 0.5
    tell application "System Events"
        tell application process "Mail"
            set afterNames to name of windows
        end tell
    end tell
    repeat with n in afterNames
        set nm to (n as text)
        set afterCount to 0
        repeat with m in afterNames
            if (m as text) is nm then set afterCount to afterCount + 1
        end repeat
        set beforeCount to 0
        repeat with m in beforeNames
            if (m as text) is nm then set beforeCount to beforeCount + 1
        end repeat
        if afterCount > beforeCount then
            set newName to nm
            set sameNamed to afterCount
            exit repeat
        end if
    end repeat
    if newName is not "" then exit repeat
end repeat
if newName is "" then error "NO_COMPOSE_WINDOW: no compose window appeared within 5 s"
if sameNamed > 1 then error "COMPOSE_WINDOW_NOT_UNIQUE: Mail opened a compose window named " & newName & " while another window of that name was open, so it cannot be addressed safely and was left open; close the other window of that name and retry"
"""


def paste_probe_strings(body: str, plain: bool = False) -> tuple[str, str]:
    """Compute the paste read-back probes for a body.

    Returns ``(snippet, raw_marker)``:
      - ``snippet``: first chunk of the TAG-STRIPPED text content —
        after a successful paste this text must be readable in the
        WebArea (arrival check). Empty when the HTML has no text
        content (checks degrade gracefully).
      - ``raw_marker``: first chunk of the raw source when it starts
        with a tag — if this appears LITERALLY in the WebArea, the
        paste degraded to plain text (the 2026-07-21 raw-``<p>``
        regression; see docs/reference/UI_GROUNDING_MAIL_SEND.md).

    A ``plain`` body is its own text: the snippet is the text itself
    and there is no raw marker, since angle brackets in it are text.
    """
    if plain:
        return " ".join(body.split())[:24].strip(), ""
    text = re.sub(r"<[^>]+>", " ", body)
    text = " ".join(text.split())
    snippet = text[:24].strip()
    raw_marker = body.strip()[:16] if body.lstrip().startswith("<") else ""
    return snippet, raw_marker


def text_paste_fill(body: str, plain: bool) -> str:
    """AppleScript that puts ``body`` on the pasteboard ``pb``: as
    HTML, or as plain text when ``plain``."""
    body_safe = escape_applescript_string(sanitize_input(body))
    flavor = "public.utf8-plain-text" if plain else "public.html"
    return (
        f'set theBody to "{body_safe}"\n'
        "set bodyNSString to current application's NSString's "
        "stringWithString:theBody\n"
        "set bodyData to bodyNSString's dataUsingEncoding:"
        "(current application's NSUTF8StringEncoding)\n"
        "pb's clearContents()\n"
        f'pb\'s setData:bodyData forType:"{flavor}"'
    )


def files_paste_fill(attachment_paths: list[Path]) -> str:
    """AppleScript that puts the files on the pasteboard ``pb`` as
    file URLs, which Mail pastes into a compose body as attachments,
    as it does a drag from Finder."""
    paths_safe = ", ".join(
        f'"{escape_applescript_string(str(Path(p).resolve()))}"' for p in attachment_paths
    )
    return (
        "set fileURLs to current application's NSMutableArray's array()\n"
        f"repeat with apath in {{{paths_safe}}}\n"
        "    (fileURLs's addObject:(current application's NSURL's "
        "fileURLWithPath:(apath as text)))\n"
        "end repeat\n"
        "pb's clearContents()\n"
        "pb's writeObjects:fileURLs"
    )


def build_paste_script(
    *,
    window_name: str,
    fill: str,
    placement: PastePlacement,
    undo_first: bool,
) -> str:
    """Full osascript source: paste into the named compose window's
    body whatever ``fill`` puts on the pasteboard (``text_paste_fill``
    or ``files_paste_fill``), at ``placement`` (``PASTE_CARET_KEYS``):

      1. readiness — poll until the body WebArea EXISTS (window-name
         alone races WebKit initialization);
      2. focus — ``set focused`` then VERIFY AXFocusedUIElement is the
         WebArea (a click can leave focus in the To field, sending
         cmd+v to the wrong control);
      3. caret, paste, then restore the clipboard IMMEDIATELY
         (shortest possible hold; restore also runs on every error
         path).

    Returns "PASTED_UNVERIFIED" — what the paste did is read back in
    a SEPARATE osascript run (``build_readback_script``,
    ``build_attachment_ax_verify_script``): within one process
    System Events serves a stale AX subtree after WebKit re-renders,
    so an in-script read-back sees nothing (observed live).
    ``undo_first`` prepends cmd+z for the retry attempt.
    """
    win_safe = escape_applescript_string(window_name)
    undo_block = 'keystroke "z" using command down\n            delay 0.5' if undo_first else ""
    caret_keys = PASTE_CARET_KEYS[placement]
    return f"""
use framework "AppKit"
use framework "Foundation"
use scripting additions

set composeName to "{win_safe}"

set pb to current application's NSPasteboard's generalPasteboard()
set savedTypes to (pb's types()) as list
set savedPairs to {{}}
repeat with t in savedTypes
    set theData to (pb's dataForType:(t as text))
    if theData is not missing value then
        set end of savedPairs to {{pbType:(t as text), pbData:theData}}
    end if
end repeat

{fill}

try
    tell application "Mail" to activate
    set bodyArea to missing value
    repeat 15 times
        tell application "System Events"
            tell application process "Mail"
                if exists window composeName then
                    set w to window composeName
                    repeat with g in groups of w
                        try
                            set sa to scroll area 1 of group 1 of g
                            set waList to (UI elements of sa whose role is "AXWebArea")
                            if (count of waList) > 0 then
                                set bodyArea to item 1 of waList
                                exit repeat
                            end if
                        end try
                    end repeat
                end if
            end tell
        end tell
        if bodyArea is not missing value then exit repeat
        delay 0.5
    end repeat
    if bodyArea is missing value then error "NO_BODY_AREA:webarea never appeared in " & composeName
    set focusOK to false
    tell application "System Events"
        tell application process "Mail"
            if exists menu item "Make Rich Text" of menu "Format" of menu bar 1 then
                click menu item "Make Rich Text" of menu "Format" of menu bar 1
                delay 0.3
            end if
            repeat 5 times
                set focused of bodyArea to true
                delay 0.3
                try
                    if role of (value of attribute "AXFocusedUIElement" of it) is "AXWebArea" then
                        set focusOK to true
                        exit repeat
                    end if
                end try
                click bodyArea
                delay 0.3
            end repeat
        end tell
    end tell
    if not focusOK then error "PASTE_FOCUS_FAILED:body area would not take keyboard focus in " & composeName
    tell application "System Events"
        tell application process "Mail"
            {undo_block}
            {caret_keys}
            keystroke "v" using command down
            delay 0.8
        end tell
    end tell
on error errText
    pb's clearContents()
    repeat with pair in savedPairs
        pb's setData:(pbData of pair) forType:(pbType of pair)
    end repeat
    return errText
end try

pb's clearContents()
repeat with pair in savedPairs
    pb's setData:(pbData of pair) forType:(pbType of pair)
end repeat

return "PASTED_UNVERIFIED"
"""


def as_close_compose_block(mode: CloseMode, *, require_empty: bool = False) -> str:
    """AppleScript fragment: close one compose window, saving it as a
    draft (``mode`` "save") or not ("discard"), and read back that it
    closed. Needs ``closeId`` (Mail's id for the window, 0 when there
    is none) and ``closeName`` (its name) set before. Sets
    ``closeOutcome`` to "SALVAGED" (with a note on Mail's send-error
    sheet when there was one) or "DISCARDED"; "NO_WINDOW" when it was
    gone already; or "SALVAGE_FAILED:…" / "DISCARD_FAILED:…", with
    nothing closed unless it says so.

    By id, the window is exactly Mail's window ``closeId``, still
    named ``closeName``, found in System Events' list by its name and
    position (``tendIndexOf``) however many windows share its name,
    and closed only when no other visible window of its name stands
    in the same place; it is verified closed by that id being gone.
    With no id, only when ``closeName`` is the only window of its
    name: a window addressed by name is the first of the name, which
    could be someone else's.

    Every window reference is an index written out from ``window i``
    (Observation 7). The close button and the sheet go through Mail's
    UI: both dictionary discards (``close … saving no``, ``delete
    outgoing message``) fail silently (observed live 2026-07-20). The
    "Don’t Save" button's name carries a curly apostrophe (U+2019).

    ``require_empty`` (a discard by tending): the window must still
    read provably empty, every header field blank, the body empty and
    no sheet, in this script, right before the click, so nothing typed
    into it since it was read is thrown away.

    A send Mail could not make through the account's server leaves a
    sheet on the window, "Cannot send message using the server …",
    whose buttons are Try Later, Try With Selected Server, Connection
    Doctor, Edit SMTP Server List and Edit Message (seen 2026-09-27),
    and no Save. On a save, found (by its Edit Message button), its
    text is read, Edit Message pressed, and the window then closed
    with Save as any other; the outcome carries the text, as
    "(Mail's send-error sheet: …)", so the caller's error says why
    Mail did not send. It cannot be provoked on demand, and no live
    test has met it.
    """
    failed = CLOSE_FAILED[mode]
    done = "SALVAGED" if mode == "save" else "DISCARDED"
    sheet_button = "Save" if mode == "save" else "Don’t Save"
    empty_check = (
        f"""
if closeOutcome is "" then
    set fieldValues to my tendFieldValues(closeIdx)
    set stillEmpty to ((count of fieldValues) > 0)
    repeat with v in fieldValues
        if not my tendIsBlank(contents of v) then set stillEmpty to false
    end repeat
    tell application "System Events"
        tell application process "Mail"
            if exists sheet 1 of window closeIdx then set stillEmpty to false
        end tell
    end tell
    if stillEmpty then set stillEmpty to ((bodyState of (my tendBodyRead(closeIdx))) is "empty")
    if not stillEmpty then set closeOutcome to "{failed}no longer empty; left open"
end if
"""
        if require_empty
        else ""
    )
    send_error_sheet = (
        f"""
if closeOutcome is "" then
    tell application "System Events"
        tell application process "Mail"
            if exists sheet 1 of window closeIdx then
                if exists button "Edit Message" of sheet 1 of window closeIdx then
                    set sheetText to ""
                    try
                        repeat with sheetVal in (value of static texts of sheet 1 of window closeIdx)
                            set sheetText to sheetText & ((contents of sheetVal) as text) & " | "
                        end repeat
                    end try
                    set sheetNote to " (Mail's send-error sheet: " & sheetText & ")"
                    click button "Edit Message" of sheet 1 of window closeIdx
                end if
            end if
        end tell
    end tell
    if sheetNote is not "" then
        repeat 10 times
            delay 0.3
            set closeIdx to my tendIndexOf(closeName, closePos)
            if closeIdx is less than or equal to 0 then exit repeat
            tell application "System Events"
                tell application process "Mail"
                    set sheetUp to exists sheet 1 of window closeIdx
                end tell
            end tell
            if not sheetUp then exit repeat
        end repeat
        if closeIdx is less than or equal to 0 then set closeOutcome to "{failed}the window was lost after its send-error sheet" & sheetNote
    end if
end if
"""
        if mode == "save"
        else ""
    )
    return f"""
set closeOutcome to ""
set closePos to {{}}
set closeIdx to 0
set sheetNote to ""
if closeId is not 0 then
    set idName to ""
    tell application "Mail"
        try
            set idName to name of window id closeId
            set idBounds to bounds of window id closeId
        on error
            set closeOutcome to "NO_WINDOW"
        end try
    end tell
    if closeOutcome is "" and idName is not closeName then set closeOutcome to "{failed}Mail's window " & closeId & " is now named " & idName & "; left open"
    if closeOutcome is "" then
        set closePos to {{item 1 of idBounds, item 2 of idBounds}}
        tell application "Mail"
            set allNames to name of every window
            set allBounds to bounds of every window
            set allVisible to visible of every window
        end tell
        set twins to 0
        repeat with j from 1 to (count of allNames)
            if (item j of allVisible) and (item j of allNames) is closeName then
                set b to item j of allBounds
                -- Assigned before it is compared: a list literal of
                -- `item n of b` compared with `is` is false (Observation 9).
                set bPos to {{item 1 of b, item 2 of b}}
                if bPos is closePos then set twins to twins + 1
            end if
        end repeat
        if twins is not 1 then set closeOutcome to "{failed}" & twins & " visible windows named " & closeName & " stand where Mail's window " & closeId & " does, so which to close cannot be told; none was closed"
    end if
else
    tell application "System Events"
        tell application process "Mail"
            set nameCount to count of (windows whose name is closeName)
            set allNames to name of windows
            set allPos to position of windows
        end tell
    end tell
    if nameCount is 0 then
        set closeOutcome to "NO_WINDOW"
    else if nameCount > 1 then
        set closeOutcome to "{failed}" & nameCount & " windows are named " & closeName & ", so which to close cannot be told by name; none was closed"
    else
        repeat with j from 1 to (count of allNames)
            if (item j of allNames) is closeName then set closePos to item j of allPos
        end repeat
    end if
end if
if closeOutcome is "" then
    set closeIdx to my tendIndexOf(closeName, closePos)
    if closeIdx is 0 then set closeOutcome to "{failed}System Events lists no window named " & closeName & " in its place; none was closed"
    if closeIdx < 0 then set closeOutcome to "{failed}System Events lists more than one window named " & closeName & " in its place; none was closed"
end if
{empty_check}{send_error_sheet}
if closeOutcome is "" then
    try
        if not my tendClickClose(closeIdx) then error "the window has no close button"
        delay 0.8
        if not my tendGone(closeId, closeName, closePos) then
            set closeIdx to my tendIndexOf(closeName, closePos)
            if closeIdx > 0 then
                tell application "System Events"
                    tell application process "Mail"
                        if exists sheet 1 of window closeIdx then
                            click button "{sheet_button}" of sheet 1 of window closeIdx
                            delay 0.8
                        end if
                    end tell
                end tell
            end if
        end if
        if my tendGone(closeId, closeName, closePos) then
            set closeOutcome to "{done}" & sheetNote
        else
            set closeOutcome to "{failed}window still open" & sheetNote
        end if
    on error errMsg
        set closeOutcome to "{failed}" & errMsg & sheetNote
    end try
end if
"""


def build_readback_script(window_name: str) -> str:
    """Full osascript source: read the compose body's text content.

    Runs as its OWN osascript process — a fresh process gets a fresh
    AX snapshot; the pasting process reads a stale (empty) subtree
    after WebKit re-renders on paste (observed live 2026-07-22).
    Returns the concatenated static-text content, or
    "READBACK_NO_AREA" when the window/WebArea can't be resolved.
    """
    win_safe = escape_applescript_string(window_name)
    return f"""
tell application "System Events"
    tell application process "Mail"
        if not (exists window "{win_safe}") then return "READBACK_NO_AREA"
        set bodyArea to missing value
        repeat with g in groups of window "{win_safe}"
            try
                set sa to scroll area 1 of group 1 of g
                set waList to (UI elements of sa whose role is "AXWebArea")
                if (count of waList) > 0 then
                    set bodyArea to item 1 of waList
                    exit repeat
                end if
            end try
        end repeat
        if bodyArea is missing value then return "READBACK_NO_AREA"
        -- Pasted HTML nests as AXGroup > AXStaticText (typed text sits
        -- directly under the WebArea) — descend three levels.
        set seenText to ""
        try
            repeat with l1 in UI elements of bodyArea
                if role of l1 is "AXStaticText" then
                    set seenText to seenText & (value of l1) & " "
                else
                    try
                        repeat with l2 in UI elements of l1
                            if role of l2 is "AXStaticText" then
                                set seenText to seenText & (value of l2) & " "
                            else
                                try
                                    repeat with l3 in UI elements of l2
                                        if role of l3 is "AXStaticText" then set seenText to seenText & (value of l3) & " "
                                    end repeat
                                end try
                            end if
                        end repeat
                    end try
                end if
            end repeat
        end try
        return seenText
    end tell
end tell
"""


def build_attachment_ax_verify_script(
    *,
    window_name: str,
    filenames: list[str],
) -> str:
    """AppleScript: poll the compose window until every attachment
    shows up in the body WebArea as an element whose description
    carries the filename, or time out. Returns "ATTACHMENTS_VERIFIED"
    or "ATTACH_MISSING:<filename>".

    Mail shows a file one of two ways, whether it was attached
    through the dictionary or pasted: an image inline, as an AXImage
    described by its file name, and anything else as an AXButton
    described "name.ext, N KB" (both measured 2026-09-27,
    docs/research/icloud-draft-resync.md, Observation 10; the button
    form first live 2026-08-24). Either role is accepted.

    Walks the same groups → scroll area → AXWebArea path as the
    paste script and descends recursively from there. NEVER uses
    ``entire contents`` — it silently returns an empty list on
    compose windows more often than not (observed live 2026-08-24;
    one lucky success during exploration, empty on every retry).
    The WebArea is re-located on every poll: WebKit re-renders
    invalidate stale element references."""
    win_safe = escape_applescript_string(window_name)
    names_safe = ", ".join(f'"{escape_applescript_string(n)}"' for n in filenames)
    return f"""
on searchAttachment(el, fname, depthLeft)
    tell application "System Events"
        try
            set r to role of el
            if (r is "AXButton" or r is "AXImage") and ((description of el) as text) contains fname then return true
        end try
        if depthLeft > 0 then
            try
                repeat with c in UI elements of el
                    if my searchAttachment(c, fname, depthLeft - 1) then return true
                end repeat
            end try
        end if
        return false
    end tell
end searchAttachment

tell application "Mail" to activate
delay 0.3
set missingName to ""
repeat with fname in {{{names_safe}}}
    set foundIt to false
    repeat 10 times
        tell application "System Events"
            tell application process "Mail"
                if exists window "{win_safe}" then
                    set bodyArea to missing value
                    repeat with g in groups of window "{win_safe}"
                        try
                            set sa to scroll area 1 of group 1 of g
                            set waList to (UI elements of sa whose role is "AXWebArea")
                            if (count of waList) > 0 then
                                set bodyArea to item 1 of waList
                                exit repeat
                            end if
                        end try
                    end repeat
                    if bodyArea is not missing value then
                        set foundIt to my searchAttachment(bodyArea, fname as text, 6)
                    end if
                end if
            end tell
        end tell
        if foundIt then exit repeat
        delay 0.5
    end repeat
    if not foundIt then
        set missingName to fname as text
        exit repeat
    end if
end repeat
if missingName is "" then
    return "ATTACHMENTS_VERIFIED"
else
    return "ATTACH_MISSING:" & missingName
end if
"""


def recipient_override_block(kind: str, addrs: list[str] | None) -> str:
    """AppleScript that clears and re-populates one recipient group of
    ``theMessage``: None keeps what Mail derived (``""``), ``[]``
    clears it, a list replaces it."""
    if addrs is None:
        return ""
    list_str = ", ".join(f'"{escape_applescript_string(sanitize_input(a))}"' for a in addrs)
    return f"""
                delete (every {kind} recipient of theMessage)
                repeat with addr in {{{list_str}}}
                    make new {kind} recipient at end of {kind} recipients of theMessage with properties {{address:addr}}
                end repeat
            """

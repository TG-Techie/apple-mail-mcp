# Mail.app UI Grounding — Send Path

Empirically mapped 2026-07-20 via System Events (osascript) with computer-use
screenshot cross-confirmation. macOS Darwin 25.5.0. Every fact below was read
from the live UI, not assumed. Purpose: ground the `_send_new_via_eml` /
`_send_html_email` hardening so every UI action has a **mechanical read-back**
(scripted verification inside the AppleScript itself — never AI judgment).

## Resting state

- Mail runs with one **viewer window**; its AX `name` is the mailbox line,
  e.g. `All Sent – 145 messages` (en dash, U+2013).
- Viewer toolbar contains **no button with description "Send"**. Top level:
  `Sidebar`, `Filter` (checkbox), `View Options` (menu), `New Message`,
  `Move` (menu), plus groups: [`Reply`, `Reply All`, `Forward`],
  [`Archive`, `Delete`, `Junk`], [`Flag`], [`Search`].
- Consequence: a Send-click script that targets the wrong (viewer) window
  **throws loudly** (`Can't get button "Send"`), it does not misfire.

## Compose window

- **Findable by exact AX name = subject** (empty subject → `New Message`).
- Toolbar (in order): `Reply` (disabled), `Format`, `Emoji & Symbols`,
  `Header Fields` (menu), `Attach` (menu), `Send Later` (menu, disabled
  when Send is disabled), **`Send` (AXButton)**.
- **`Send` is `enabled=false` until a valid recipient exists.** A System
  Events `click` on a disabled button is a **silent no-op** — no error, no
  dispatch. This is the leading explanation for the 2026-07-20 vanished
  send: click fired before recipients populated → nothing happened → script
  returned `"SENT"` anyway.
- Fields: three `AXTextField`s in DOM order **To, Cc, Subject**, labeled by
  sibling `AXStaticText`s `To:` / `Cc:` / `Subject:` / `From:`. Values are
  mechanically readable (`value of text field N`) — verified live by reading
  the probe subject back. Use this for post-type/post-paste read-back.
- **From is an `AXPopUpButton`** with a readable value (verified against
  the live sending account's address). It is mechanically settable ⇒ the
  `from_account`-is-ignored limitation of the mailto path is fixable via UI.
- Body nesting: `AXGroup > AXScrollArea > AXWebArea (> AXGroup)`. WebArea
  content is readable ⇒ paste read-back is possible for HTML injection.

## Sheets

- Closing an unsaved compose window raises sheet **"Save this message as a
  draft?"** with buttons `Save` / `Don’t Save` / `Cancel`.
  - **`Don’t Save` contains a curly apostrophe (U+2019).** A straight-quote
    `"Don't Save"` match fails. There is **no** "Delete Draft" button (an
    assumed name that ground truth killed).
  - The existing code's `click button "Cancel" of (first sheet of w)` keeps
    the compose window open — it does not discard.

## Silent-failure modes observed live (why read-back is mandatory)

| Action | Result | Error raised? |
|---|---|---|
| `close window <compose> saving no` (Mail dictionary) | window stays open | **no** |
| `close targetWin saving yes` (Mail dictionary) | window stays open; a `<subject> — All Drafts` viewer window appears on later `open <draft>`; the original window's Send button never re-enables | **no** |
| `delete first outgoing message` | window stays open | **no** |
| `click` on a disabled button | nothing happens | **no** (standard AX) |
| `click button 1 of <compose window>` intending the close button | clicks **"add contacts"** (button 1 ≠ close; use `subrole "AXCloseButton"`) | **no** |
| `click button "<wrong name>"` | nothing | yes (−1728, loud — but only if the name is exact incl. Unicode punctuation) |

### Paste read-back findings (2026-07-22, the raw-`<p>` regression)

- A click on the body WebArea can leave keyboard focus in the **To field**
  (`AXFocusedUIElement` = AXTextField) — `keystroke "v"` then pastes into
  the wrong control. Mechanical fix: `set focused of bodyArea to true`,
  then VERIFY the focused element's role is AXWebArea.
- **Same-process AX reads after a WebKit re-render are stale**: the
  process that pastes reads an empty subtree even after re-resolving the
  element. Content verification must run in a **separate osascript
  process** (fresh AX snapshot).
- AX structure differs by input route: **typed** text sits directly under
  the WebArea as AXStaticText; **pasted HTML** nests as
  `AXGroup > AXStaticText` (descend when reading).
- Styled runs (`<b>…</b>`) split into multiple AXStaticTexts — normalize
  whitespace before comparing read-back text to expectations.
- The compose window's Format state is mechanically checkable: menu shows
  `Make Plain Text` when already rich / `Make Rich Text` when plain —
  check `exists` instead of blind-clicking in a `try`.

Also: `st` is a **reserved AppleScript token** (`set st to 1` is a syntax
error) — loop variables named `st` fail to compile. And after any compose
open/reopen, the Send button enables **asynchronously** — the verified-send
primitive waits (bounded) for `enabled` before clicking. These findings were
confirmed by the Phase-0 integration tests (`tests/integration/test_verified_send.py`),
which drove the mailto path to direct-send-from-the-live-window (the
close/reopen dance was the vanished-send mechanism).

The only reliable way found to discard a compose window: click its close
button, wait for the save sheet, click `Don’t Save` (U+2019), then **verify
the window is gone**.

## Verified send postconditions (from the successful 18:12 send)

1. The compose window (identified by name) disappears.
2. The message appears in the sending account's Sent mailbox (iCloud:
   "Sent Messages"; appeared within seconds — poll with the existing
   15×1s idiom).
3. The source draft is removed from Drafts.

## Mechanical read-back recipe for any UI send

```
PRECONDITIONS (verify, don't assume):
  - window with exact expected name exists          → else fail: list window names
  - button "Send" of its toolbar exists             → else fail: list toolbar descs
  - enabled of that button is true                  → else fail: "Send disabled (no recipient?)"
  - if recipients were set: value of To field
    read back equals what was written               → else fail with actual value
ACT:
  - click the Send button (resolved by reference, never `window 1`)
POSTCONDITIONS (poll up to ~15s):
  - window with that name no longer exists
  - message with that subject present in Sent mailbox
  - if a sheet appeared instead: return its static-text
    contents in the error — never Cancel-and-continue blindly
Return SENT only when all postconditions hold; anything else is a loud,
descriptive error and the draft is left recoverable.
```

Screenshot cross-check: the computer-use screenshot of the resting state
matched the AX enumeration exactly (window title, toolbar contents, no
compose windows, no sheets).

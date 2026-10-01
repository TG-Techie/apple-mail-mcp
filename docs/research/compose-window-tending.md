# Tending Mail's compose windows (observations, and the rule)

Recorded 2026-09-27 against the live machine, and extended on
2026-10-01 (Observations 7–10). The 2026-10-01 work corrects
Observations 2 and 5 and replaces the rule. Format follows
`docs/research/attachment-property-10000.md`: observations with the
command that produced them first, derivations labeled and secondary. The
2026-09-27 measurement (Observations 1–6) was read-only: nothing was
clicked, focused, closed or typed into. The rule and the code came
after it.

Environment: macOS Darwin 25.5.0. Mail.app had hung and been relaunched
at 07:40:04 local (`ps -o lstart= -p <Mail pid>`), with the operator's
approval. At the relaunch it restored the compose windows below, left
behind over earlier days by earlier code paths (mailto and dictionary
composes, failed salvages). The operator, 2026-09-27 07:39:51, approving
the relaunch: "the mail app regularly has been accumulating failed
windows so the MCP should periodically tend it reguardless". The
constraint from his channel holder: never close a window that is not the
MCP's without a rule for telling them apart. A person uses this Mail too,
and other clients drive it.

In this note a window with a real subject is described by its shape (a
reply to an automated notification, a forward), never by its subject.

## Observation 1 — what System Events lists

```
tell application "System Events" to tell application process "Mail"
    repeat with w in windows
        -- name, subrole, AXMinimized, exists (first sheet of w),
        -- (count of (buttons of (first toolbar of w) whose description is "Send")) > 0,
        -- position and size
```

08:19–08:22, and again at 08:36 and 09:02: 27 windows. 25 have a Send
button in their toolbar (the compose windows). The other two: the
viewer ("All Sent – N messages") and "Mail Connection Doctor", which
another session opened while diagnosing Mail's fetch (by its account),
and which was open in every listing taken. All 25 compose windows:
AXStandardWindow, not minimised, no
sheet, on screen in cascaded positions; the Mail process visible and
frontmost.

Their names: 18 "New Message"; 2 identical "Re: …" replies to an
automated repository notification; 4 identical "Re: …" replies to an
automated site-check alert; 1 "Fwd: …" forward.

## Observation 2 — each window's fields and body

Corrected 2026-10-01: this walk held each window in a variable, which
System Events makes a reference by name (Observation 7). Each row for a
shared name is therefore the first window of that name, read once per
window. Observation 8 has the windows read one by one.

A scratch script walked each compose window, addressed as `window i`
(Observation 5 says why), reading each child's role and value, the
To/Cc/Bcc and Subject text fields by their `To:` / `Cc:` / `Bcc:` /
`Subject:` labels, and the body at
`group 1 > group 1 > scroll area 1 > AXWebArea`, descending up to eight
levels for AXStaticText values and for AXButton / AXImage (how Mail
shows an attachment, see `_build_attachment_ax_verify_script`). 08:28,
repeated 08:30 with the same result:

| Shape | Count | To tokens | Cc | Bcc field | Subject field | Body (AX text) | Attachments |
|---|---|---|---|---|---|---|---|
| "New Message" | 18 | 0 | 0 | not shown | empty | WebArea with no children | 0 |
| reply to an automated repository notification | 2 | 1 | 0 | not shown | 105 chars, Mail's "Re: …" | 25 chars: a marker of the form the integration suite's `TEST_DRAFT_SUBJECT_PREFIX` starts, no quote | 0 |
| reply to an automated site-check alert | 4 | 1 | 0 | not shown | 51 chars, Mail's "Re: …" | 831 chars, starting with Mail's "On …, … wrote:" quote header, nothing above it | 0 |
| forward | 1 | 1 | 0 | not shown | 30 chars, Mail's "Fwd: …" | 476 chars, starting "Begin forwarded message:" | 0 |

The From pop-up named one of the machine's two sending accounts on 22
windows and the other on 3 (both repository-notification replies and one
"New Message").

A recipient shows in its field as one U+FFFC per token; the token is a
child AXTextField ("attached text") whose value is the display name, not
the address.

## Observation 3 — what Mail says about a window: an id, no age, no origin

```
tell application "Mail" to repeat with w in windows
    -- id of w, index of w, name of w, visible of w, miniaturized of w
tell application "Mail" to get properties of window id <a compose window>
```

- Every window has an integer `id`. The viewer and the 25 compose
  windows carry 2755–2781, one block; windows opened later carry higher
  ids (a hidden "iCloud Mail Cleanup" window 2807, the Connection
  Doctor 2880). The ids were the same at 08:24 and 08:28.
- `properties of window id 2781`: zoomable, closeable, zoomed, class,
  index, visible, name, miniaturizable, id, miniaturized, resizable,
  bounds, document (`missing value`). No date of any kind.
- A compose window's AX attributes: AXFocused, AXFullScreen, AXTitle,
  AXChildrenInNavigationOrder, AXFrame, AXPosition, AXGrowArea,
  AXMinimizeButton, AXDocument, AXSections, AXCloseButton, AXMain,
  AXActivationPoint, AXFullScreenButton, AXProxy, AXDefaultButton,
  AXMinimized, AXChildren, AXRole, AXParent, AXTitleUIElement,
  AXCancelButton, AXModal, AXSubrole, AXZoomButton, AXRoleDescription,
  AXSize, AXToolbarButton, AXIdentifier. AXDocument is `missing value`;
  AXIdentifier is `_NS:41` on every compose window.
- Mail's `bounds` top-left equals System Events' `position` (449,232
  for window id 2781).
- `id of every window whose name is "New Message"` returned the 18 ids;
  System Events' `count of (windows whose name is "New Message")`
  returned 18.
- Mail's sandboxed saved-state directory
  (`~/Library/Containers/com.apple.mail/Data/Library/Saved Application State/`)
  was empty. Where Mail keeps the state it restores windows from was not
  looked for further.

## Observation 4 — `outgoing messages`

`count of outgoing messages`: 0 at 08:19:10, with the 25 windows open.
1 at 08:28:37, 3 from 08:30 through 09:02, each `visible false` with one
To recipient; no window listing showed a new window for them.

## Observation 5 — addressing windows through `every` fails on some

A walk written `repeat with w in windows … repeat with g in groups of w
… scroll area 1 of group 1 of g` failed on the same 7 of the 25 windows
in three runs (08:20, 08:21, 08:23): "Can't get group 1 of item 1 of
every group of item N of every window of application process "Mail".
Invalid index." The same lookup on the same windows succeeded addressed
as `window N`, both in a fresh osascript per window and in one osascript
with `set w to window i` (08:28, all 25 read; but each such read was
of the first window of its name, Observation 7).

## Observation 6 — what one read of all the windows costs

Reading each window's children's roles and values in one event each,
and its body only when every header field is blank: 6.5 s wall for the
25 windows (08:30, scratch script), 5.4 s through the connector's
inventory script (`_build_compose_inventory_script`, 08:51). The
connector's Mail lock is held for that long.

## Derivations (labeled — secondary to the above)

1. **Provably empty: the 18 "New Message" windows.** No recipient token
   or text in any field, an empty subject, a body with no element at
   all, no attachment, no sheet (Observations 1, 2). Closing one loses
   nothing, whoever opened it. The other 7 carry content. *Superseded
   by Derivation 7: read one by one, 16 are empty, and 2 have no body
   where the others do.*
2. **Provably the MCP's: none.** Mail exposes no age and no origin for a
   window (Observation 3): the id is unique within one Mail process and
   grows with creation, and the restored windows got theirs at the
   relaunch, so it tells "restored" from "opened since", never how old a
   composition is or who opened it. The two repository-notification
   replies carry an integration-test marker as their whole body, so an
   integration run almost certainly opened them; that is read from their
   content, which no rule should rest on.
3. **Nothing tells an empty restored window from one a person has just
   opened**: same name, same empty fields, same empty body, no age.
4. **Names are not identity.** 18 windows share one name and 4 another.
   Every close this connector did then addressed a window by name (the
   discard and salvage blocks), so with two of a name it could close
   the other one. Since 2026-10-01 every close is addressed by id
   ("Closing loses nothing, and closes exactly one window"). Mail's window `id` is identity, within one
   Mail process: a relaunched Mail gives the windows it restores new
   ids.
5. **`outgoing messages` does not see these windows** (Observation 4):
   0 with all 25 open. The outgoing messages it later listed were
   windowless ones some other client made during the measurement.
6. **Windows are addressed by index in one script**, never through a
   nested `every` reference (Observation 5).

## Observations 7–10, 2026-10-01

Taken 13:30–14:00 local against the live Mail, which listed 26 windows
in System Events: the same 25 compose windows (by shape) as on
2026-09-27 and the viewer. Their Mail ids were now 3896–3922, so Mail
had restored them again at a later launch. Observations 7, 8 and the
first half of 10 were read-only. Observations 9 and 10's closes were
made only on windows the probe or test had opened itself, with the
`ZZZ-AMM-INTEG` prefix. Probes are scratch scripts run through the
connector's `_run_applescript`. Names are printed as shapes only.

## Observation 7 — a System Events window held in a variable is a reference by name

13:30. The probe read each window's position three ways in one script:
`position of windows` (bulk), `position of window i` written inline,
and `set w to window i` followed by `position of w`.

- Bulk and inline gave 25 distinct positions, one per window.
- Through the variable, every "New Message" window gave [275, 246], the
  position of the first window of that name. Every "Re:<51>" window gave
  [217, 188], again the first of its name.
- `(window 3) as text` fails with the reference in its message:
  `Can't make window "<11 chars>" of «class pcap» "<4 chars>" of
  application "<13 chars>" into type text`. That is a window named
  "New Message", addressed by name, not by index.

## Observation 8 — the 25 windows read by index alone, and what they hold

The inventory script was rewritten to name `window i` inline in every
specifier, with the body read level by level in one event per level
(`role of UI elements of … of UI element k of scroll area 1 of group 1
of group 1 of window i`, eight levels).

- 13:32, a six-level probe: 18 "New Message" windows. 16 read with an
  empty To, Cc and Subject and a web area with no element; for 2 of
  them, no AXWebArea is listed under `scroll area 1 of group 1 of group
  1`. The 4 "Re:<51>" windows were two pairs with different bodies (830
  and 1776 characters of static text). The 2 "Re:<105>" windows held 24
  characters each, and the "Fwd:<30>" window 475.
- 13:52 and 13:53, `tend_compose_windows(dry_run=True)` and a second
  scratch read (`tsc-fpstable.py`): 25 compose windows, every one
  matched to exactly one Mail id. Body states: 16 `empty`, 2
  `unreadable` (the two without a web area), 7 `content`. Each window's
  fingerprint was the same in two inventories 20 s apart, and Mail's
  pid did not change between them.
- One inventory took 9.3 s, 9.5 s and 9.8 s for the 25 windows, against
  5.4 s for the by-name inventory on 2026-09-27 (Observation 6), which
  read 25 windows' worth of events from a few distinct windows.

## Observation 9 — an AppleScript list built inline does not compare equal

13:56. A close by id of a test window failed safe, with "0 visible
windows … stand where Mail's window 3936 does", though the window was
visible at exactly that place. Reduced:

```
osascript -e 'set b to {884, 232, 1, 1}' -e 'set cp to {item 1 of b, item 2 of b}' \
  -e 'set p2 to {item 1 of b, item 2 of b}' \
  -e 'return ({item 1 of b, item 2 of b} is cp) & (p2 is cp) & ({item 1 of b, item 2 of b} = cp)'
→ false, true, false
```

A list held in a variable compares as expected: `(item 2 of allPos) is
pos` is true. The close script assigns each position before it compares
them.

## Observation 10 — Mail can keep a closed window in its list

- 13:58. Two test windows of one name were discarded by id. System
  Events no longer listed either, and its window count was back to 26.
  Mail still listed one of them, by id and name: `visible false,
  miniaturized false`, still so a minute later. The other was gone from
  Mail's list. `exists window id` had reported both closes as failed.
- 13:59. The probe's own window, open: `visible true, miniaturized
  false`. Minimized through System Events (`AXMinimized`): `visible
  false, miniaturized true`. Restored: `true, false`. Discarded: still
  listed, `false, false`.

## Derivations from Observations 7–10 (labeled)

7. **The 2026-09-27 per-window figures for same-named windows were
   reads of the first window of the name.** Observation 2's walk, and
   the connector's inventory then, held each window in a variable
   (Observation 5's `set w to window i`), so by Observation 7 every
   "New Message" row described one window, and every "Re:<51>" row
   another. The counts by name in Observations 1 and 3 came from bulk
   reads and stand. The 18 being empty and the 4 replies being alike do
   not stand (Observation 8).
8. **Every System Events specifier names `window i` inline**, and an
   index is re-found by name and position after any click that can
   reorder the windows (`tendIndexOf`). A reference from a variable
   would act on the first window of its name.
9. **Closed means gone from Mail's list, or listed neither visible nor
   miniaturized and gone from System Events at its place**
   (Observation 10). A minimized window is not mistaken for a closed
   one, since it is miniaturized.
10. **Mail's id joins to System Events by name and position**: Mail's
    `bounds` top-left is System Events' `position` (Observation 3, and
    all 25 on 2026-10-01). Only visible Mail windows are matched, so a
    closed window Mail still lists cannot take a live window's id. Two
    visible windows of one name at one place get no id and are left.

## The rule

Written before the code. The code is in `compose_ledger.py`,
`compose_tending.py`, `compose_clock.py`, the tending section of
`mail_connector.py`, and `tender.py`. The operator settled the open
question on 2026-10-01. The ask, as relayed to the implementing agent
(not his verbatim words), was that tending close every stale compose
window, whoever opened it, without losing anything typed in it.

### The connector records every compose window it opens

`_open_compose` records each window it opens in the compose ledger
(`compose_ledger.py`). There is one JSON file per window under
`compose_windows/` in the data home; the root is resolved at use time,
and record ids are 32 lowercase hex characters, validated before any
path is built. A record holds:

- the window's name, as `_as_new_compose_window_block` found it;
- Mail's id for the window (the one window of that name whose id was not
  there before it opened), and Mail's process id;
- the time, the operation (send or save), and the seed.

The window name is the subject of the mail, so the store is personal
data at rest, like `audit.jsonl`.

A record's life:

```
open ──► left_open(failure) ──► closed(…, by tending)
  └────► closed(sent | saved <draft id> | salvaged | discarded | gone,
                by the composition or by tending)
```

The composition ends its window's record exactly once
(`_window_lifecycle`), with one of these:

- sent;
- saved, as a draft id;
- salvaged, by a failed step's salvage or by a save whose draft had not
  settled;
- discarded, for an off-allowlist recipient;
- gone, when the close found no window;
- left open with the failure, when a close failed or when anything
  stopped the composition with nothing closing its window.

A failure after the window exists no longer escapes the open script.
COMPOSE_WINDOW_NOT_UNIQUE (whose window stays open by design) and a
failed header or read-back are reported with the window's identity, so
the window is recorded as left open instead of lost from view.

A window can also open without any report: see "Every way a window
could end open and unrecorded" below. `closed` is final, and only
tending closes a window that a composition left open.

A ledger that cannot be written is logged, not raised: either the window
is open or the mail has been sent.

### A pass: two rules, one clock

A pass (`AppleMailConnector.tend_compose_windows`) does three things in
one script, in this order:

1. It reads every compose window that System Events lists, addressing
   each by index alone (Derivation 8). For each window it reads the
   name, position, To/Cc/Subject values, the body level by level,
   whether a sheet is showing, and whether it is minimized.
2. It reads Mail's id, name, bounds and visibility for every window.
3. It ties each compose window to its Mail id (Derivation 10).

`compose_tending.plan_tending` then decides each window, in this order:

- **Minimized**: left. A close on a minimized window is not something
  this code has observed.
- **Unidentified** (no single Mail id): left.
- **In flight**: a ledger record names the window's id, and its
  composition may still be running (open and younger than
  `TEND_GRACE_S`, 15 minutes). Left.
- **Abandoned**: one unfinished record from this Mail process names the
  id under the window's present name. It is closed at once.
- **Stale**: any other window, whoever opened it, whose
  `content_fingerprint` has stayed the same for `STALE_S` (one hour). It
  is closed.
- **Not yet stale**: left, with the minutes remaining.

The fingerprint is a SHA-256 hash of the window's header field values,
its body state and static texts, and its attachment count. Only the
hash is stored. The clock (`compose_clock.py`, `compose_clock.json`
beside `compose_windows/` in the data home, resolved at use time)
holds Mail's pid and, per window id, the fingerprint and when it was
first seen.

- A changed fingerprint restarts that window's clock.
- A new Mail pid restarts every clock.
- A window no longer listed is forgotten.
- A window first seen in a pass is never closed in that pass.
- A dry run reads and decides, but saves no clock and closes nothing.
- A clock that cannot be saved is logged, and the next pass then finds
  every window first-seen, so it closes nothing by the stale rule.

A renamed ledger window is no longer special: its record does not
match, so it falls to the stale rule. The September reasons `unowned`,
`unowned_empty`, `name_not_unique` and `renamed` are gone.

### Closing loses nothing, and closes exactly one window

Every close (`_as_close_compose_block`, used by tending, by a
composition's own salvage and discard, and by the allowlist refusal)
is addressed by Mail's id. In one script, it:

1. Reads that id's name and bounds from Mail. If Mail no longer lists
   the id, the close reports `NO_WINDOW`. If the window is now named
   something else, it is left.
2. Counts the visible Mail windows of that name at that place, and goes
   on only if there is exactly one.
3. Finds the System Events index by name and position (`tendIndexOf`).
4. For a discard, checks again that the window still reads empty: no
   field value, no body element, no attachment. If not, it does not
   click.
5. Clicks that window's own close button (`tendClickClose`, which finds
   the button by its AXCloseButton subrole), then answers the sheet
   with "Save" (a salvage) or "Don't Save" (a discard). Before the
   sheet, it re-finds the index by name and position.
6. Verifies by the id being gone (Derivation 9).

A salvaged window becomes a draft in its account's Drafts. Tending never
sends.

Without an id (a caller that holds only a name), the close goes by name
only when exactly one window has that name. Otherwise nothing is closed.

### Every way a window could end open and unrecorded

Audit of every path that opens a compose window, on this branch (line
numbers at the commit):

- **One opener.** `send` (`mail_connector.py:6280`) and `create_draft`
  (`:6570`) both go through `_compose` (`:6585`) to `_open_compose`
  (`:6994`). That builds the only script that opens a compose window
  (`_build_open_compose_script`, `:6868`; the opening verb is in
  `_build_creation_block`, `:5159`: `make new outgoing message …
  visible:true`, or `reply`/`forward … opening window true`). No other
  module opens one: `grep "outgoing message\|open location\|mailto"`
  over `src/` finds nothing outside `mail_connector.py` but a comment.
  `scripts/spike_draft_resave.py` is a developer spike, not run by the
  server.
- **Ways it ended open and unrecorded, and their fixes:**
  1. *NO_COMPOSE_WINDOW* (`:5368`): Mail opens the window more than 5 s
     after it was asked. The script errors before reporting it.
  2. *A timeout*: the osascript run is killed, or an AppleEvent times
     out, after Mail made the window but before the report.
  3. *An unreadable report*: `parse_applescript_json` or
     `_compose_window_from_report` fails.

  The fix for all three: `_open_compose` reads Mail's pid and window ids
  first (`_mail_window_snapshot`, `:7088`). On any of these failures it
  polls for up to `_ADOPT_WAIT_S` (10 s) for visible compose windows of
  the same Mail process whose ids are new (`_build_adopt_script`,
  `:7112`). For a fresh message, only windows named the subject count.
  It records each one as left open (`_adopt_unreported_windows`,
  `:7169`), which wakes tending, and adds to the raised error how many
  it found. SEED_NOT_FOUND opens nothing and is not looked after. A
  window that another unfinished record already names is left to that
  record.
- **Covered only by the stale rule:**
  - a ledger that cannot be written (`_record_window_open`, `:7077`,
    logs and goes on);
  - a window that someone else opens during the 10 s look and that has
    the subject's name, which would be recorded as the connector's
    (it would then be salvaged, so nothing is lost);
  - a window Mail reopens after an asynchronous send failure (not
    observed);
  - any window a person, another client, or a test run opens.
- **By-name closes** could close the wrong one of two same-named
  windows. All closes now go by id, as described above.
- **The by-name inventory** (Derivation 7) read only the first window
  of each name. It now reads by index.

Where the 25 windows came from is not established. On 2026-09-27 they
were said to be left by earlier code paths. Two carry an
integration-test marker as their whole body.

### When a pass runs

A pass runs in the daemon (`mail-serve`, `tender.py`):

- once at start;
- every `TEND_INTERVAL_S` (15 minutes; the inventory holds the Mail lock
  for about 10 s for 25 windows, Observation 8);
- soon after any composition leaves its window open
  (`on_window_left_open`, which adoption also calls);
- never two passes within 60 s.

`--tend-interval 0` turns it off. The stdio server does not tend: many
run at once, one per session, and one resident process is enough. What
a stdio server leaves is in the same ledger, and the daemon's stale rule
covers the rest.

Every pass is logged through `operation_logger` as
`tend_compose_windows`. It is an internal operation in `security.py`,
in the `expensive_ops` tier with the other mutations. The log records
what the pass found, closed per rule and how, failed to close, and left,
by reason.

No MCP tool: nothing about a pass is a caller's decision, and the
api-design decision tree ends at "a distinct operation" only for a
different Mail object or action a caller asks for.

## Live results, 2026-09-27

Under the September rule: closes by name, and windows nobody recorded
left.

`MAIL_TEST_MODE=true`, `MAIL_TEST_ACCOUNT` the iCloud test account, no
`MAIL_TEST_LOOPBACK`. Nothing sent.

- Dry run against the real Mail with an empty ledger (08:51): 25 compose
  windows, `left: {unowned: 7, unowned_empty: 18}`, nothing to close.
- `tests/integration/test_compose_tending.py`, 5 passed (08:54–08:55),
  each with a ledger of its own:
  - a window opened through `_open_compose` and abandoned was recorded
    (open, with a Mail id not among the windows open before), left as
    `in_flight` inside the grace period, planned for salvage by a dry
    run with the grace at 0 (which changed nothing), then salvaged by a
    real pass, its record `closed(salvaged, by tending)` and its draft
    in Drafts;
  - a window opened past the connector was left, counted `unowned`;
  - a real pass with an empty ledger closed nothing: the ledger it read
    held no record, so no window, the 25 restored ones included, could
    be the connector's;
  - two windows of one name: the salvage closed neither;
  - a connector window refused as COMPOSE_WINDOW_NOT_UNIQUE was
    recorded left open under its own Mail id, not the other window's,
    and the pass left both as `name_not_unique`.
  Every test checked that each window open before it was still open
  after. System Events window count 27 and `outgoing messages` 3 before
  and after each test.
- The existing no-send integration tests over the changed AppleScript,
  9 passed: the discard and salvage primitives, fresh and reply draft
  saves, the HTML reply and forward saved (a subject set on a reply or
  forward, so the second naming of the window), and a reply draft from
  an RFC id. They wrote 7 records to the real ledger, each
  `closed(saved)`.

## Live results, 2026-10-01

Run with `MAIL_TEST_MODE=true`, `MAIL_TEST_ACCOUNT` set to the iCloud
test account, and no `MAIL_TEST_LOOPBACK`. Nothing was sent. No window
that a test or probe did not open was clicked, focused, typed into, or
closed.

- **Dry run against the real Mail** (13:52, with the real ledger and no
  clock file): 25 compose windows, `left: {not_yet_stale: 25}`, each
  with 60 minutes remaining, and nothing to close. From the fingerprints
  read at 13:53 (Observation 8), a pass an hour after the first one
  would discard the 16 empty windows and salvage the other 9. Those 9
  are the 7 with content and the 2 "New Message" windows whose body was
  unreadable.
- **`tests/integration/test_compose_tending.py`**: 9 passed (14:02–14:07).
  Each test had its own ledger and clock. Before each real pass that
  could close a window, a dry plan named only the test's own window,
  by id.
  - A window opened through `_open_compose` and abandoned was recorded,
    left `in_flight`, planned by a dry run, and then salvaged by a real
    pass as `abandoned`. Its record was ended, and its draft was in
    Drafts.
  - A first real pass with a fresh clock closed nothing and clocked
    every window.
  - Two empty "New Message" windows opened by the test, among the other
    16: the one whose clock was backdated past `STALE_S` was discarded
    by id as `stale`, and its sibling stayed open.
  - An unrecorded window with content, backdated, was salvaged as
    `stale`, and its draft reached Drafts.
  - A backdated window was edited through `_paste_verified`. The next
    pass left it, with its clock restarted at the edit.
  - Two windows of one name: a close by name closed neither. A close by
    id closed the newer one, then the older one behind a third, each
    time leaving the others.
  - A connector window refused as COMPOSE_WINDOW_NOT_UNIQUE was
    recorded under its own id and closed by the next pass. The
    same-named window that was already open stayed.
  - A window opened after a snapshot and found by
    `_adopt_unreported_windows` was recorded as left open, then closed
    by the next pass.

  In every test, each window open before it was still open after it.
  System Events listed 26 windows, and `outgoing messages` was 0,
  before and after each test.
- **Other no-send integration tests over the changed AppleScript**: 12
  passed (14:07–14:13). They cover the discard and salvage primitives,
  fresh and reply draft saves, a draft's state and attachments, an
  update keeping its account, a draft delete, and the HTML forward that
  is composed and saved.
  - The HTML reply variant
    (`test_a_reply_keeps_mails_quote_and_takes_a_file`) errored twice,
    in its fixture `earlier_seed`, which timed out at 60 s scanning Sent
    and Trash before any window opened. That code is unchanged on this
    branch.

## What is not verified

- **Real passes over windows not opened by the tests.** The deployed
  daemon has not run this code. The first real closes of the 25 will
  be its own, at least an hour after its first pass.
- **The 2 "unreadable" New Message windows.** Why their body has no web
  area at the usual path is not known. They are salvaged, not
  discarded, which loses nothing but files two empty drafts.
- **A minimized compose window** is left by every pass. Closing one was
  not tried.
- **A sheet already showing** on a window when tending reaches it (a
  send-error sheet): the salvage handles it in unit tests only. It
  cannot be provoked on demand.
- **NO_COMPOSE_WINDOW and timeouts themselves were not provoked.** The
  adoption was run live only on a window the test opened after a
  snapshot. The unit tests cover each failure route.
- **Mail truncating a long window title.** If Mail shortens the title
  relative to the subject, the fresh-message name filter in adoption
  would miss the window, and the stale rule would close it instead.
- **Adoption on a hung Mail.** The 10 s look adds to an error that
  already took the timeout. The look's own osascript is bounded by the
  same timeout, so time-to-error at most roughly doubles.
- **Windows addressed by name in other scripts.** The verified send,
  the paste and the read-back still address a window by name, for the
  window the composition itself opened and checked to be unique. A
  person opening a same-named window mid-composition is not guarded
  there.
- **Mail's state files.** Which of them restores windows at a relaunch,
  and whether they carry any date, was not looked into further.
- **Positions shared by two visible same-named windows.** Such windows
  get no id and are left as `unidentified`. None did on 2026-10-01.

## Re-check commands

Observations 1–5 and 7–10 were osascript probes in a scratch directory;
the lines above give their shape, and Observation 9's is quoted whole.
Observations 6 and 8 and the dry run re-run, with no change to Mail, to
the ledger or to the clock (a dry run saves none), as:

```
uv run python -c "
from apple_mail_mcp.mail_connector import AppleMailConnector
print(AppleMailConnector().tend_compose_windows(dry_run=True).as_dict())"
```

# Tending Mail's compose windows (observations, and the rule)

Recorded 2026-09-27 against the live machine. Format follows
`docs/research/attachment-property-10000.md`: observations with the
command that produced them first, derivations labeled and secondary. The
measurement (Observations 1–6) was read-only: nothing was clicked,
focused, closed or typed into. The rule and the code came after it.

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
with `set w to window i` (08:28, all 25 read).

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
   nothing, whoever opened it. The other 7 carry content.
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
   Every close this connector does addresses a window by name
   (`_as_discard_compose_block`, the salvage), so with two of a name it
   may close the other one. Mail's window `id` is identity, within one
   Mail process: a relaunched Mail gives the windows it restores new
   ids.
5. **`outgoing messages` does not see these windows** (Observation 4):
   0 with all 25 open. The outgoing messages it later listed were
   windowless ones some other client made during the measurement.
6. **Windows are addressed by index in one script**, never through a
   nested `every` reference (Observation 5).

## The rule

Written before the code; the code is `compose_ledger.py`,
`compose_tending.py`, the tending section of `mail_connector.py`, and
`tender.py`.

### The connector records every compose window it opens

`_open_compose` records each window it opens in the compose ledger
(`compose_ledger.py`): one JSON file per window under
`compose_windows/` in the data home, root resolved at use time, record
ids 32 lowercase hex characters validated before any path is built. A
record holds the window's name as `_as_new_compose_window_block` found
it, Mail's id for the window (the one window of that name whose id was
not there before it opened) and Mail's process id, the time, the
operation (send or save) and the seed. The window name is the subject
of the mail, so the store is personal data at rest, like `audit.jsonl`.

A record's life:

```
open ──► left_open(failure) ──► closed(…, by tending)
  └────► closed(sent | saved <draft id> | salvaged | discarded | gone,
                by the composition or by tending)
```

The composition ends its window's record exactly once
(`_window_lifecycle`): sent; saved as a draft id; salvaged (by a failed
step's salvage, or a save whose draft had not settled); discarded (an
off-allowlist recipient); gone (the close found no window); or left
open with the failure (a close that failed, or anything that stopped
the composition with nothing closing its window). A failure once the
window exists no longer leaves the open script: COMPOSE_WINDOW_NOT_UNIQUE,
whose window stays open by design, and a header or read-back that fails
are reported with the window's identity, so the window is recorded as
left open instead of lost from view. `closed` is final, and only tending
closes a window a composition left open. Tending never sends or saves.

A ledger that cannot be written is logged, not raised: the window is
open, or the mail sent, either way.

### A pass closes only the connector's own windows

A pass (`AppleMailConnector.tend_compose_windows`) reads every compose
window System Events lists (by index), and Mail's id and name for every
window, then decides (`compose_tending.plan_tending`):

- **The connector's own, abandoned.** A ledger record that is not
  closed, from this Mail process, whose Mail window id is still there
  under the name the record holds, and which is the only window of that
  name. It is closed when its composition cannot still be running: at
  once when the composition ended leaving it open, and otherwise after
  `TEND_GRACE_S` (15 minutes; after its window is found a composition
  makes at most eight more osascript calls, each bounded by the 60 s
  timeout and the 30 s wait for the Mail lock, so twelve minutes at the
  defaults). Empty, it is discarded (`_as_discard_compose_block`, the
  verified close without Save), only if it still reads empty in the same
  script as the click, so nothing typed into it since is thrown away.
  Otherwise it is salvaged to Drafts (the salvage block), so nothing
  typed is lost. Each close re-checks, in the same script as the click,
  that Mail's id still names the window and that no other window has its
  name. The cross-process Mail lock keeps a close from landing between
  another process's osascript calls, and the grace period keeps it off a
  composition still between them.
- **Everything else is left and counted**: windows no record names,
  those that are empty apart from the rest (`unowned_empty`); a record's
  window whose composition may still be running (`in_flight`), that
  shares its name (`name_not_unique`), or whose subject someone edited
  since (`renamed`: someone is working in it).
- **Records whose window is gone** (closed by someone else, or lost with
  a Mail relaunch) are ended as `gone`. Closed records older than seven
  days are pruned.

`dry_run` reads and decides and closes and records nothing. A close that
Mail does not answer stops the pass; the rest are reported not
attempted.

### Empty windows nobody recorded: left, by default and for now

Recommendation: leave them, count them, report them. Closing one loses
nothing, but the measurement gives no safe discriminator between a
leftover and a window a person has just opened to write in
(Derivation 3). The risk of closing is not lost text; it is a window
vanishing under someone about to type into it. Two things would be
needed before tending could close them, and both are the operator's
decision, not built here:

1. A discriminator from time rather than from the window: Mail's id is
   stable within a Mail process, so a pass could note an empty window
   by id and close it only once it has stayed empty across passes for,
   say, an hour.
2. A close that tells same-named windows apart: all 18 are "New
   Message", and both closes address a window by name. A discard of
   "the first window named New Message" that re-checks emptiness on
   exactly that window, and verifies by the count of the name going
   down one rather than by the name being gone.

Until then the 18 stay, and every pass reports them as `unowned_empty`.

### When a pass runs

In the daemon (`mail-serve`, `tender.py`): once at start, every
`TEND_INTERVAL_S` (15 minutes; the inventory holds the Mail lock about
5.5 s for 25 windows), and soon after any composition leaves its window
open (`on_window_left_open`), never two within 60 s. `--tend-interval 0`
turns it off. The stdio server does not tend: many run at once, one per
session, and one resident process is enough; what a stdio server leaves
is in the same ledger.

Every pass is logged through `operation_logger` as
`tend_compose_windows` (an internal operation in `security.py`, in the
`expensive_ops` tier with the other mutations): what it found, closed,
failed to close and left, by reason.

No MCP tool: nothing about a pass is a caller's decision, and the
api-design decision tree ends at "a distinct operation" only for a
different Mail object or action a caller asks for.

## Live results, 2026-09-27

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

## What is not verified

- The discard branch of a pass (an empty window the ledger names) has
  unit tests only. The connector never opens an empty window (a fresh
  one has its subject from the start), and an empty one would be named
  "New Message", which 18 other windows are.
- The send-side endings (sent, and the gate's discard) have unit tests
  only here; no send was made.
- Whether compiling the inventory script starts a Mail that is not
  running was not tested (Mail was not quit). The script asks nothing of
  Mail before it checks `application "Mail" is running`.
- A window Mail opens more than 5 s after the connector asked
  (NO_COMPOSE_WINDOW) is not recorded; a pass counts it as someone
  else's.
- The daemon's tender was not run against the real Mail; its thread is
  unit-tested with the pass replaced.
- Which of Mail's state files restores windows at a relaunch, and
  whether it carries any date.

## Re-check commands

Observations 1–5 were osascript probes in a scratch directory; the
lines above give their shape. Observation 6 and the dry run re-run, with
no change to Mail or to the ledger, as:

```
uv run python -c "
from apple_mail_mcp.mail_connector import AppleMailConnector
print(AppleMailConnector().tend_compose_windows(dry_run=True).as_dict())"
```

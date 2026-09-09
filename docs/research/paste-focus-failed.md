# PASTE_FOCUS_FAILED on attachment sends

Observations first with the commands that produced them, derivations
labeled and last, gaps named. Same shape as
`attachment-property-10000.md`, per `docs/DISCIPLINE.md`.

## Observation 1 — the report (2026-09-05)

A peer session, sending through the `email_send_html` MCP tool, hit
`PASTE_FOCUS_FAILED` three times running. Because `email_send_html` is
the only attachment-capable send path, and `draft_send` refuses fresh
drafts carrying attachments and points back at it, there was no route
to send an attachment through the MCP at all.

## Observation 2 — what was measured then (2026-09-05)

On a compose window made by `make new outgoing message` with an **empty**
body, live against Mail.app:

- `set focused` on the body WebArea left `AXFocusedUIElement` at
  `missing value`. So did `click`, a real coordinate click inside the
  WebArea's own reported bounds, and Tab from the subject field.
- Nothing typed reached the body. Both Mail's scripting layer and the AX
  tree read it back empty.

Fixed in `0176e88` by seeding the compose body with a single space
(`_BODY_SEED`). Three later attempts to remove the seed after focus was
obtained are recorded in the docstring of
`_build_attach_compose_script`; all three left a visible artifact, which
is why the seed is a space rather than a character that would show.

## Observation 3 — it does not reproduce (2026-09-07)

Run after a full machine restart, against live Mail.app.
**Time since boot matters here and is recorded deliberately:** the machine
booted 2026-09-07 21:03:24 EDT, Mail started 21:03:37, and both runs below
finished before the note was committed at 21:14:46. So this was a Mail
process roughly seven to eleven minutes old, on a host up under twelve
minutes.


    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT="<account>" uv run pytest \
      "tests/integration/test_verified_send.py::TestHtmlSendWithAttachments::test_fresh_html_with_attachments_end_to_end" \
      --run-integration -v

    1 passed in 10.31s

That test predates the fix — it was added in `6825844`, and `0176e88`
did not touch `tests/`. So a pass does not by itself say the fix is
doing anything. The discriminating run, with `_BODY_SEED` temporarily
set to `""` and everything else unchanged:

    1 passed in 6.62s

**The path works today with the seed and without it.** The source file
was reverted immediately (`git checkout --`); the tree is clean.

## Observation 4 — an aged Mail does not reproduce it either (2026-09-09)

The gap named on 2026-09-07 was that the runs happened minutes after
boot, while the failure had been reported from a long-running session.
Re-run against the same Mail process two days later, without restarting
it:

    $ ps -o lstart=,etime= -p $(pgrep -x Mail)
    Mon Sep  7 21:03:37 2026     01-16:22:22

So: same pid, up 1 day 16 hours, on a host up the same. Both runs
repeated:

    with _BODY_SEED = " "     1 passed in 19.95s
    with _BODY_SEED = ""      1 passed in 10.08s

**Still passes with the seed and without it, on an aged Mail.** The
source was reverted immediately; the tree is clean.

A session restart does not restart Mail, so the process keeps ageing
across one. That is what made this measurable without arranging
anything.

### What this does and does not settle

It removes process age as the explanation. It does not remove the
degraded-state hypothesis, because **age is not the same variable as
wear**: the Mail that failed had been driven repeatedly through the
failing path and had four orphaned compose windows in it, and this one
has been idle. Nobody has tried to reproduce the failure by first
putting Mail into that state, and that is now the untested condition
rather than "an old Mail".

## Derivations, mine

- The condition that produced `PASTE_FOCUS_FAILED` is **not present on
  this machine today**, so `0176e88` cannot be shown to be load-bearing
  under current conditions. It is not disproven either. Nothing here
  says the seed is unnecessary; it says the experiment that would settle
  it is unavailable while the failure will not reproduce.
- A restart intervened between the failure and the first runs. That
  made a degraded Mail or window-server state a candidate explanation,
  which would also fit the four orphaned compose windows cleared on
  2026-09-05 — windows present in the AX tree and in Mail's own window
  list, with no backing object in `outgoing messages`. **Candidate,
  not established.** Observation 4 narrows it: whatever the state is,
  it is not simply an old process, because a process up nearly two days
  behaves like a fresh one. If the hypothesis survives, the variable is
  what Mail has been made to do, not how long it has been running.
- The seed stays. It is a single space, it costs nothing, and removing
  it would trade a state known to work for one whose failure mode is
  understood only from a report that no longer reproduces.

## Gaps, deliberately named

- **The MCP tool layer was not exercised.** This run calls
  `connector._send_html_email` directly. The reported failure came
  through the `email_send_html` MCP tool, which adds the server layer
  and the outbound allowlist gate on top. Exercising that end of it
  needs a send to an allowlisted real recipient, which is outward-facing
  and needs the operator's authorization each time, so it was not done.
- **Why the focus behaviour differed between 2026-08-27 and 2026-09-05
  is still unchased.** The attachment test passed on the earlier date
  and the tool failed on the later one, with no code change in between
  that anyone has identified.
- **Whether the failure is intermittent or state-dependent is unknown.**
  Three consecutive failures were observed on 2026-09-05; two
  consecutive passes were observed on 2026-09-07. No one has run it
  enough times, on either side, to tell those apart.

- **Superseded by Observation 4 below.** This gap said the
  non-reproduction was scoped to a freshly started Mail and that nobody
  had tested an aged one. That has now been tested. The requirement it
  introduced stands: any future attempt records time since boot and the
  age of the Mail process alongside the result, because without those
  two numbers a pass and a failure are not comparable.

Recorded 2026-09-07; Observation 4 added 2026-09-09.

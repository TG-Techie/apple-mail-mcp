# A freshly saved draft is not yet the draft you get back

Observations first with the commands that produced them, derivations
labeled and last, gaps named. Same shape as `paste-focus-failed.md`,
per `docs/DISCIPLINE.md`.

Account names below are the local test accounts; the behaviour is what
matters, not the names.

## Observation 1 — the integration test that failed (2026-09-11)

`tests/integration/test_mail_integration.py::TestDraftsLifecycleIntegration::test_update_keeps_the_draft_in_its_account`
run with `MAIL_TEST_ACCOUNT` set to the iCloud test account failed twice
running, both times the same way:

```
AssertionError: {'success': False, 'error': "'to' is required when seed='new'", 'error_type': 'unknown'}
```

The test creates a fresh draft with one `to` address through
`server.create_draft`, then calls `server.update_draft(body="v2")` at
once. `update_draft` reads the draft back with `get_draft_state`, got
`to: []`, merged that as the recipient list, and the connector refused
to rebuild a fresh draft with nobody to send to.

The same three tests, run the same minute with `MAIL_TEST_ACCOUNT` set
to the Gmail test account: `3 passed`.

## Observation 2 — polling one fresh iCloud draft

`connector.create_draft(seed="new", to=["target@example.com"], ...)`
on the iCloud account, then `get_draft_state(id)` every ~2.4 s, beside
`get id of (every message of mailbox "Drafts" of account … whose
subject is …)`:

```
created 1894
  0.7s id=1894 to=[]  drafts-with-subject=1894
  3.1s id=1894 to=[]  drafts-with-subject=1894
  ...
 19.7s id=1894 to=[]  drafts-with-subject=1894
```

Roughly a minute later the same subject was in Drafts as id `1895`
with `to-recips=1`. An earlier run of the same probe (`created 1892`)
read `to=[]` at 0.7 s and 2.5 s and raised `MailDraftNotFoundError` for
`1892` at about 4 s; the draft was then in Drafts as `1893`.

## Observation 3 — the Message-ID changes too

Same creation, then every 1.5 s for 45 s, printing only on change:

```
created 1910
  0.2s 1910 msgid=E0A26789-…@icloud.com to-recips=0 source-has-To=false
 24.6s 1911 msgid=7FEC624E-…@icloud.com to-recips=1 source-has-To=true
```

`source-has-To` is `(source of d) contains "To: target@example.com"`.
The first copy carries no `To:` header in its raw source at all. The
replacement carries a different Mail id and a different `Message-ID`.

## Observation 4 — the same probe on the Gmail account

```
created 1908
  1.7s id=1908 to=['target@example.com']  drafts-with-subject=1908
  ...
 29.1s id=1908 to=['target@example.com']  drafts-with-subject=1908
```

Recipients read back at once; nothing changed in 30 s.

## Observation 5 — compose windows are left behind

`count of outgoing messages` in Mail was 25 before the probe in
Observation 3 and 26 after it. Mail's window list included one window
per test subject. `close (every window whose name is "<subject>")
saving yes` returned without error, left the count at 26, and did not
change the timing in Observation 3's pattern (a further run: id `1912`
replaced by `1913` at 19.6 s). Afterwards, `close w saving no` on each
window taken by direct reference from `every window whose name starts
with …` returned without error for both, and both were still in the
window list with the count unchanged at 26.

## Observation 6 — the compose order decides whether the first copy has recipients (2026-09-11, later)

Raw AppleScript against the Gmail test account, `make new outgoing
message` then `save`, polling the aggregate `drafts mailbox` every
0.5 s for a new id and printing only on change:

```
connector order (set sender, set content, delete/make to recipient):
  1.5s: id=2023 to=0
 12.0s: id=2025 to=1
no sender at all:
  0.5s: id=2026 to=1            (nothing else in 20 s)
recipient first, sender last:
 10.5s: id=2028 to=1
 17.0s: id=2030 to=1
```

Then with the sender set last, varying only the sender value, printing
the account each copy landed in:

```
sender = the default account's bare address:   0.5s id=2031 to=1 (iCloud); nothing else in 25 s
no sender:                                     0.5s id=2033 to=1 (iCloud); nothing else in 25 s
sender = the other account's bare address:     2.0s id=2034 to=1 (Gmail);  19.0s id=2035 to=1 (Gmail)
sender = default account, "Name <addr>" form:  0.5s id=2037 to=1 (iCloud); 18.0s id=2038 to=1 (iCloud)
```

## Observation 7 — appearing is not settling

Create with no sender, poll the aggregate id list every 0.25 s, and
delete the draft (through `first message of drafts mailbox whose id
is …`) after an extra wait; then poll for it to vanish:

```
extra-wait=0s appeared-at=0.25s id=2054 gone-after=never   (16 polls, 8 s)
extra-wait=1s appeared-at=0.25s id=2056 gone-after=0.5s
extra-wait=3s appeared-at=0.25s id=2058 gone-after=0.5s
```

Deleting through the per-account mailbox reference instead of the
aggregate made no difference at 0 s (`id=2048 gone after never`).
Sampling `message size`, `to recipients` count, `message id` and
`was forwarded` every 0.25 s over the first 3 s after appearance
showed no change in any of them (`source` raised throughout).

The aggregate `drafts mailbox` and the per-account mailbox listed a
new draft at the same poll in every comparison (bulk `id of every
message`, and a `repeat` walk, both ways).

## Observation 8 — the re-save on the Gmail account, and what a cleanup by id leaves (2026-09-11, afternoon)

Create with `from_account` naming the Gmail test account (not Mail's
default sender), then list every draft whose subject carries the probe
prefix, with its id and the account its mailbox resolves to, once a
second for 45 s:

```
created 2276 t=2.9s
t=  3.9s drafts matching: [2276@<gmail test account>]
t=  8.1s drafts matching: [2277@<gmail test account>]
```

One re-save, 4–5 s after creation, no further change to 45 s. Two
earlier runs of the same shape, read through `get_draft_state`
instead of the list, lost the original id sooner: at 1.2 s after the
create returned in one, between 5 and 10 s in the other. Observation 4
had no re-save on this account in 30 s; that probe set no sender.

Deleting by the original id, as the integration suite's `finally`
blocks do, therefore removes nothing once the re-save has happened,
and the copy under the new id stays. After one run of the drafts
lifecycle class on the iCloud test account, six drafts with the
class's `ZZZ-AMM-INTEG-` subjects remained, all under ids the tests
never held. They were removed by id through `delete_draft`; a listing
20 s later showed none.

A walk of the form `repeat with d in messages of drafts mailbox … delete
d` inside a `try` deleted nothing and reported 0, three runs; the same
drafts deleted through `first message of drafts mailbox whose id is …`.
Whether the `delete` on the walk's item reference errors, or is
accepted and lost, was not separated.

## Observation 9 — the class failure's error text, and the spread of the re-save (2026-09-11, later)

Running the drafts lifecycle class on the iCloud test account, one
test in nine failed, in `get_draft_state`, during the walk of the
aggregate drafts mailbox:

```
MailMessageNotFoundError: 250:258: execution error: Mail got an error:
Can't get message id 2359 of mailbox "Drafts" of account id "…". (-1728)
```

The walk evaluates `messages of drafts mailbox` once and then reads
`id of d` for each item; a draft that is gone by the time the walk
reaches it raises there. Which draft 2359 was — the test's own, or a
copy from an earlier test in the class — was not captured. The same
test's shape run alone, four times (create with the test account as
sender, read the state, delete by id, then watch for the copy):

```
run 0: created 2380 @3.6s; state ok @4.3s; delete @4.4s; copy 2382 appeared @28.3s
run 1: created 2384 @2.8s; state ok @3.6s; delete @3.7s; copy 2386 appeared @12.3s
run 2: created 2388 @2.8s; state ok @3.5s; delete @3.6s; copy 2390 appeared @25.7s
run 3: created 2392 @3.0s; state ok @3.7s; delete @3.8s; copy 2394 appeared @8.5s
```

Four for four alone; the copy came 8.5–28.3 s after creation, and a
fifth measurement the same hour gave 30.6 s. Observation 3 had 12–19 s.
Ninety seconds of watching after a sweep that deleted such copies
showed none returning: deleting a copy does not start another re-save.

The walks in `get_draft_state` and `extract_draft_attachments` now
read each id inside a `try` and skip an item that raises. Whether that
is what the class failure needed is a derivation from the error text,
not a re-run under the same conditions.

## Derivations, mine

- On an iCloud account, the id `draft_create` returns is transient. In
  four runs the replacement arrived between ~4 s and ~60 s after the
  save. Anything holding that id past that point (`draft_update`,
  `draft_send`, the documented create → human review → send lifecycle)
  gets `draft_not_found`.
- Inside that window, `get_draft_state` on a fresh draft reads no
  recipients, because the first saved copy has none. `draft_send` then
  refuses with "draft has no recipients" and `draft_update` on a fresh
  seed fails as in Observation 1. Neither error names the real cause.
- The `Message-ID` header is not a stable key across the replacement
  (Observation 3), so persisting it at create time would not let a
  later call find the replacement. Subject plus sender plus body would
  match, and is not a key.
- The first saved copy lacking recipients (Observations 2, 3) came from
  the connector setting `sender` before content and recipients
  (Observation 6, four runs to=0 that way, three runs to=1 with the
  sender last). The connector now sets the sender last.
- The replacement under a new id is tied to the sender: none in 25 s
  with no sender or with the default account's bare address; present
  at 12-19 s with the other account's address or with the default
  account in "Name <addr>" form (Observation 6). That is consistent
  with the open compose window (Observation 5) being autosaved when
  its sender differs from what Mail would have chosen, and is **not
  established**: the window cannot be closed by script to test it.
  It is not an iCloud-versus-Gmail difference; the earlier Gmail probe
  used no sender.
- A draft listed in Drafts is not yet a draft Mail will act on: a
  delete at the moment of listing is lost, one a second later is not,
  and nothing readable on the draft distinguishes the two states
  (Observation 7). `create_draft` now polls for the listing and then
  waits `_DRAFT_SETTLE_S` (1 s) before returning the id; that number
  is a measured bound, not a signal, and is recorded as such in the
  code.

## What this bears on

- `update_draft` now recreates before it deletes (see the commit that
  added this file), so the Observation 1 failure leaves the caller's
  draft in Drafts instead of in Trash. That is the failure the reorder
  exists for; it does not make the read-back correct.
- The read-back is fixed by the compose order and the settle wait
  (same commit as Observations 6 and 7). The id churn for a draft
  saved with a non-default sender remains: a draft key that survives
  Mail's re-save, or a way to close the compose window, is a design
  change. Queued.
- The integration suite cleans up by the id it created (Observation
  8), so every test that names a sender leaves its draft behind under
  the re-saved id. `tests/integration/conftest.py` now sweeps the test
  account's `ZZZ-AMM-INTEG-` drafts by id before the first test and,
  after the last, keeps sweeping until none has appeared for a quiet
  period (Observation 9's spread is why it is a quiet period and not
  a fixed wait).

## Not tried

- Closing the compose window through its `outgoing message` object
  rather than through the window; closing it through the UI.
- Whether the re-save happens when the sender is set inside the
  `make new outgoing message` properties rather than afterwards.
- Reading the draft through the IMAP path instead of AppleScript inside
  the window.
- Any account type other than these two.

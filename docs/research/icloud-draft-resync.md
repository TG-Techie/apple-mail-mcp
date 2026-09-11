# A freshly saved draft on an iCloud account is not the draft you get back

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
replaced by `1913` at 19.6 s).

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
- What triggers the replacement is **not established**. The open
  compose window (Observation 5) is the obvious candidate — Mail
  re-saving the outgoing message — but closing the window by name did
  not stop it, and it is not known whether that `close` reached the
  window at all, since the outgoing-message count did not move.
- Why Gmail differs is not established either. The 30 s Gmail probe may
  simply have been shorter than that account's replacement delay.

## What this bears on

- `update_draft` now recreates before it deletes (see the commit that
  added this file), so the Observation 1 failure leaves the caller's
  draft in Drafts instead of in Trash. That is the failure the reorder
  exists for; it does not make the read-back correct.
- The fix for the read-back and the id churn is a design change, not a
  patch: a draft key that survives Mail's re-save, or a wait-until-
  settled step after save that is measured rather than guessed. Queued.

## Not tried

- Closing the compose window through its `outgoing message` object
  rather than by window name, or with `saving no`.
- A Gmail probe longer than 30 s.
- Reading the draft through the IMAP path instead of AppleScript inside
  the window.
- Any account type other than these two.

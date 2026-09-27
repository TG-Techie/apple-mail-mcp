# Which save path keeps a draft's id when it names a sender (spike, 2026-09-27)

Observations first, each with the command that produced it and its
output. Derivations are labeled and come last, and gaps are named. Same shape as
`attachment-property-10000.md`, per `docs/DISCIPLINE.md`. This extends
`icloud-draft-resync.md`, Observation 6. The question is the DESIGN-QUEUE
entry "A draft saved with a non-default sender is re-saved under a new
id".

## Environment and method

macOS 26.5 (25F71), Mail 16.0. Three accounts are configured in Mail,
one of them disabled. Every draft was saved in **the test account**, an
iCloud-type account. The code is branch `spike/draft-resave` from
`acc7d96`, and the harness is `scripts/spike_draft_resave.py`, which is
measurement scaffolding and not part of the server. Every live command
ran with `MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<the test account>`.

Every run follows the same steps:

1. t=0 is when the create call is issued. `ret` is when it returned.
2. The create calls go through `AppleMailConnector`. So does the
   cleanup, which is Mail's `delete` and moves a draft to Trash. Both
   hold its cross-process lock.
3. `ret` includes any wait for that lock. A concurrent integration
   suite was driving the same Mail throughout.
4. Every 2 s, from `ret` until 45 s after it, a bare `osascript` reads
   `messages of drafts mailbox whose subject is <subject>`. For each
   message it records the id, `message id`, `sender`, and the name of
   the account its mailbox belongs to.
5. The sender is logged as a token, never as itself:
   - `test:named` is exactly the `"Name <addr>"` string that
     `_resolve_account_to_sender` returns for the test account.
   - `test:bare` is the test account's bare address.
6. After the poll, the harness reads the Mail windows and System Events
   windows with that name, `count of outgoing messages` (all, and those
   with that subject), any sheet on any Mail window, and the copies of
   that subject in `trash mailbox`. It reads these before its own
   cleanup.

Each subject is `ZZZ-AMM-INTEG-spike-<variant>-<hex>`. The exception is
the reply variant, whose subject is Mail's `Re: <seed subject>`. The
Message-IDs below are cut to their first 8 hex digits.

Re-check, for any variant:

```
MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=<the test account> \
    uv run python scripts/spike_draft_resave.py <variant> --log <file.jsonl> [--seed-id <id>]
```

## Observation 1: which account is Mail's default (2026-09-27 01:30–01:58 EDT)

`defaults read com.apple.mail` has no key naming a send-from account.
`AccountOrdering` lists, after the local mailboxes, the test account's
UUID first. In three `v1-none` runs, a draft saved with no sender was
filed in the test account with the test account's bare address as its
sender (Observation 3). So the test account is Mail's default account
here, as measured by what Mail chose, not by any setting that was read.

## Observation 2: the dictionary path with the account named (`v1-named`)

`connector.create_draft(seed="new", to=["probe@example.com"],
subject=…, body="x", from_account=<the test account>)`. This is the
connector's own path: `make new outgoing message` with `visible:false`,
then the content, the recipients and the sender last, then `save`.

```
01:34:54  ret= 3.4s  3482 (163EA650) test:named  ->  3483 (6EA053D3) between t=3.6 and 5.8
01:36:42  ret=15.5s  3515 (0E5D1060) test:named  ->  3516 (6788F049) between t=15.7 and 17.7
01:54:16  ret= 4.0s  3620 (CA4A046C) test:named  ->  3621 (4A60A914) between t=10.2 and 12.2
```

The id changed in 3 runs of 3. The old id was gone, and the new one had
a new Message-ID, the same sender and the same account. The change came
0.2–2.4 s, 0.2–2.2 s and 6.2–8.2 s after the call returned. Nothing
else changed to 45 s after the return. The first run's draft was then
trashed at t=37.7–39.6 by another process (Observation 9).

## Observation 3: the dictionary path with no sender (`v1-none`)

The same call with `from_account=None`.

```
01:38:02  ret=3.1s  3519 (DF539D54) test:bare  unchanged to t=47.3
01:38:57  ret=2.4s  3521 (5C51FE05) test:bare  unchanged to t=34.6, then trashed by another process
01:57:50  ret=3.7s  3656 (71E815CB) test:bare  unchanged to t=47.9
```

The id and the Message-ID stayed the same in every sample.

## Observation 4: the sender inside the creation properties (`v2-named`, `v2-bare`)

This raw script ran through `connector._run_applescript`, with the new
id found by the connector's own Drafts diff, poll and settle
(`create_raw` in the harness):

```
set theMessage to make new outgoing message with properties {subject:<S>, content:"x", sender:<SENDER>, visible:false}
make new to recipient at end of to recipients of theMessage with properties {address:"probe@example.com"}
save theMessage
```

`<SENDER>` was the `"Name <addr>"` form (`v2-named`), or the bare
address (`v2-bare`):

```
v2-named 01:40:08  ret=3.2s  3530 (5C17DA1A) test:named  unchanged to t=47.4
v2-named 01:41:09  ret=3.2s  3533 (56968215) test:named  ->  3534 (6BDEDF65) between t=17.4 and 19.4
v2-named 01:52:14  ret=3.7s  3610 (25A0F0E3) test:named  ->  3611 (AEC45530) between t=11.9 and 13.9
v2-named 01:53:20  ret=3.4s  3617 (0B61D7F3) test:named  ->  3618 (1094ED7A) between t=7.7 and 9.7
v2-bare  01:42:26  ret=3.2s  3540 (F478B7B2) test:bare   unchanged to t=48.5
v2-bare  01:43:33  ret=3.3s  3565 (7CFEF9B8) test:bare   unchanged to t=47.4
```

The named form changed id in 3 runs of 4, at 4.3–16.2 s after the
return. The bare form kept its id and Message-ID in both runs. The
t=13.9 poll of the 01:52:14 run caught the swap mid-walk: it listed
3611 and then raised `Can't make id of item 2 of {… id 3611 …, … id 3610
…} into type text` for 3610.

## Observation 5: the visible-window path, closed with Save (`v3-reply`, `v3-fresh`)

**`v3-reply`** calls `connector.create_draft(seed="reply", seed_id=<seed>,
to=["probe@example.com"], body="spike note", from_account=<the test
account>, send_now=False)`. That goes through `_compose_note_above_seed`:
a visible reply window, the note pasted, then
`_save_compose_window_as_draft`.

- The `to` override was added to the brief's call so the saved draft
  held no real address.
- Each run used a different seed, so each reply subject was distinct.
- The seeds were three messages from earlier loopback test runs (subjects
  `ZZZ-AMM-INTEG-loopback-…-seed-<hex>`) in the test account's Deleted
  Messages mailbox. The test account's Sent Messages held one `ZZZ-AMM`
  message, sent at 01:29:57 by the concurrent run in progress. It was
  not used, because that run would trash it.

**`v3-fresh`** builds a fresh message from the connector's pieces
(`open_fresh_visible` in the harness):

1. `make new outgoing message with properties {subject:<S>,
   content:"x", sender:<"Name <addr>">, visible:true}`, after `activate`
   and a System Events window-name snapshot. Then a `to` recipient.
2. The window is found by `_as_new_compose_window_block`. It was named
   after the subject in every run.
3. `connector._save_compose_window_as_draft(window, subject, before_ids)`
   closes it with `_salvage_compose_to_draft` and finds the new id by
   subject-and-Drafts diff.

```
v3-reply 01:44:37  ret=10.0s  3569 (A92637D6) test:named  unchanged to t=54.2
v3-reply 01:46:07  ret= 9.8s  3576 (B0316CE5) test:named  unchanged to t=54.0
v3-reply 01:56:35  ret=22.8s  3650 (CBAFBE84) test:named  unchanged to t=67.1
v3-fresh 01:47:29  ret= 5.9s  3579 (CE091DE8) test:named  unchanged to t=50.1
v3-fresh 01:48:32  ret= 5.9s  3589 (58F44004) test:named  unchanged to t=50.0
v3-fresh 01:55:26  ret= 6.1s  3633 (FEE3C47A) test:named  trashed by another process between t=6.3 and 8.3
v3-fresh 01:58:55  ret= 6.3s  3660 (8B7512A9) test:named  unchanged to t=50.5
```

Six runs kept their id and Message-ID for the full 45 s. The seventh was
cut off after 2 s. In each, the sender read back as the named form that
was set. `_save_compose_window_as_draft` returned, so
`_salvage_compose_to_draft` had returned `SALVAGED`.

## Observation 6: `draft_update` on a named-sender draft (`v4-update`)

`v1-named`, then at once `apple_mail_mcp.tools.drafts.draft_update(draft_id=<it>)`,
patching nothing. `server.mail` was set to the harness's connector, and
nothing else was changed. The tool carried the sender over from the
draft.

```
01:49:36  create 3591 (00BDE2C1) ret=3.9s; update -> 3592 (99232194) ret=11.0s, success, no warning
          3592 unchanged to t=21.3; at t=23.3 two drafts: 3594 (1503D82A) and 3595 (68FBDF77),
          both test:named; unchanged to t=55.3
01:50:56  create 3605 (26632616) ret=3.9s; update -> 3606 (790FD5DD) ret=9.8s, success, no warning
          3606 -> 3608 (58030B84) between t=30.0 and 32.0; unchanged to t=54.0
```

The id `draft_update` returned was gone 10.3–12.3 s and 20.2–22.2 s
after the call returned. In the first run two drafts replaced it in the
same 2 s interval. The draft the update retired (3591, in Trash by then)
had also produced a copy. Neither copy can be told from the other: they
have the same content, sender and account.

## Observation 7: revealing the dictionary path's window and closing it (`v5-reveal`, beyond the brief)

This variant was not among the brief's four. It is an intervention on
the compose session that the dictionary path leaves open.

1. `v1-named`, then at once, through `_run_applescript`: `set visible of
   item 1 of (windows whose name is <S>) to true` in Mail. System Events
   then listed one window of that name. No sheet was on any window.
2. `connector._salvage_compose_to_draft(<S>)`.

```
02:00:39  create 3669 (E01F5059) ret=4.0s; revealed t=4.9; salvage SALVAGED t=6.6;
          still 3669 (E01F5059) at t=7.5; -> 3670 (75485A91) between t=17.7 and 19.7
02:01:58  create 3672 (4712587B) ret=4.1s; revealed t=5.6; salvage SALVAGED t=7.2;
          still 3672 (4712587B) at t=8.1; right after the close: Mail windows named <S> = 1,
          System Events windows named <S> = 0, outgoing messages with subject <S> = 1;
          -> 3679 (653C54AA) between t=28.6 and 31.6
```

Closing the revealed window saved no new copy right away, since the id
was the same about 1 s later. The re-save still came in both runs.
Mail's outgoing message for the subject and a hidden Mail window of
that name were both still there after the close. Whether the salvage
clicked a Save sheet or found none is not recorded:
`_salvage_compose_to_draft` returns `SALVAGED` either way.

## Observation 8: outgoing messages and windows, per subject

`count of (outgoing messages whose subject is <S>)`, read after the poll:

- **1** after every dictionary-path run: `v1-named`, `v1-none`, `v2-named`,
  `v2-bare` and `v5-reveal`.
- **2** after each `v4-update` run.
- **0** after every visible-window run (`v3-reply` and `v3-fresh`).

This held whether or not the id changed.

System Events listed no window named after any spike subject after any
run. Mail's own `windows whose name is <S>` counted 1 after some runs of
every variant, including three of the seven window-path runs. Probed once
at 01:47, the window of a `v3-reply` subject was `visible:false`. By the
final sweep at 02:04, no Mail window carried any spike subject.

The total `count of outgoing messages` was 44 at 01:30:26 before the
first run and 64 at 02:04:14 after the sweep. Of those, 18 carry spike
subjects, one per dictionary-path save (v1 ×6, v2 ×6, v4 ×4, v5 ×2).
The other 2 were not this spike's.

## Observation 9: where a replaced id goes, and the concurrent sweep

In every run whose id changed, the subject's count in `trash mailbox`
read before this spike's own cleanup was 0. The exceptions are the
`v4-update` runs, where it was 1 (the draft `draft_update` retires), and
the first `v1-named` run, which ran before the harness read Trash. So
the id replaced by a re-save leaves Drafts without passing through
Trash.

Three runs lost their draft to Trash in the middle of the poll, and it
was not this spike's cleanup:

- `v1-named` 01:34:54 at t=37.7–39.6. This spike's cleanup then found
  nothing, and a later read found the copy in Trash.
- `v1-none` 01:38:57 at t=34.6–36.5, with 1 in Trash before cleanup.
- `v3-fresh` 01:55:26 at t=6.3–8.3, with 1 in Trash before cleanup.

A concurrent integration test run was in progress at each of those times
(`ps`: `pytest … --run-integration`). Its session fixture
`_test_drafts_swept` (`tests/integration/conftest.py`) trashes every
test-account draft whose subject starts with `ZZZ-AMM-INTEG-`, and this
spike's subjects do too. Those three runs are reported only up to the
sample before the loss.

## Observation 10: what Mail showed

Two runs (01:36:42 and 01:56:35) found a sheet open on a window named
`discard-int-<hex>`. That window belongs to the concurrent integration
run's discard test. It was recorded and not touched. No sheet was open
on a window named after a spike subject at any read, and no dialog or
macOS permission prompt appeared.

## Cleanup (read 2026-09-27 02:04:14 EDT)

- Drafts with a spike subject: 0. The final `sweep` ran until 35 s passed
  with nothing new and found nothing.
- The three `Re: …` reply subjects: 0 in Drafts.
- `trash mailbox` holds 23 messages with `ZZZ-AMM-INTEG-spike-`
  subjects: one per run of that prefix (20), plus the retired draft and
  the second copy from the first `v4-update` run, plus the retired draft
  from the second. The reply runs' drafts carry `Re:` subjects, and went
  to Trash through the same delete.
- No window of any spike subject is open, in Mail or in System Events.
- 18 hidden outgoing messages with spike subjects remain. That is the
  dictionary path's known leak, one per save, and it is not fixed here.

## Observation 11: `v4-update` once every draft is saved from a window (2026-09-27 03:39–03:42 EDT)

Two more `v4-update` runs, on the branch that made `create_draft` save
every draft from a compose window closed with Save. Both the `v1-named`
create and the rebuild inside `draft_update` went that way. Same harness
and command as above. No integration pytest was running before or after
either poll, and ids are left out here.

```
03:39:13  create ret=10.1s; update ret=22.0s, success, no warning
          the returned id: the only draft of the subject, unchanged from t=22.1 to t=66.2
03:40:56  create ret=10.1s; update ret=22.1s, success, no warning
          the returned id: the only draft of the subject, unchanged from t=22.2 to t=66.3
```

In both runs, read after the poll and before the harness's own
cleanup:

- The draft read back `test:named` in the test account.
- `trash mailbox` held one copy of the subject: the draft the update
  retired.
- `count of (outgoing messages whose subject is <S>)` was 0, and the
  total was 66 before, after the poll and after cleanup.
- Mail's `windows whose name is <S>` counted 1 and System Events' 0, as
  after the window-path runs in Observation 8. No sheet was open.

Against Observation 6, where the id each run returned was gone within
23 s and the first run left a second copy.

## Derivations (mine)

1. **Which paths keep the id, within 45 s of the return, on this
   account type:**
   - The visible-window path kept it in 6 runs of 6, both for a reply
     with a note (the connector's path) and for a fresh message
     (assembled from connector pieces).
   - The dictionary path kept it when no sender was set (3 of 3, one cut
     at 34.6 s) and when the default account's bare address was set in
     the creation properties (2 of 2).
   - The dictionary path re-saved it with the `"Name <addr>"` form: set
     last (3 of 3), set in the creation properties (3 of 4), or carried
     by `draft_update` (2 of 2).

   The sender inside the creation properties does not suffice.
2. **The re-save now comes sooner than recorded.** On 2026-09-11 it came
   12–19 s after the save (8.5–30.6 s in later runs). Today it came
   0.2–2.4 s after `create_draft` returned in two runs, and 4–28 s in the
   rest. The DESIGN-QUEUE entry's "reliable for ~10 s" does not hold. The
   id `draft_create` returns for a named-sender draft can be stale before
   the caller makes its next call. Why the timing differs from
   2026-09-11 was not separated: Mail's load from the concurrent suite is
   one difference, and the day and machine state are others.
3. **Mechanism: consistent with, not established.**
   - In every run that re-saved, an outgoing message with that subject
     was still alive afterwards. After every window-path run, none was.
   - Surviving is not enough on its own: the no-sender and bare-default
     drafts also left an outgoing message alive and kept their id.
   - What fits every run here is an open compose session whose sender is
     not the one Mail would choose (the `"Name <addr>"` form counts, even
     for the default account), which Mail later saves again.
   - Observation 7 shows that ending the window is not ending the
     session. The dictionary path's window can be made visible and closed
     through its close button, and the outgoing message survives it, and
     so does the re-save.
   - What ended the session here was a compose that was visible from
     creation and closed through the UI.
   - Whether it is the session's survival or the save route (the
     dictionary's `save` against the UI's Save) that produces the re-save
     is not separated. No variant saved through the dictionary and then
     ended the session.
4. **Recommendation for `draft_create`: save every draft that names a
   sender through a visible compose window closed with Save.** That
   means `make new outgoing message … visible:true` for a fresh draft,
   `opening window true` for a reply or forward, then
   `_save_compose_window_as_draft`, which the reply and forward path
   already uses when it carries a note. On today's data it gives a
   stable id and Message-ID, it leaves no hidden outgoing message behind
   (the leak ends too), and `draft_update` and `draft_send` inherit it,
   since both rebuild through `create_draft`. What it costs:
   - Mail is activated and a compose window is on screen for about
     2–4 s of each save.
   - A fresh save took 5.9–6.3 s here, against 2.4–4.1 s on the
     dictionary path. One dictionary save took 15.5 s because it waited
     for the lock.
   - Each save depends on System Events (Accessibility) and on no other
     window having the same name (`COMPOSE_WINDOW_NOT_UNIQUE`).
   - Each save shares the UI with anything else driving Mail's windows,
     which here means the send paths and anyone using Mail at the
     machine.

   A cheaper option exists for one case. For the default account, the
   dictionary path could set no sender, or the bare address, instead of
   `"Name <addr>"`: 5 of 5 kept their id. Two things about it are
   **UNVERIFIED**. There is no read of which account Mail treats as
   default, and Observation 1 inferred it from behaviour. And whether a
   bare-address sender loses the display name in the saved draft's From
   was not checked. It does nothing for any other account: on 2026-09-11
   the other account's bare address re-saved.
5. `draft_update` also leaves a second copy in Drafts when the draft it
   retires had its own pending re-save (Observation 6, first run). This
   is the Observation 9 finding of `icloud-draft-resync.md`, seen now
   through the tool, and it is user-visible: a duplicate draft.
   With both saves made from a window, two runs of two left no copy
   (Observation 11). The copy came from the retired draft's own
   pending re-save, and a draft saved from a window had none pending in
   any run here. So it can still come only from a draft saved the old
   way, by an earlier version of this server or by another client, and
   retired within its re-save window. Two runs do not rule it out for
   the window path.
6. Mail's own window list is not a sign of an open compose session. A
   hidden window named for the draft showed up after window-path runs
   too, whose outgoing message was gone, and System Events never listed
   it (Observation 8). The per-subject outgoing-message count is the
   reading that separated the two paths in every run. Whether that
   hidden window is Mail's object for the saved draft is not
   established.

## Not tried

- The other account, or any account type other than iCloud. The brief
  confined live work to the test account, so the 2026-09-11 finding on
  the other account's bare address was not re-measured.
- A reply or forward without a note, which takes the dictionary path
  with `reply … opening window false`, with a sender.
- A forward with a note on the window path. Only replies were measured.
- A fresh visible-window draft with the sender set after creation rather
  than in the properties.
- What the window-path draft holds beyond its id, Message-ID, sender and
  account: its recipients and body were not read back.
- Polling beyond 45 s after the return. A later re-save would be missed.
  The Trash and Drafts listings at 02:04 show no late copy of any
  subject, but that is 2 to 30 minutes after each run, not a watch.
- Separating the save route from the session's survival (derivation 3).
- Which of `v4-update`'s two copies came from which compose session.
- Content in the creation properties, as the brief specified for `v2`.
  The connector avoids this because of a stray leading newline
  (`_build_creation_block`). Whether it affected anything here was not
  checked.

# AppleScript reads ISO dates as the year 12169

Observations first with the commands that produced them, derivations
labeled and last, gaps named. Same shape as
`attachment-property-10000.md`, per `docs/DISCIPLINE.md`.

This file exists because the defect below was confirmed on 2026-07-20
and written only into one agent's private memory. The tool went on
returning false empties for another seven weeks, because a note in an
agent's memory is not loaded by the session that needs it. The rule is
in `communication.md`: record it where it will load. For this project
that means here.

## Observation 1 — the coercion, verbatim

Run on macOS 26.5, 2026-09-07:

    $ osascript -e 'return (date "2026-09-01") as text'
    Monday, October 9, 12169 at 00:00:00

    $ osascript -e 'return (date "2026-01-01") as text'
    Sunday, October 1, 12169 at 00:00:00

    $ osascript -e 'return (date "1999-12-31") as text'
    Wednesday, July 12, 12197 at 00:00:00

**AppleScript's `date` coercion does not parse ISO 8601 and does not
fail when handed one.** It returns a date roughly ten thousand years in
the future. No error, no warning, and the value is a real date object
that compares cleanly against other dates.

## Observation 2 — what that did to search_messages

`search_messages` emitted, inside the per-message loop:

    if (date received of msg) < (date "2026-09-01") then set includeThis to false
    if (date received of msg) >= (date "2026-09-16") then set includeThis to false

Every real message is earlier than the year 12169. So:

- **`date_from` excluded every message.** Any value, any mailbox, zero
  rows, no error and no warning.
- **`date_to` excluded no message.** A silent no-op that returned
  everything.

Reproduced live 2026-09-07 through the MCP tool. A `subject_contains`
search of a real INBOX returned 17 rows; the identical call plus
`date_from: "2026-09-01"` returned `{"messages":[],"count":0}`, while
nine of those 17 rows were dated on or after that day.

## Observation 3 — a third instance, on a different path

`server.py` post-filtered the `source=[ids]` path with a string
comparison:

    str(m.get("date_received", "")) < date_from

Mail renders `date_received` as `Monday, September 7, 2026 at
18:29:25`. Compared against `"2026-09-01"`, `"M"` sorts after `"2"`, so
the test is never true and the bound never excluded anything. **Read
from the code, not reproduced live.**

## Observation 4 — when, and who was affected

Introduced 2026-04-21 in `897906a` (PR #65); current form from
2026-05-01 in `815fc31`. Live for about four and a half months.

`search_messages` tries IMAP first and falls back to AppleScript. The
IMAP path converts to a `SINCE` predicate and is correct, so only
accounts without a Keychain opt-in were affected. Checked 2026-09-07 on
the machine where this was found: every configured account raised
`MailKeychainEntryNotFoundError` on the opt-in check, so **every one
took the AppleScript path and every date-filtered search was wrong.**
The tested path was the one nobody used, which is why this survived.

## The fix

`applescript_iso_date_statements` in `utils.py` builds the cutoff by
assignment instead of coercion, once per search, before the loop:

    set dateFromCutoff to current date
    set day of dateFromCutoff to 1
    set year of dateFromCutoff to 2026
    set month of dateFromCutoff to 9
    set day of dateFromCutoff to 1
    set time of dateFromCutoff to 0

`day` is set to 1 before `year` and `month` deliberately: if today is
the 31st and the target month is shorter, setting the month first rolls
the date into the following month.

The literal form is locale-dependent even when it parses — `date
"9/1/2026"` reads differently under other regional settings — so the
string form is not correct anywhere and `parse_date_filter`, an unused
helper in `utils.py` that produced it, was deleted rather than fixed.
Its unit test had asserted `date "2024-01-15"`, locking the defect in.

## Why no test caught it

Unit tests mock `_run_applescript`, so they see the generated script
text and never its meaning. A test asserting the script contains
`date "2026-04-01"` passes whether or not AppleScript can read it.
That is why the IMAP branch was tested and correct while the
AppleScript branch was untested and wrong.

The regression tests are therefore integration tests, in
`TestDateFilterAppleScriptPath`, and they call
`_search_messages_applescript` directly rather than `search_messages`,
because on an account with an IMAP opt-in the correct IMAP path would
mask the defect entirely. They are not redundant with the unit tests
and cannot be replaced by them.

## Still open — ordering, same origin

Confirmed 2026-07-20, still true 2026-09-07, **not fixed**:

Reading `date received` of the first and last items of `messages of
mailbox` shows the first item is the newest message and the last item
is the oldest.

Mail returns `messages of mailbox` **newest-first**. The search loop
runs `repeat with i from total to 1 by -1`, which walks that list
backwards — oldest to newest. So `limit=N` returns the N **oldest**
messages. Measured on a several-hundred-message INBOX: `limit=5`
returned five messages from nearly four months earlier.

`815fc31` is titled "drop `whose` clause for reverse iteration" and its
comment claims "newest-first". The comment is wrong about the
direction. Left alone here because it is a separate defect with its own
behaviour change, and fixing it uninvited is out of this change's blast
radius.

## Gaps, deliberately named

- Observation 3 is read from code. Nobody has run the `source=[ids]`
  path against a real date bound before or after the fix.
- Only the accounts on one machine were checked. An account elsewhere
  with a Keychain opt-in would have been on the correct IMAP path.
- Nobody has audited what was concluded from a false empty during the
  four and a half months. Any search by date in that window returned
  zero regardless of the mail present.

Recorded 2026-09-07.

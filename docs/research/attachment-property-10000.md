# Attachment property reads raise -10000 (observations)

Recorded 2026-08-27 against the live machine. Format follows
`imessage-mcp/docs/EPISTEMICS.md`: observations with the command that
produced them first, derivations labeled and secondary.

Environment: macOS Darwin 25.5.0, Mail.app, the iCloud test account.

## Observation 1 — the reported symptom, through our own code

Reported by the yummy-vine session: `search_messages` / `get_messages` /
`save_attachments` all failed on iCloud INBOX messages 1463-1466.

```
uv run python -c "
from apple_mail_mcp.mail_connector import AppleMailConnector
c = AppleMailConnector(timeout=60)
for mid in ['1463','1464','1465','1466']:
    print(mid, c._enumerate_attachments_for_message(mid))"
```

Output (all four identical in shape):

```
1463 attachments= []
1463 warnings= ['attachment enumeration failed for message 1463: Mail got
                an error: AppleEvent handler failed. (error -10000)']
```

## Observation 2 — which property actually fails

Each attachment property read individually, under its own `try`
(osascript, `first message of mb whose id is <id>`):

```
== msg 1463
  count=1
  name=[3D RATIONALITY.pdf] mime=ERR-10000 size=[4216772] downloaded=[true]
== msg 1464
  count=1
  name=[3D RATIONALITY.pdf] mime=ERR-10000 size=[4216772] downloaded=[true]
== msg 1465
  count=1
  name=[Travel Expense Reimbursement Form2.pdf] mime=ERR-10000 size=[81314] downloaded=[true]
== msg 1466
  count=1
  name=[• Staples.pdf] mime=ERR-10000 size=[7255617] downloaded=[true]
```

`count of mail attachments` succeeded. `MIME type of att` raised -10000.
`name`, `file size`, `downloaded` of the SAME attachment read cleanly.

## Observation 3 — the `whose` locator is not the factor

The prior known -10000 class involved `whose`-located messages, so the
same read was repeated through a direct index reference (no `whose`):

```
index=4
direct-index mime=ERR-10000
```

Same failure. The locator form did not change the outcome.

## Observation 4 — scale, after the per-property guard landed

Full INBOX sweep, `has_attachment=True, include_attachments=True`:

```
messages w/ attachments (per Mail filter): 140
attachment records returned: 140
mime_type readable: 0
mime_type blank: 140
name blank: 0
messages that still returned ZERO attachment records: 0
total warnings: 140
whole-walk failures among warnings: 0
```

Every attachment probed in this mailbox (140/140) had an unreadable MIME
type; every one had a readable name. No message hit the whole-walk guard.

## Observation 5 — save form: bare string vs POSIX file

Same attachment, saved both ways, same run:

```
A(string path, /private/tmp/...): ERR -10000 To view or change permissions,
    select the item in the Finder and choose File > Get Info.
B(POSIX file, /private/tmp/...): OK

A(string path, ~/Downloads): OK
B(POSIX file, ~/Downloads): OK
```

The bare-string form failed in one of the two directories tested; the
`POSIX file` form succeeded in both.

## Observation 6 — files retrieved

Saved via `POSIX file` before any code change, sizes matching Observation 2:

```
4216772  1463_3D RATIONALITY.pdf
4216772  1464_3D RATIONALITY.pdf
  81314  1465_Travel Expense Reimbursement Form2.pdf
7255617  1466_• Staples.pdf
```

## Derivations (labeled — secondary to the above)

1. Building the attachment record from four inline property reads in ONE
   expression meant one unreadable property aborted the whole walk, so
   the whole-walk guard fired and reported "enumeration failed". Fix:
   read each property into its own variable under its own `try`
   (`_attachment_walk_block`). Observation 4 is the post-fix measurement.
2. Given Observation 4, before this fix attachment enumeration returned
   nothing for effectively every attachment-bearing message in this
   mailbox — not a rare edge case.
3. `save_attachments` bailed on ANY warning (`if warnings: return 0,
   warnings`). That was correct while the only warning meant "walk
   failed, list empty"; with property-level warnings it blocked saves of
   files that save fine. Fix: bail on "no attachments", not on warnings.
4. Pass 2 wrapped each save in an unqualified `try` with no on-error
   branch, so a failed save returned `(0, [])` — no files, no reason.
   That hid Observation 5 for a full debugging cycle. Fix: JSON
   `{saved, warnings}` with an on-error branch per save.
5. UNVERIFIED, worth checking: the pre-existing "inline-image multipart"
   explanation for the whole-walk -10000 may have been this same MIME
   type failure misattributed — the surfaced warning string is identical.
   Not tested; recorded so nobody treats the old explanation as settled.
6. FIXED 2026-09-09. Was: pass 2 builds the destination as
   `"<dir>/" & attName` from the attachment's own name, so a name
   containing `/` or `..` would write outside `save_directory`.

   **The mechanism is now probed rather than assumed.** In a scratch
   directory:

       set targetPath to ("<scratch>/safe/inner/" & "../../escaped.txt")
       set fh to open for access (POSIX file targetPath) with write permission
       write "traversed" to fh
       close access fh

   left a file at `<scratch>/escaped.txt` — two directories above the
   intended one. `POSIX file` does not normalise the string it is given;
   the filesystem resolves it at write time.

   **Fix is structural, not a filter.** Pass 1 already returns every
   attachment name to Python, so Python now sanitizes each one with
   `safe_attachment_filename` (utils.py) and passes the safe names into
   pass 2 as data. The AppleScript no longer derives the destination from
   `name of att` at all, which also subsumes the old in-script
   `attachment-N` fallback.

   **What is still NOT established, and the fix does not depend on it:**
   whether Mail.app's `name of att` ever returns a string containing `..`
   or a separator. Determining that needs a crafted message; the routes
   are a real send (blocked by the Claude Code classifier — see
   `mail-test-mode-unknown-at-runtime.md`) or importing a message into the
   live mailbox, which is a state change not worth making for a probe.

   The defect being fixed is therefore not "this is exploitable today".
   It is that the code depended on an undocumented upstream sanitization
   it never checked, in a property an external sender controls.

   **Coverage, stated honestly.** The unit tests in
   `TestSaveAttachmentsPathTraversal` are discriminating: they were RED
   against the old script and GREEN after. The integration test
   `test_saved_files_stay_inside_the_target_directory` asserts containment
   against real Mail but would also have passed against the old code,
   because it uses whatever names the inbox happens to carry rather than a
   hostile one. It guards against regression of the class, not against the
   original defect.

## Re-check commands

Observations 2/3/5 are osascript probes; the scripts are in this session's
transcript. Observations 1 and 4 re-run with the Python snippets above.

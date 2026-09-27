---
name: performance-patterns
description: Use when optimizing Apple Mail MCP operations, diagnosing slow queries, adding new filtering logic, or modifying how data is fetched from Mail.app. Covers osascript overhead, the cost of an Apple event, bulk property reads, batch operation patterns, and known operation timings.
---

# Apple Mail MCP Performance Patterns

## The Core Insight

**Two costs dominate: the subprocess and the Apple event.** Each `osascript` call costs 100-300ms regardless of what it does, so work is batched into one script. Inside a script, each Apple event to Mail costs about 15 ms (measured 2026-09-27), so a script that reads one property of one message at a time spends 15 ms per property per message. Mail answers a property for many messages in one event; see "Reading many messages at once".

## Known Operation Timings

| Operation | Time | Notes |
|-----------|------|-------|
| Single `osascript` call overhead | 100-300ms | Minimum cost per subprocess |
| One Apple event inside a script | ~15 ms | e.g. `subject of msg`; 813 ms for 50 messages read one at a time |
| `search_messages`, AppleScript path | ~1 s for 50 rows, ~0.5 s for 10, ~0.3 s for a subject filter matching nothing; ~1.5 s for a text filter that read a day's bodies for 20 rows, when no body was slow | Test account's INBOX, 2026-09-27; see "How the search reads" |
| `get_message` (single) | <1s | Direct ID lookup |
| `draft_send` | ~1-2s, plus a draft read and removal | The ~1-2s is Mail.app compose + send; reading the draft first and removing it after are further `osascript` calls |
| `update_message` (bulk read/flag) | ~1-2s | Single script for N messages via `_bulk_repeat_block` |
| `update_message` (move) | ~1-3s | Varies by account type (Gmail slower) |
| `save_attachments` | ~2-5s | Depends on attachment count/size |

## Reading many messages at once

Measured 2026-09-27 on the test account (iCloud), read-only, timed inside the script with `NSDate`. How these scale on mailboxes much larger than the test account's is not measured.

| Read | Cost |
|------|------|
| `subject of messages of mb` (every message, one event) | 16-26 ms for the whole INBOX |
| `sender` / `date received` of every message | ~25-32 ms |
| `read status` / `flagged status` of every message | ~15 ms |
| `message id` of every message | ~0.13 ms a message |
| `properties of to recipients of messages of mb` | ~0.3-0.4 ms a message |
| `mail attachments of messages of mb` | ~0.68 ms a message |
| `subject of messages a thru b of mb` (a range) | ~10 ms + 1.3 ms a message: 70 ms for 50, 250-285 ms for 200, 512 ms for 400 |
| `subject of <a list of message references>` | refused, error -1728: AppleScript does not distribute a property over a list |
| `a reference to («class mssg» id X of mb)` | no event (0.07 ms); `set r to «class mssg» id X of mb` costs one (~16 ms) |
| `content of messages a thru b of mb` | what each message's body costs, plus ~11 ms an event less than reading them one at a time; see below |

So a property of every message is far cheaper per message than a range, and a range far cheaper than one event per message. The matched messages cannot be read as a set: a list of their references is refused, so a caller reads positions instead.

**A body costs what its message costs.** `content` is not like the other properties: Mail answers it per message, at a price that depends on the message. Measured the same day, read-only:

| Bodies | Range of 20 | One range per message | One event per message |
|--------|-------------|-----------------------|-----------------------|
| 20 short, read lately | 116-127 ms | 330-342 ms | 324-326 ms |
| 20 older | 5.1-6.0 s | 5.1-6.1 s | 5.0-5.7 s |

Five other older messages cost 0.85-6.4 s each on the one read of them measured, and one message, whose body came back empty, cost anywhere from 17 ms to 10.7 s a read, alone or in a range, between reads seconds apart. So reading bodies in bulk saves the event (~11 ms a body), and nothing else: a slow body is as slow in a range. A search whose bodies include such a message swings by ten seconds or more from one run to the next, whatever the code does.

**Lists at a script's top level:** `item i of aList` slows with the list's length. Over 50,000 items (no Mail involved) the loop took 26.5 s directly and 83 ms through `set listRef to a reference to aList` then `item i of listRef`; over an INBOX-sized list read from Mail, 8.6 ms against 1.6 ms. Reach items of any list that can be large through a reference. Appending is fine directly (50,000 `set end of` in 160 ms).

## How the search reads

`AppleMailConnector._search_messages_applescript` builds on those numbers:

- **Each filter's property** (subject, sender, read and flagged status, date received) is read for the whole mailbox in one event and tested in the script. This cost grows with the mailbox, as the old loop's `messages of mb` read already did.
- **Each row property**, and each kind of recipient, is read once per run of matched positions (`<prop> of messages runStart thru runEnd of mailboxRef`). A run takes in up to 8 unmatched positions between two matches (`_SEARCH_RUN_GAP`): a range read's fixed ~10 ms is about what 8 more messages at 1.3 ms cost, so a wider gap is cheaper as a new run. This cost grows with the rows returned.
- **Attachments** have no cheap bulk form and are asked one message at a time, through a reference by id, only of messages the other filters kept.
- **Body and text** read the content over runs of the positions the other filters kept (`content of messages a thru b`), and only those: a run takes in no position they dropped (`_SEARCH_CONTENT_RUN_GAP` is 0), since the cheapest body costs about the event a gap would save and a dear one costs seconds. The kept positions wait until they are as many as the matches still wanted, or 50 (`_SEARCH_CONTENT_BATCH`), or the scan ends, so no body is read past the limit and no one read holds more than 50. A text filter's subject and sender are read for the whole mailbox, like a filter's property, rather than two events for each message.
- **Fallbacks:** a bulk read that fails is warned about and redone one message at a time. The lists must line up by position (each as long as the id list, a content run's ids, read after its bodies, the ones the filters kept, each row's id the one the filters matched, the first and last matched ids read again at the end); when they do not, the mailbox changed under the reads and the search runs again one message at a time, with a warning.
- **The limit** is a counter that ends the scan; with no filter the rows are the first `limit` positions, read as one run.

Medians of three, Mail-lock wait subtracted, the test account's INBOX, 2026-09-27:

| Search | Before (one event per property per message) | After (bulk reads) |
|--------|--------|-------|
| `limit=50` | 8.64 s | 1.05 s |
| subject filter matching nothing, `limit=50` | 13.53 s | 0.30 s |
| `limit=10` | 1.95 s | 0.54 s |

Body and text, the same INBOX and day, `limit=20`, the text a reserved domain found in no sender; main and the bulk body reads run in turn, three rounds, medians of three (each run in brackets):

| Search | Bodies one event each | Bodies in bulk |
|--------|--------|-------|
| `date_from` today, `text_contains` (20 rows) | 3.96 s [3.96, 9.48, 3.28] | 1.67 s [1.67, 20.52, 1.47] |
| `date_from` today, no text | 0.65 s | 0.66 s |
| `sender_contains` the same text (0 rows) | 0.28 s | 0.29 s |
| `text_contains`, no date (20 rows) | 9.75 s [9.75, 10.9, 3.36] | 1.56 s [1.56, 1.54, 1.82] |

Both text searches read the same bodies: the 20th match fell within the day, so neither read further. The runs of 9-20 s are most likely the message whose body came back empty being slow on that read (see "A body costs what its message costs"; it was the only slow body of the day's on four reads in a row), which both ways read once. Six more rounds of the two text searches: in bulk, 1.44-1.54 s on nine runs and 8.0-11.4 s on three; one event per body, 3.31-3.43 s on four and 7.3-13.1 s on eight.

### What was measured of `whose`, and why the search dropped it

`messages of mb whose <filter>` makes Mail evaluate the predicate across the whole mailbox before returning anything: over 120 s for permissive filters on an 8443-message MobileMe Sent folder, where testing each message in a script loop took about a second (#32). The search stopped using `whose` then, and does not use it now. `whose message id is "X"` is not indexed either (~21 s per lookup; see the applescript-mail skill). Reading a subject through `first message of mb whose id is X` cost about 18 ms a message on 2026-09-27, near one event.

## Pattern 1: Single Script Per Batch Operation

```python
# GOOD: One osascript call for N messages (near-constant time)
script = """
tell application "Mail"
    repeat with msgId in {id1, id2, id3}
        set read status of (first message whose id is msgId) to true
    end repeat
end tell
"""
self._run_applescript(script)

# BAD: N osascript calls for N messages (linear time)
for msg_id in message_ids:
    self._run_applescript(f'tell application "Mail" ...')
```

Batch operations should always build a single AppleScript that handles all items.

## Pattern 2: Use `limit` for Pagination

The `search_messages` tool accepts a `limit` parameter (default: 50). The script counts matches and stops at the limit, and reads rows only for the matches. Always pass a reasonable limit — fetching 10,000 messages when the user wants the latest 10 wastes time.

## Pattern 3: Accept Optional Account/Mailbox Parameters

Message ID lookup is O(accounts x mailboxes) when searching globally. Always accept optional `account` and `mailbox` parameters to narrow the search scope. If the caller knows which account, the search is dramatically faster.

## Anti-Patterns

- **Repeated subprocess calls in loops** — Build one script, execute once
- **One Apple event per message per property** — Read the property for every message, or a range, in one event
- **Fetching all properties when only some are needed** — Scripts currently fetch a fixed set of fields; when adding a new field, consider whether every caller needs it
- **A `whose` filter over a whole mailbox** — see above for what it cost
- **`item i` of a large top-level list** — go through `a reference to` the list
- **Default timeout too low for large operations** — Increase from 60s for bulk operations on large mailboxes

## Gmail Performance Notes

Gmail operations are inherently slower than IMAP because:
- Move requires copy + delete (two operations instead of one)
- Label operations don't map cleanly to folder operations
- Search across Gmail labels may scan differently than IMAP folders

## Profiling

No formal benchmarking infrastructure yet (see issue #31). When added:
- Use 5 iterations per operation
- Calculate mean, stdev, CV%
- Set thresholds at 5x documented baseline
- Detect cold starts (first run > 2x median of remaining)

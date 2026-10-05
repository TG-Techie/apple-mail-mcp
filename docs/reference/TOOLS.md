# Tools Documentation

Complete reference for all MCP tools provided by the Apple Mail MCP server.

## Overview

**Current Version:** v0.6.0
**Total Tools:** 27

## Phase 1 Tools (v0.1.0) - Core Foundation

### search_messages

Search for messages matching specified criteria.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Conditional | None | Account name (e.g., "Gmail", "iCloud"). Required when `source` is None; ignored when `source` is a list. |
| `mailbox` | string | No | "INBOX" | Mailbox/folder name. Ignored when `source` is a list. |
| `sender_contains` | string | No | None | Filter by sender email or domain |
| `subject_contains` | string | No | None | Filter by subject keywords |
| `read_status` | boolean | No | None | Filter by read status (true=read, false=unread) |
| `is_flagged` | boolean | No | None | Filter by flagged status (true=flagged, false=not flagged) |
| `date_from` | string | No | None | Inclusive lower bound on `date_received`. ISO 8601 YYYY-MM-DD. |
| `date_to` | string | No | None | Inclusive upper bound on `date_received` (full day included). ISO 8601 YYYY-MM-DD. |
| `has_attachment` | boolean | No | None | Filter messages with (true) or without (false) attachments |
| `limit` | integer | No | 50 | Maximum number of results to return |
| `source` | list[string] \| null | No | null | Optional list of message ids (with optional `"SELECTED"` sentinel) to scope the search to. `null` (default) searches the account/mailbox normally. |
| `include_attachments` | boolean | No | false | When true, each row includes an `attachments` field with per-attachment metadata. Default off — opt-in because the AppleScript fallback path can be slow on cold caches (#142). Free on the IMAP fast path. |
| `body_contains` | string | No | None | Substring match against message body content. IMAP: server-side `BODY` predicate (sub-second). AppleScript: reads the bodies of the messages the other filters kept (can be slow — see performance note). Case-insensitive. |
| `text_contains` | string | No | None | Substring match against headers + body (RFC 3501 `TEXT`). IMAP: server-side `TEXT` predicate. AppleScript: matches `content + subject + sender` (recipients omitted). Same perf characteristics as `body_contains`. |

**Notes:**
- Returns metadata-only rows (id, rfc_message_id, subject, sender, to, cc, bcc, date_received, read_status, flagged; see "Row fields" below). For full bodies, pipe the result ids into `get_messages([ids])`.
- Malformed `date_from` / `date_to` raise `error_type: validation_error`. Only ISO 8601 YYYY-MM-DD is accepted; relative dates like "7 days ago" are not supported.
- On the AppleScript path, the other filters are read for the whole mailbox at once. `has_attachment` is asked one message at a time, and `body_contains` / `text_contains` read bodies a run of messages at a time, in both cases only of messages the other filters kept, and bodies only as far as `limit` needs.
- `source=[ids]` (folded-in `get_selected_messages` and the `thread_of` use case) scopes the search to a specific id list. Filter parameters (`sender_contains`, `read_status`, etc.) compose with `source` — the resolved messages are post-filtered. The literal token `"SELECTED"` may appear in the list and is server-resolved to Mail.app's current UI selection (zero-or-more ids); mixed lists like `["SELECTED", "12345"]` are valid. Returns `account: null` and `mailbox: null` in the response. Missing ids drop out silently (partial-results convention).
- For thread retrieval, call `get_thread(message_id)` to expand an anchor into thread member ids; pipe those ids into `source=[ids]` for filtered metadata.
- Omitting both `account` and `source` returns `error_type: validation_error`.
- `include_attachments` defaults to **false** for `search_messages` (unlike `get_messages` which defaults to true). Reason: search results can span 50+ rows, and the AppleScript fallback path enumerates attachments per row — measured 1s for 50 messages but 97s for 100 cold-cache messages on a 47k-message Gmail INBOX (#142). To get attachment metadata for a small known set, prefer the two-step: `search_messages(...)` to get ids → `get_messages([those_ids])` (default-on attachments, bounded cardinality).

**Performance note for `body_contains` / `text_contains`:**

On the IMAP path, body search is server-side and sub-second. On the AppleScript fallback, body search is **dramatically slower** — measured 148s for 100 cold-cache messages on a 47k-message INBOX, vs 1s for `subject_contains` on the same slice. This is because Mail.app must read each candidate message's body, and what a body costs is the message's: on the test account (2026-09-27), a short body Mail had read lately cost about 6 ms, older ones 215-370 ms, and one message anywhere from milliseconds to over 10 s a read. The AppleScript path reads bodies in bulk, which saves the ~11 ms Apple event a body read on its own costs, but it cannot make a slow body quick; a date bound, a sender or a smaller `limit` means fewer bodies read. To get sub-second body search, run `apple-mail-mcp setup-imap --account <name>` to enable IMAP delegation for that account.

A search the AppleScript path cannot finish within the connector's timeout (60 s by default) answers `error_type: "timeout"`, not `applescript_error`: narrow it (a date bound, a sender, a smaller `limit`) and it may succeed.

When the call commits to the AppleScript path **and** a body/text filter is set, the response includes a `warnings` field describing the cost — see "Warnings" below.

**Warnings field:**

`search_messages` responses may include an optional `warnings: list[str]` field. The field is **omitted** when there are no warnings (don't pollute the cheap-call default case). It fires for AppleScript-path body/text search, surfacing the cost before the slow path runs, and on the AppleScript path for each row whose `to`, `cc` or `bcc` Mail could not read (see "Row fields"). The AppleScript path also warns when a property could not be read for many messages at once and was read one message at a time instead (`subject could not be read in bulk ...`), and when the mailbox changed while it was being read, so the search was done again one message at a time; the rows are complete either way, only slower. Example:

```json
{
  "success": true,
  "messages": [...],
  "count": 17,
  "warnings": [
    "AppleScript body search can take minutes on large mailboxes (measured 148s for 100 cold-cache messages on a 47k-message Gmail INBOX). Run `apple-mail-mcp setup-imap --account 'Gmail'` for sub-second IMAP body search."
  ]
}
```

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailbox": "INBOX",
  "messages": [
    {
      "id": "12345",
      "rfc_message_id": "CABc123@example.com",
      "subject": "Meeting Tomorrow",
      "sender": "john@example.com",
      "to": ["Jane Doe <jane@example.com>", "team@example.com"],
      "cc": [],
      "bcc": [],
      "date_received": "Mon Jan 15 2024 10:30:00",
      "read_status": false,
      "flagged": false
    }
  ],
  "count": 1,
  "limit": 50,
  "truncated": false
}
```

`truncated` is `true` when `count` reached `limit`: the result may be
the first page of more matches, so raise `limit` or narrow the filters
before treating it as the whole set.

**Row fields:**
- `id` — path-native: Mail.app internal numeric id when the AppleScript path runs, RFC 5322 Message-ID when the IMAP path runs. Fast for downstream same-path operations.
- `rfc_message_id` — RFC 5322 Message-ID (bracketless), or `null` when the message lacks a Message-ID header. Always present, regardless of which path produced the row. Accepted by the IMAP fast paths in `update_message` / `delete_messages` (#149 / #150 / #151 / #152) — the dual-emit means cross-path consumers don't need to know which path generated their input.
- `to`, `cc`, `bcc` — who the message went to, each a list of strings in the form `sender` uses: `Name <address>` when the header gives a display name, else the bare address. `[]` when the message has none of that kind. Both paths render them with one function, so the same message gives the same lists whichever path built the row, on `search_messages`, `get_messages` and `get_thread` alike.
  - `bcc` is only ever non-empty on a message the account itself sent: a received message carries no Bcc. Whether a sent copy kept its Bcc is up to the client that saved it.
  - On the AppleScript path each list is read on its own; one Mail cannot read comes back `[]` with a `warnings` entry naming the list and the message (`cc recipients unreadable for message 12345: ...`). An empty list is "none" only when no such warning names it.
  - On the IMAP path a group (`team: a@example.com, b@example.com;`) lists its members and not its name, so `undisclosed-recipients:;` lists no one. Display names written as RFC 2047 encoded-words are decoded, as Mail decodes them.

**Examples:**

```python
# Find all unread messages
search_messages(account="Gmail", read_status=False)

# Find messages from specific sender
search_messages(account="Gmail", sender_contains="john@example.com")

# Return metadata for Mail.app's current UI selection
search_messages(source=["SELECTED"])

# Scope to specific ids (e.g., from a prior get_thread call)
search_messages(source=["12345", "67890"])

# Mixed list: selection plus an explicit id
search_messages(source=["SELECTED", "12345"])

# Filter the selection to unread messages only
search_messages(source=["SELECTED"], read_status=False)

# Find messages with keyword in subject
search_messages(account="Gmail", subject_contains="invoice", limit=10)

# Complex search
search_messages(
    account="Gmail",
    mailbox="Work",
    sender_contains="@company.com",
    subject_contains="urgent",
    read_status=False,
    limit=20
)

# Check who a sent message went to, from its copy in Sent
sent = search_messages(account="iCloud", mailbox="Sent Messages",
                       subject_contains="Q3 report", limit=1)
row = sent["messages"][0]
row["to"], row["cc"], row["bcc"]   # read with sent.get("warnings")
```

**Error Codes:**

- `validation_error`: Malformed date, or neither `account` nor `source` given
- `account_not_found`: Specified account doesn't exist
- `mailbox_not_found`: Mailbox not found
- `unknown`: Unexpected error occurred

---

### get_messages

Retrieve full details of one or more messages, with bodies. Returns a list (always — possibly of length 0 or 1).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | List of message ids to fetch. May include the literal token `"SELECTED"` (server-resolved to Mail.app's current UI selection at call time). Mixed lists like `["SELECTED", "12345"]` are valid. Empty list is a no-op. |
| `include_content` | boolean | No | true | Include message bodies |
| `headers_only` | boolean | No | false | IMAP fast-path optimization for explicit ids; ignored on AppleScript fallback |
| `account` | string | No | None | Mail.app account name. With `mailbox`, activates the IMAP fast path for explicit ids (issue #72) |
| `mailbox` | string | No | None | Folder for the IMAP fast path (e.g. "INBOX") |
| `include_attachments` | boolean | No | true | When true, each message gains an `attachments: [{name, mime_type, size, downloaded}]` field. Default on for `get_messages` because id-list cardinality is bounded (typically 1-10) — cost is acceptable on both paths. |
| `include_links` | boolean | No | false | When true, each message gains a `links: [{url, text}]` field, read from its raw source. See "Links" below. |

**Notes:**
- Missing ids drop out silently — the response contains whatever was found (partial-results convention).
- The `"SELECTED"` sentinel is resolved server-side via `mail.get_selected_messages()` at call time. Empty selection expands to nothing.
- Pair with `search_messages` (metadata-only, criteria-based) and `get_thread` (thread member ids) to fetch bodies for specific messages.

**Performance note (path-dependent cost):**

For accounts configured with IMAP (via `apple-mail-mcp setup-imap --account <name>`), `include_attachments` is essentially free — `BODYSTRUCTURE` bundles into the existing FETCH. For accounts without IMAP, the AppleScript fallback enumerates attachments per message — fine for small id lists (1-10) but can be slow on cold caches for larger lists. If you have a mix of IMAP-configured and non-IMAP accounts, expect variance. To opt out: pass `include_attachments=False`.

**Returns:**

```json
{
  "success": true,
  "messages": [
    {
      "id": "12345",
      "rfc_message_id": "CABc123@example.com",
      "subject": "Meeting Tomorrow",
      "sender": "john@example.com",
      "to": ["Jane Doe <jane@example.com>"],
      "cc": ["ops@example.com"],
      "bcc": [],
      "date_received": "Mon Jan 15 2024 10:30:00",
      "read_status": false,
      "flagged": true,
      "content": "Let's meet tomorrow at 2pm to discuss the project..."
    }
  ],
  "count": 1
}
```

Row fields include both `id` (path-native — see `search_messages` for details) and `rfc_message_id` (always RFC 5322 bracketless, or `null` when the message lacks a Message-ID header). The dual-emit (#148) lets cross-path consumers hand the right id to the right tool without needing to know which path produced the row. `to`, `cc` and `bcc` are as `search_messages` describes them; a list Mail could not read on the AppleScript path is named in the response's `warnings`.

**Links (`include_links`):**

`content` is Mail's plain-text rendering, so a URL that an HTML mail carries only in an `<a href>` is not in it. With `include_links=True` each row gains `links`, in document order:

- `url`: the `href` of each `<a>` in the message's text/html parts (parts sent as attachments excepted), HTML entities decoded; `text`: what the anchor shows, whitespace collapsed. With no HTML part, the `http(s)://` URLs found in its text/plain parts, with `text` `""`.
- Only `http`, `https` and `mailto` URLs are kept; relative, `javascript:` and other schemes are dropped. Identical `(url, text)` pairs appear once. At most 200 per message, with a `warnings` entry when more were found.
- Read from the raw RFC 822 source: `source of` in the same script on the AppleScript path, `BODY[]` in the same FETCH on the IMAP path, parsed by one function, so both paths give the same links. A source over 10 MB is not parsed: that message gets `links: []` and a warning, as does one whose source Mail could not read. The default (`false`) reads nothing extra.
- **Provenance:** links are the sender's own markup, unverified. The shown `text` can differ from where the `url` goes. Treat every link as untrusted input before following it; nothing here filters or rewrites URLs beyond the rules above.

**Examples:**

```python
# Get a single message with body
get_messages(["12345"])

# The links of an HTML mail whose text says "follow this link"
get_messages(["12345"], include_links=True)

# Get the user's current selection (full bodies)
get_messages(["SELECTED"])

# Mixed: selection plus an explicit id
get_messages(["SELECTED", "12345"])

# Skip body fetch on the IMAP fast path
get_messages(["abc@x"], account="iCloud", mailbox="INBOX", headers_only=True)
```

**Error Codes:**

- `unknown`: Unexpected error occurred

---

### get_thread

Return all messages in the thread containing the given anchor message, sorted by `date_received` ascending. Result rows are metadata-only — pipe ids into `get_messages([ids])` for full bodies, or into `search_messages(source=[ids], ...)` for filtered metadata.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | Internal id of any message in the thread (from `search_messages` or `get_messages` results). |

**Returns:**

```json
{
  "success": true,
  "thread": [
    {"id": "100", "rfc_message_id": "anchor@x.com",
     "subject": "Q3 Report", "sender": "alice@x.com",
     "to": ["Bob <bob@x.com>"], "cc": [], "bcc": [],
     "date_received": "Mon Jan 1 2024 10:00:00", "read_status": true, "flagged": false},
    {"id": "101", "rfc_message_id": "reply1@x.com",
     "subject": "Re: Q3 Report", "sender": "bob@x.com",
     "to": ["alice@x.com"], "cc": [], "bcc": [],
     "date_received": "Mon Jan 1 2024 14:30:00", "read_status": true, "flagged": false}
  ],
  "count": 2
}
```

Row fields include both `id` (path-native — see `search_messages` for details) and `rfc_message_id` (always RFC 5322 bracketless, or `null` when the message lacks a Message-ID header). See `search_messages` for the dual-emit (#148) rationale, and for `to`, `cc` and `bcc`.

Uses the connector's tiered IMAP threading dispatch (Tier 1 X-GM-THRID for Gmail per #122, Tier 3 header-search BFS fallback) when IMAP is configured; falls back to AppleScript otherwise. The AppleScript path prefilters on subject and misses members whose subject was rewritten mid-thread; whenever it is the path that built the result, the response carries a `warnings` list saying so and why IMAP was not used (not configured, failed, or cooling down after a failure), and naming any row whose recipients Mail could not read. A response without `warnings` came from IMAP.

**Examples:**

```python
# Get the conversation around a message found via search
matches = search_messages(account="Gmail", subject_contains="Q3")
thread = get_thread(matches["messages"][0]["id"])

# Pipe thread ids into get_messages for bodies
ids = [m["id"] for m in thread["thread"]]
full = get_messages(ids)
```

**Error Codes:**

- `message_not_found`: Anchor message doesn't exist or was deleted
- `unknown`: Unexpected error occurred

---


---

### list_mailboxes

List all mailboxes (folders) for a specific account.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Account name (e.g., "Gmail", "iCloud") |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailboxes": [
    {
      "name": "INBOX",
      "unread_count": 5
    },
    {
      "name": "Sent",
      "unread_count": 0
    },
    {
      "name": "Archive",
      "unread_count": 2
    }
  ]
}
```

**Examples:**

```python
# List mailboxes
list_mailboxes(account="Gmail")

# List mailboxes for different account
list_mailboxes(account="iCloud")
```

**Error Codes:**

- `account_not_found`: Account doesn't exist
- `unknown`: Unexpected error occurred

---

### update_message

Patch one or more messages: change read state, flag color, and/or move to another mailbox in a single call. Replaces `mark_as_read`, `move_messages`, and `flag_message`.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | Up to 100 message IDs to update |
| `read_status` | boolean \| null | No | null | `True` marks read, `False` marks unread, `null` leaves unchanged |
| `flagged` | boolean \| null | No | null | `True` flags (red if no `flag_color` given — Mail.app's default flag color), `False` clears, `null` leaves unchanged |
| `flag_color` | string \| null | No | null | One of `orange`, `red`, `yellow`, `blue`, `green`, `purple`, `gray`, `none`. `"none"` clears the flag. Implies `flagged=True` for non-`none` values. |
| `destination_mailbox` | string \| null | No | null | Target mailbox name to move to. Requires `account`. |
| `account` | string \| null | No | null | Account name (required when `destination_mailbox` is set; also unlocks the IMAP narrow-path optimization) |
| `source_mailbox` | string \| null | No | null | Optional narrow-path hint — narrows the AppleScript scan to one mailbox. Required to unlock the IMAP fast path on move-only patches (#149) — without it, the move runs via AppleScript even when IMAP is configured. |
| `gmail_mode` | boolean | No | false | Use Gmail-specific copy+delete instead of move (label-based accounts) |

**Patch semantics:** caller specifies only the fields they want changed. At least one field parameter must be set; otherwise returns `validation_error`.

**Order of operations:** read-state and flag changes apply first (in the source mailbox), then the move. IMAP requires the message to exist in the source folder for STORE before MOVE.

**Performance — IMAP fast paths:**

- **Move-only patches (#149):** When `destination_mailbox` is the only field set and `source_mailbox` is provided, the move runs server-side via IMAP `UID MOVE`. On a 47k-message Gmail INBOX this drops the move from ~57s to <1s. Falls back to AppleScript when the server lacks `MOVE` / `UIDPLUS`.
- **Read-status-only patches (#151):** When `read_status` is the only field set and `account` + `source_mailbox` are provided, the read/unread mutation runs server-side via IMAP `UID STORE +/-FLAGS (\Seen)`. `\Seen` is base IMAP (RFC 3501), universal across all servers — no capability check needed.
- **Flag-only patches (#152):** When `flagged` is the only field set (no `flag_color`) and `account` + `source_mailbox` are provided, the flag/unflag runs server-side via IMAP `UID STORE +/-FLAGS (\Flagged)`. Same base-IMAP universality as `\Seen`. Bare `\Flagged` renders identically in Mail.app to the existing AppleScript default flag (verified empirically, no UI divergence). **Caveat on unflag:** calling `flagged=False` via this path on a message that was previously color-flagged removes `\Flagged` but does NOT remove the `$MailFlagBit*` color keyword Mail.app set — standard IMAP clients show no flag, but Mail.app may resurface the color on next sync. To clean both: omit `source_mailbox` (forces AppleScript, which also clears `flag index`), or use `flag_color="none"` instead.

Combined patches (move + read, read + flag, etc.) and any patch with `flag_color` set currently run via AppleScript regardless — Mail.app's color attributes (`$MailFlagBit*` user keywords) are out of IMAP scope. All fast paths require Keychain credentials per the IMAP setup flow (`apple-mail-mcp setup-imap --account <name>`); they fall back to AppleScript transparently when IMAP isn't configured.

**Returns:**

```json
{
  "success": true,
  "updated": 3,
  "requested": 3
}
```

`updated` is how many messages Mail actually changed; compare it with
`requested`, since an id that matched nothing is skipped rather than
reported.

**Examples:**

```python
# Mark messages as read
update_message(message_ids=["12345", "12346"], read_status=True)

# Flag a message red
update_message(message_ids=["12345"], flag_color="red")

# Clear a flag
update_message(message_ids=["12345"], flagged=False)

# Move to Archive on a Gmail account
update_message(
    message_ids=["12345"],
    destination_mailbox="Archive",
    account="Gmail",
    gmail_mode=True,
)

# Restore from Trash — no special verb required
update_message(
    message_ids=["12345"],
    destination_mailbox="INBOX",
    source_mailbox="Deleted Messages",
    account="iCloud",
)

# Combined: mark read, flag green, and move — all in one AppleScript pass
update_message(
    message_ids=["12345"],
    read_status=True,
    flag_color="green",
    destination_mailbox="Done",
    account="Work",
)
```

**Validation Rules:**

- Maximum 100 message IDs per request
- At least one of `read_status`, `flagged`, `flag_color`, `destination_mailbox` must be set
- `destination_mailbox` requires `account`

**Error Codes:**

- `validation_error`: Too many IDs, no fields set, or missing `account` for move
- `account_not_found`: `account` does not match a configured Mail.app account
- `mailbox_not_found`: `destination_mailbox` not found on the account
- `unknown`: Unexpected error occurred

---

## Coming Soon (Phase 2 - v0.2.0)


### create_mailbox

Create a new mailbox/folder.

**Parameters:**
- `account`: string - Account name
- `name`: string - Mailbox name
- `parent_mailbox`: string (optional) - Parent for nested mailboxes

### delete_messages

Delete messages (move to trash).

**Parameters:**
- `message_ids`: array[string] - Messages to delete
- `confirm`: boolean - Require confirmation

---

## Error Handling

All tools return a consistent error format:

```json
{
  "success": false,
  "error": "Detailed error message",
  "error_type": "error_category"
}
```

Every tool turns a failure into its `error_type` through one table, so a
failure answers with the same type whichever tool met it, and `error` is
the failure's own message. The per-tool lists in this document name the
ones each tool is likely to meet; any tool may answer with any of these:

- `validation_error`: Invalid parameters
- `account_not_found`: Account doesn't exist
- `mailbox_not_found`: Mailbox doesn't exist
- `mailbox_not_empty`: Mailbox still holds messages
- `message_not_found`: Message doesn't exist or was deleted
- `file_not_found`: A file or directory the call names doesn't exist
- `file_exists`: A file the call would write is already there
- `imap_required`, `unsupported_gmail_system_label`: A mailbox operation this account cannot do
- `rule_not_found`, `rule_changed`, `unsupported_rule_action`: Rules
- `draft_not_found`, `invalid_draft_id`, `draft_not_settled`, `draft_error`: Drafts
- `template_not_found`, `template_exists`, `invalid_template_name`, `invalid_template_format`, `missing_template_variable`, `template_error`: Templates
- `outbound_disallowed`: A recipient is off the outbound allowlist
- `allowlist_unavailable`: The outbound allowlist cannot be read, so nothing is sent
- `applescript_error`: Mail.app or `osascript` failed
- `timeout`: `osascript` did not finish within the connector's timeout
  (60 s by default) and was killed. The call was too slow rather than
  broken, so a narrower one may succeed; what the script had done by
  the time it was killed is not known.
- `unknown`: Unexpected error

Refusals answer before any work is done, with their own types:
`rate_limited`, `safety_violation` (test mode), `confirmation_required`
and `cancelled` (the confirmation prompt).

---

## Best Practices

### Search Performance

```python
# Good: Use specific filters
search_messages(
    account="Gmail",
    sender_contains="@company.com",
    read_status=False,
    limit=20
)

# Bad: Retrieve everything then filter
all_messages = search_messages(account="Gmail", limit=10000)
# ... filter in Python
```

### Error Handling

```python
# Always check success field
result = search_messages(account="Gmail")

if result["success"]:
    messages = result["messages"]
    print(f"Found {result['count']} messages")
else:
    print(f"Error: {result['error']}")
    print(f"Type: {result['error_type']}")
```

### Batch Operations

```python
# Good: Process in batches
message_ids = [...]  # Large list
for i in range(0, len(message_ids), 100):
    batch = message_ids[i:i+100]
    update_message(message_ids=batch, read_status=True)

# Bad: Single request with too many IDs
update_message(message_ids=message_ids, read_status=True)  # May fail if > 100
```

### Account Names

```python
# Use exact account name from Mail.app
# Check in Mail → Settings → Accounts

# Good
list_mailboxes(account="Gmail")

# Bad (won't work)
list_mailboxes(account="gmail")
list_mailboxes(account="my gmail account")
```

---

## Security Considerations

### Sending Emails

- All send operations require user confirmation
- Validate recipients before sending
- Limit recipient count to prevent spam
- Operations are logged for audit trail

### Input Validation

- All inputs are sanitized and validated
- Email addresses must match valid format
- Message IDs are sanitized
- File paths are validated (Phase 2+)

### Rate Limiting

- Bulk operations limited to 100 items
- Consider implementing additional rate limits for production use

---

## Phase 2 Tools (v0.2.0)


---

### save_attachments

Save attachments from a message to a directory.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | Message ID to save attachments from |
| `save_directory` | string | Yes | - | Directory path to save attachments |
| `attachment_indices` | list[int] | No | None | 0-based positions in the message's attachment list, in the order `get_messages` reports them (None = all). An index the message does not have is refused with `validation_error` and nothing is written. |
| `overwrite` | boolean | No | false | Replace files already in the directory. Without it, a name that is already taken is refused with `file_exists` and nothing is written. |

**Returns:**

```json
{
  "success": true,
  "saved": 2,
  "directory": "/Users/me/Downloads"
}
```

`saved` is the number of files written. A `warnings` list is present
when Mail could not read some attachment metadata (the files still
save); `saved: 0` with warnings means the enumeration itself failed.

**Examples:**

```python
# Save all attachments
save_attachments(
    message_id="12345",
    save_directory="/Users/me/Downloads"
)

# Save specific attachments only
save_attachments(
    message_id="12345",
    save_directory="/Users/me/Downloads",
    attachment_indices=[0, 2]  # Save the 1st and 3rd only (0-based)
)
```

**Security Notes:**
- Directory must exist and be writable
- Path traversal attacks prevented
- Filenames sanitized for safety
- Existing files are never replaced unless `overwrite=True`; a collision is
  refused before anything is written (`file_exists`). Attachments that share
  a name within one message are saved as `name.ext`, `name (2).ext`, …

**Error Codes:**

- `directory_not_found` / `invalid_directory`: `save_directory` is missing or is not a directory.
- `file_exists`: A name is already taken and `overwrite` is false; nothing was written.
- `file_not_found`: The directory was removed after that check, before the save.
- `validation_error`: An index the message does not have; nothing was written.
- `message_not_found`: The message doesn't exist or was deleted.
- `unknown`: Unexpected error.

---

### create_mailbox

Create a new mailbox/folder.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Account name to create mailbox in |
| `name` | string | Yes | - | Name of the new mailbox |
| `parent_mailbox` | string | No | None | Parent mailbox for nesting (None = top-level) |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailbox": "Client Work",
  "parent": "Projects"
}
```

**Examples:**

```python
# Create top-level mailbox
create_mailbox(
    account="Gmail",
    name="Archive"
)

# Create nested mailbox
create_mailbox(
    account="Gmail",
    name="Client Work",
    parent_mailbox="Projects"
)

# Create organizational structure
create_mailbox(account="Gmail", name="2024")
create_mailbox(account="Gmail", name="Q1", parent_mailbox="2024")
create_mailbox(account="Gmail", name="Q2", parent_mailbox="2024")
```

**Security Notes:**
- Mailbox names sanitized for safety
- Path traversal attacks prevented
- Special characters removed

---

### update_mailbox

Rename and/or re-parent (move) an existing mailbox.

**Two delivery paths:**

- **Rename only** (`new_name` set, `new_parent` is `None`): AppleScript's `set name of mailbox X to "Y"`. Fast, no IMAP credentials needed.
- **Move** (`new_parent` set, optionally combined with rename): IMAP `RENAME`. Requires IMAP credentials in Keychain (#73 opt-in flow) — returns `error_type: "imap_required"` when missing.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Mail.app account name or UUID (from `list_accounts`) |
| `name` | string | Yes | - | Current mailbox name. Slash-separated for nested mailboxes (e.g. `"Archive/2024"`) |
| `new_name` | string | No | None | Replacement leaf name. `None` keeps the current leaf when moving. Path-traversal characters stripped via `sanitize_mailbox_name`. At least one of `new_name` / `new_parent` is required. |
| `new_parent` | string | No | None | Destination parent path. `None` keeps current parent (rename-only). `""` (empty string) moves to top-level. Non-empty string moves under that path. |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "name": "Archive/2024",
  "new_name": null,
  "new_parent": "OldStuff"
}
```

**Examples:**

```python
# Simple rename (no IMAP needed)
update_mailbox(account="Gmail", name="ToDo", new_name="Tasks")

# Rename a nested mailbox (slash-separated path)
update_mailbox(
    account="Gmail", name="Projects/Q1", new_name="Q1-Archive",
)

# Move a nested mailbox to a different parent (IMAP)
update_mailbox(
    account="Gmail", name="Inbox/Projects/Q1",
    new_parent="Archive/2024",
)
# -> "Inbox/Projects/Q1" becomes "Archive/2024/Q1"

# Promote a nested mailbox to top-level
update_mailbox(account="Gmail", name="Inbox/Old", new_parent="")
# -> "Inbox/Old" becomes "Old"

# Move + rename in one IMAP RENAME
update_mailbox(
    account="Gmail", name="A/B", new_name="Renamed", new_parent="C",
)
# -> "A/B" becomes "C/Renamed"
```

**Caveat — Gmail system labels:** Renaming or moving a Gmail folder under
`[Gmail]/` (Drafts, Sent Mail, Trash, etc.) may not stick — Gmail's
IMAP server may auto-restore the canonical name. User-created Gmail
labels behave normally. Tracked as #164.

**Error Codes:**

- `validation_error`: Empty / whitespace-only `name`, missing both `new_name` and `new_parent`, or `new_name` sanitizes to empty.
- `imap_required`: Move requested but no IMAP credentials in Keychain for `account`.
- `mailbox_not_found`: No mailbox at `name`.
- `account_not_found`: `account` doesn't match any configured account.
- `applescript_error`: Mail.app rejected a rename for an underlying reason.
- `unknown`: Unexpected error.

---

### delete_mailbox

Delete a mailbox via IMAP. Mail.app's AppleScript dictionary doesn't
expose a working delete primitive for mailboxes (verified by probe), so
this operation requires IMAP credentials in Keychain (#73 opt-in flow).

**Always elicits user confirmation** (destructive). Refuses non-empty
mailboxes by default to prevent accidental data loss; pass
`delete_messages=True` to cascade.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Mail.app account name or UUID |
| `name` | string | Yes | - | Mailbox name. Slash-separated for nested. |
| `delete_messages` | boolean | No | False | When False, refuses if the mailbox contains messages. When True, cascade-deletes the mailbox and its contents. |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "name": "Old/Archive",
  "deleted_message_count": 0
}
```

`deleted_message_count` is 0 when the mailbox was empty; positive when
`delete_messages=True` cascaded.

**Examples:**

```python
# Safe delete (refuses if any messages)
delete_mailbox(account="Gmail", name="Old/Empty")

# Cascade-delete a non-empty mailbox
delete_mailbox(
    account="Gmail", name="Old/Archive", delete_messages=True,
)
```

**Error Codes:**

- `cancelled`: User declined the elicitation prompt.
- `validation_error`: Empty `name`.
- `imap_required`: No IMAP credentials in Keychain for `account`.
- `mailbox_not_empty`: Mailbox contains messages and `delete_messages=False`.
- `mailbox_not_found`: No mailbox at `name`.
- `account_not_found`: `account` doesn't match any configured account.
- `unknown`: Unexpected error.

---

### delete_messages

Delete messages — always moves them to the account's Trash mailbox.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | List of message IDs to delete |
| `permanent` | boolean | No | False | Reserved; currently a no-op. Passing `True` returns `permanent: false` and a `warning`. See [issue #111](https://github.com/s-morgan-jeffries/apple-mail-mcp/issues/111). |
| `account` | string \| null | No | null | Account name (or UUID). Pair with `source_mailbox` to narrow the scan and unlock the IMAP fast path (#150). |
| `source_mailbox` | string \| null | No | null | Mailbox the messages live in. Required to unlock the IMAP fast path (#150) — without it, the delete runs via AppleScript even when IMAP is configured. Either alone (without `account`) raises `validation_error`. |

**Returns:**

```json
{
  "success": true,
  "count": 2,
  "requested": 2,
  "permanent": false
}
```

`count` is how many messages were actually moved; compare it with
`requested`, since an id that matched nothing is skipped rather than
reported. `permanent` reports what happened and is always `false`:
Mail.app exposes no way to bypass Trash (#111), so asking for
`permanent=True` returns `false` with a `warning` saying the messages
went to Trash.

**Examples:**

```python
# Move messages to trash (cross-scan; finds them across all mailboxes)
delete_messages(
    message_ids=["12345", "12346"],
)

# Faster: narrow-scan or IMAP fast path when source is known
delete_messages(
    message_ids=["12345", "12346"],
    account="iCloud",
    source_mailbox="INBOX",
)
```

**Performance — IMAP fast path (#150):** When invoked with `account` and `source_mailbox`, the delete runs server-side via IMAP `UID MOVE` to the account's Trash folder. On a 47k-message Gmail INBOX this drops the operation from ~57s to <1s — the AppleScript path uses `whose message id is`, which is a linear scan against RFC 5322 Message-IDs. Trash folder is resolved via RFC 6154 SPECIAL-USE `\Trash`; falls back to conventional names (`Trash`, `[Gmail]/Trash`, `Deleted Messages`, `Deleted Items`). Capability fallback chain: `MOVE` → `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` (UIDPLUS only) → AppleScript. Requires Keychain credentials per the IMAP setup flow (`apple-mail-mcp setup-imap --account <name>`); falls back to AppleScript transparently when IMAP isn't configured or the server lacks both `MOVE` and `UIDPLUS`.

**Note on `permanent`:**

Mail.app's AppleScript dictionary exposes no path to permanent-delete that bypasses Trash. Calling `delete msg` always moves to the account's Trash; calling `delete` again on a message already in Trash is a no-op, and there is no `empty trash` command. The `permanent` parameter is preserved for API compatibility but currently has no effect; passing `True` returns `permanent: false` and a `warning` in the response so the gap is visible to the caller (the connector's `DeprecationWarning` fires in the server process, where an MCP client cannot see it). Track #111 for status.

**Safety Notes:**
- Bulk deletions limited to 100 messages for safety
- All deletes are recoverable from the account's Trash mailbox until that mailbox is emptied (typically by Mail.app's per-account "empty trash" schedule, configurable in Mail's preferences)

---

## Drafts Lifecycle (v0.7.0)

The drafts lifecycle replaces the v0.6 send group (`send_email`,
`send_email_with_attachments`, `reply_to_message`, `forward_message`)
with four tools that match Mail.app's actual primitive: every outgoing
message is a draft until it is sent. `draft_create` and `draft_update`
only save; `draft_send` is the one send from a draft, so the outbound
allowlist gate sits on one tool and a refused send leaves the draft as
it was. `email_send_html` sends without saving a draft first.

### draft_create

Create a draft (fresh, reply, or forward). Does not send; send it with
`draft_send`.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `reply_to` | string | No | None | Id of a message to reply to. Accepts either Mail's internal numeric id or an RFC 5322 Message-ID — pass the `id` field from any `search_messages` / `get_messages` row verbatim (#205). Mutually exclusive with `forward_of`. When set, `to`/`cc` recipients and `subject` are auto-derived (override by passing them explicitly). |
| `forward_of` | string | No | None | Id of a message to forward. Accepts the same id forms as `reply_to`. Mutually exclusive with `reply_to`. `to` is required (recipient of the forward). |
| `to` | array[string] | When fresh | [] | Recipient list. For reply/forward: empty keeps auto-derived; a populated list replaces. |
| `cc` | array[string] | No | [] | CC recipients (same semantics as `to` for reply/forward). |
| `bcc` | array[string] | No | [] | BCC recipients. |
| `subject` | string | When fresh | None | Subject. For reply/forward, `None` keeps Mail's `Re:`/`Fwd:` prefix. |
| `body` | string | No | "" | Body text, at most 10,000 characters; a longer body is refused (`validation_error`) rather than cut. Pasted as plain text (see **Composition**). For reply/forward, a non-empty body goes **above** what Mail wrote, which stays: the quoted original, or the forwarded message with its header block and every attachment Mail carried. An empty body leaves Mail's quote or forward exactly as Mail made it. |
| `attachment_paths` | array[string] | No | [] | List of file paths to attach, pasted into the body after everything else: on a reply or forward, after Mail's quote or forwarded message. Each must exist, must not carry an executable extension (`.exe`, `.sh`, …), and must be under 25MB — the same checks as `email_send_html`. |
| `reply_all` | boolean | No | False | For `reply_to` only — use `reply to all`. |
| `template_name` | string | No | None | Optional template to render for `subject` + `body`. Caller-supplied `subject`/`body` override the rendered output. |
| `template_vars` | object | No | None | Variables for the template renderer. Requires `template_name`. |
| `from_account` | string | No | None | Mail.app account name or UUID. None = Mail's default. The saved draft keeps it, and `draft_send` sends from it. |

**Returns:**

```json
{
  "success": true,
  "draft_id": "161055",
  "sent_message_id": "",
  "details": {"seed_kind": "new", "send_now": false}
}
```

`sent_message_id` is always empty here: `draft_create` saves and never
sends. `draft_send` returns the id of the message it sends.

**Composition:** every draft is composed in a visible compose window,
so Mail comes to the front for a few seconds of each save: a fresh
message made by `make new outgoing message`, or Mail's own reply or
forward window. The subject, recipients and sender are set on the
message, the body is pasted and read back, the files are pasted and
each seen in the window, and the window is closed with Save. The id
returned is the draft that save made, and in every run measured it
held for the 45 s watched. Drafts saved through Mail's scripting
dictionary instead, as they were until 2026-09-27, were re-saved by
Mail under a new id within seconds when they named a sender, held
their body inside a quote (`blockquote type="cite"`), so they went out
quoted when sent from Mail.app, and lost Mail's quote or forwarded
message when they carried a file on a reply or forward
(docs/research/draft-resave-spike.md,
docs/research/icloud-draft-resync.md, Observations 10 and 11). A
window of the same name already open stops the save with
`COMPOSE_WINDOW_NOT_UNIQUE`, and saving needs Mail.app UI automation
permission, as sending does.

**Examples:**

```python
# Save a fresh draft for later
draft_create(
    to=["alice@example.com"],
    subject="Project Update",
    body="Here's the latest..."
)

# Reply, save as draft (preserves Mail's auto-quote)
draft_create(reply_to="160989")

# Reply with custom body, then send it
r = draft_create(reply_to="160989", body="Sounds good, thanks!")
draft_send(draft_id=r["draft_id"])

# Forward with attachment
draft_create(
    forward_of="160989",
    to=["recipient@example.com"],
    body="FYI",
    attachment_paths=["/tmp/report.pdf"]
)

# Template-driven reply
draft_create(reply_to="160989", template_name="thanks-for-meeting")
```

**Error Codes:**

- `validation_error`: Mutually exclusive seeds, missing required fields, `template_vars` without `template_name`, or a body over 10,000 characters.
- `message_not_found`: `reply_to` / `forward_of` doesn't match any Mail.app message.
- `account_not_found`: `from_account` doesn't match.
- `file_not_found` / `validation_error` (attachments): a listed file is
  missing, has a blocked extension, or exceeds 25MB — no draft was created.
- `draft_not_settled`: Mail accepted the save but the new draft had not
  appeared in Drafts within 10 s, so there is no id to return. Look for
  the draft in Mail.app before saving again.
- `rate_limited`: more than 20 calls in 60 s to the `expensive_ops`
  tier, which draft saves, updates and deletes share with searches and
  the other mutations.
- `applescript_error`: a mechanical read-back of the compose window
  failed (`NO_COMPOSE_WINDOW:…`, `COMPOSE_WINDOW_NOT_UNIQUE:…`,
  `NO_BODY_AREA`, `PASTE_FAILED:…`, `ATTACH_MISSING:…`, `draft save:
  …`), and the error carries the actual UI state. A window a failed
  paste leaves is closed with Save, so what was composed so far is in
  Drafts, and the error ends with that outcome; a window whose name was
  not unique, or whose close failed, is left open (a close addresses a
  window by name, so none is made while another window has its name).
  Such a window is recorded, and the daemon closes it with Save once it
  is the only window of its name (docs/research/compose-window-tending.md).
  Also lower-level failures.
- `unknown`: anything else.

---

### draft_update

Update an existing draft. Implemented as **recreate-then-delete** —
Mail.app forbids mutating saved drafts, so this tool reads the
current state, creates a new draft with the merged fields, composed
as `draft_create` composes one (a visible compose window closed with
Save), and then removes the old one. Threading headers (for replies)
and forward anchors are preserved via persisted seed metadata. Does
not send.

**⚠️ Returns a NEW `draft_id`** — after a success the input id is no
longer valid. Callers caching the id must re-read the response.

The old draft is removed only after the new one exists, so a failure
at any point leaves it in Drafts under the id you already hold. If the
removal itself fails after that, the response is still a success and
carries a `warning` naming the old id, which is then still in Drafts.

**Your own part:** on a reply or forward, what `draft_update` and
`draft_send` hand back to Mail when they rebuild the draft is only the
text and files you gave `draft_create` or the last `draft_update`,
never what Mail reads back, which already has the quoted original and
a forward's own attachments; a draft created outside this server has no
record of your part and is rebuilt from everything Mail reads back.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `draft_id` | string | Yes | - | Mail.app id of the existing draft. |
| `to` / `cc` / `bcc` | array[string] | No | None | Override recipient groups: `None` keeps existing, `[]` clears, populated list replaces. |
| `subject` | string | No | None | Override subject. `None` keeps existing. |
| `body` | string | No | None | Override body. `None` keeps existing; non-None replaces (including `""`). |
| `attachment_paths` | array[string] | No | None | Override attachments: `None` **preserves existing** (extracted to a temp dir and re-attached, not re-checked; on a reply or forward only the files you attached, since a forward's own come with Mail's forward); `[]` clears; populated list replaces, and is checked like a send (exists, no executable extension, under 25MB) before the existing draft is touched — a refused list leaves the draft as it was. |
| `template_name` / `template_vars` | string / object | No | None | Optional template render. User-supplied `subject`/`body` override the rendered output. |
| `from_account` | string | No | None | Override sender. `None` keeps the draft in the account it was saved from (the sender is read back from Mail and carried over, so an update never silently moves a draft to Mail's default account). |

**Returns:**

```json
{
  "success": true,
  "draft_id": "161200",
  "sent_message_id": "",
  "details": {"seed_kind": "reply", "send_now": false}
}
```

**Externally-created drafts:** for drafts not created via `draft_create`,
seed recovery falls back to scanning Mail.app for the draft's
`In-Reply-To` header — this can take 30s+ on large mailboxes. Forward
seeds without persisted state are misclassified as fresh.

**Examples:**

```python
# Fix a typo in the body, keep recipients/attachments/threading
draft_update(draft_id="161055", body="Corrected body text")

# Add a recipient (replaces the to list)
draft_update(draft_id="161055", to=["alice@example.com", "bob@example.com"])

# Clear all attachments
draft_update(draft_id="161055", attachment_paths=[])

# Edit, then send the new id
r = draft_update(draft_id="161055", body="Final version")
draft_send(draft_id=r["draft_id"])
```

**Error Codes:** Same as `draft_create`, plus:

- `draft_not_found`: `draft_id` doesn't match any existing draft.
- `invalid_draft_id`: `draft_id` failed validation (path traversal, etc.).
- `draft_error`: an attachment to carry over could not be read out of
  the draft; nothing was changed.

---

### draft_delete

Move a draft to Trash. One-way discard for the lifecycle; Mail.app no
longer treats trashed drafts as editable.

A draft id names a draft in any account. The tool reads the draft's
account back from Mail before acting (the audit entry carries it), so
under `MAIL_TEST_MODE` a draft outside the test account is refused with
`safety_violation` and left as it is; `draft_update` and `draft_send`
do the same.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `draft_id` | string | Yes | - | Mail.app id of the draft. |

**Returns:**

```json
{"success": true, "draft_id": "161055"}
```

**Error Codes:**

- `draft_not_found`: `draft_id` doesn't match any existing draft.
- `invalid_draft_id`: `draft_id` failed validation.
- `rate_limited`: the `expensive_ops` tier is full, as for `draft_create`.

---

### draft_send

Send a saved draft: the one send from a draft. The draft is rebuilt and
sent through Mail, then removed; what is handed back to Mail is your
own part of it, as `draft_update` describes.

**⚠️ Security Note:** every recipient on the draft must be on the
outbound allowlist. One that is not refuses the whole send
(`outbound_disallowed`), and an allowlist that cannot be read refuses
every send (`allowlist_unavailable`). A refused send leaves the draft
exactly as it was. A send the allowlist does not already cover asks the
user to confirm.

Every draft goes out from the sender it was saved with, composed as
`draft_create` composes one and sent through the window's Send button
with the same mechanical read-back as `email_send_html`: a fresh draft
as plain text over the whole body, a reply's or forward's own text
above Mail's quote or forwarded message, with the draft's attachments
read out of the draft first and pasted after everything else.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `draft_id` | string | Yes | - | Mail.app id of the draft to send. |

**Returns:**

```json
{
  "success": true,
  "draft_id": "",
  "sent_message_id": "161300",
  "sent_rfc_message_id": "A1B2C3D4-0000-0000-0000-000000000000@example.com",
  "details": {"seed_kind": "reply", "send_now": true}
}
```

`sent_message_id` and `sent_rfc_message_id` name the copy the send
filed in Sent, found as `email_send_html` finds it (see **The Sent
copy** there); when it could not be identified both are `""` and a
`warnings` list says why, and Mail still accepted the message. The
old draft is removed only after Mail accepted the send; if that
removal fails, the response is a success carrying a `warning` naming
the draft, which is then still in Drafts.

**Error Codes:**

- `validation_error`: the draft has no recipients.
- `outbound_disallowed`, `allowlist_unavailable`: see the security note.
- `account_not_found`: the sender the draft was saved with matches no
  account in Mail; nothing was sent and the draft is unchanged.
- `safety_violation`: under `MAIL_TEST_MODE`, a draft outside the test
  account or a recipient outside the reserved test domains.
- `rate_limited`: too many sends in the window.
- `cancelled`, `confirmation_required`: the user declined, or could not
  be asked.
- `draft_error`: an attachment to carry over could not be read out of
  the draft; nothing was sent.
- `draft_not_found`, `invalid_draft_id`, `message_not_found` (a reply's
  or forward's original is gone), `applescript_error`, `unknown`.

---

### email_send_html

Send an HTML email directly — no draft is saved first. The body is
composed via clipboard injection into Mail.app's rich-text compose window
and sent immediately, with **mechanical dispatch verification**: a success
result means Mail accepted the message, its compose window closing after
Send with no sheet on it (see `docs/reference/UI_GROUNDING_MAIL_SEND.md`),
and the response names the copy this send filed in Sent, found by
identity (see **The Sent copy** below).

Three modes:

- **Fresh mail** (default): `to` and `subject` required.
- **Reply into a thread**: pass `reply_to=<message id>` (Mail internal or
  RFC 5322 id — use the thread's LATEST message, e.g. from `get_thread`).
  Mail carries the threading headers (`In-Reply-To`/`References`);
  `subject` defaults to the derived `Re: …`; recipients default to Mail's
  derived reply set. The HTML is pasted ABOVE the auto-quoted original.
- **Forward**: pass `forward_of=<message id>` (the same id forms) and
  `to`, which is required: Mail derives no recipient for a forward.
  `subject` defaults to Mail's `Fwd: …`. The HTML is pasted ABOVE Mail's
  forwarded message, which keeps its header block ("Begin forwarded
  message:") and the original's attachments. This is Mail's own forward
  of the message; a fresh message with the original pasted into it is
  not a forward.

**Reply-all:** there is no `reply_all` flag. Fetch the thread participants
(`get_thread`/`get_messages`) and pass them explicitly via `to`/`cc`.
Every recipient — derived or explicit — is validated against the outbound
allowlist with no exceptions; one off-list participant blocks the entire
send (the compose window is discarded; nothing partial is sent). A
recipient entry must be exactly one address (`addr@host` or
`Name <addr@host>`); an entry carrying two addresses, or none, is
off-list by definition, whichever addresses it contains. This holds on
every send path.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `to` | array | Fresh and forward modes | `[]` | Recipients. In reply mode, explicit values REPLACE Mail's derived set; a forward has none to derive. |
| `subject` | string | Fresh mode | `""` | Subject. Reply and forward modes derive `Re: …` / `Fwd: …` when omitted. |
| `body` | string | Yes | - | HTML string for the email body, at most 10,000 characters. |
| `cc` | array | No | `[]` | CC recipients (replace derived CC when replying). |
| `bcc` | array | No | `[]` | BCC recipients. |
| `from_account` | string | No | null | Mail.app account name or UUID. Null uses Mail's default sender. Set as the sender of the message composed, in every mode. |
| `reply_to` | string | No | null | Message id to reply to; enables reply mode. Mutually exclusive with `forward_of`. |
| `forward_of` | string | No | null | Message id to forward, in the same forms as `reply_to`; enables forward mode. |
| `attachment_paths` | array | No | `[]` | File paths to attach, in every mode, pasted after everything else: on a reply or forward, after Mail's quote or forwarded message. Each file must exist, must not carry an executable extension (`.exe`, `.sh`, …), and must be under 25MB. |

**Returns:**

```json
{
  "success": true,
  "draft_id": "",
  "sent_message_id": "161300",
  "sent_rfc_message_id": "A1B2C3D4-0000-0000-0000-000000000000@example.com"
}
```

**The Sent copy:** `sent_message_id` is Mail's id for the copy this send
filed in Sent (the id `get_messages` takes), and `sent_rfc_message_id`
its RFC 5322 Message-ID, bracketless as the read tools emit it. The copy
is found by identity: the ids of the Sent mailbox (every account's) are
taken before the compose window opens, and the copy is the one message
in Sent with the window's subject whose id was not among them. The
subject alone found the oldest message of that subject, so a send under
a subject used before had its files checked on an earlier message; and
the verified send, which once also waited 15 s for a message of the
subject in Sent, would report a message whose new subject's copy was
slower than that as not sent, though it had gone. The copy is looked
for once the window has closed, at once and then every second for
30 s. When it
cannot be identified (none appeared in that time, more than one new
message of the subject did, or looking failed), the response is still a
success, both ids are `""`, and a `warnings` list carries one entry
saying why and, when files were attached, that they are unverified:

```json
{
  "success": true,
  "draft_id": "",
  "sent_message_id": "",
  "sent_rfc_message_id": "",
  "warnings": ["Mail accepted the message (its compose window closed after Send, with no sheet), but its copy in Sent could not be identified: no new message with its subject appeared in Sent within 30s. No id is returned, and the files it was sent with are unverified; look in Sent and in Mail's Outbox before sending it again."]
}
```

**Error Codes:**

- `outbound_disallowed`: one or more recipients off the allowlist —
  nothing was sent; when it was one Mail derived for a reply, the
  compose window it was read from was discarded.
- `allowlist_unavailable`: the outbound allowlist cannot be read, so
  every send is refused — nothing was sent.
- `validation_error`: missing `to`/`subject` in fresh mode, missing `to`
  in forward mode, `reply_to` together with `forward_of`, or a body over
  10,000 characters (refused rather than cut) — nothing was sent.
- `message_not_found`: `reply_to` / `forward_of` matches no message in
  Mail — nothing was sent.
- `file_not_found` / `validation_error` (attachments): a listed file is
  missing, has a blocked extension, or exceeds 25MB — nothing was sent.
- `applescript_error`: a mechanical read-back failed
  (`SEND_DISABLED`, `SHEET:…`, `WINDOW_STILL_OPEN:…` (the window was
  still open 15 s after Send was clicked, with no sheet on it),
  `NO_COMPOSE_WINDOW:…`, `COMPOSE_WINDOW_NOT_UNIQUE:…`, `NO_BODY_AREA`,
  `PASTE_FAILED:…`, `ATTACH_MISSING:…`) — the error carries the actual
  UI state; the message was NOT sent, with ONE exception: an error
  saying "message WAS sent, but the sent copy lacks [...] of the files
  attached" means dispatch succeeded and the copy this send filed in
  Sent (named by its id in the error) does not carry every file —
  inspect that copy before resending. A compose window a failure
  leaves is closed with Save, so the message is in Drafts; the error
  ends with that outcome. When
  Mail could not send through the account's server, that outcome also
  carries the text of Mail's send-error sheet ("Cannot send message
  using the server …"). While another window has its name no close is
  made, and a window left open is closed with Save by the daemon later
  (docs/research/compose-window-tending.md).

**Composition:** every mode is composed in a visible window: a fresh
message made by `make new outgoing message`, or Mail's own reply or
forward window. The window is found by comparing Mail's window names
before and after (a window of the same name already open stops the send
with `COMPOSE_WINDOW_NOT_UNIQUE`). The subject, recipients and sender
are set on its message, and the recipients it then holds, those Mail
derived for a reply included, pass the outbound allowlist before
anything is pasted. The HTML is pasted and read back: over the whole
body of a fresh message; above Mail's quote or forwarded message on a
reply or forward, where an empty body leaves Mail's part as Mail made
it. Any attachments are pasted after everything else, as files. Each
must be mechanically visible in the compose window's AX tree before
Send is clicked (an image shows there inline, as an image, anything
else as an attachment button), and the copy this send filed in Sent is
checked after dispatch to carry every file, by name; a forward's carries
the original's files as well. This is the composition every saved draft
uses too (see `draft_create`). Read back from a delivered copy, a fresh
message carries nothing quoted: no `blockquote type="cite"`, which iOS
Mail draws as a purple bar (docs/research/icloud-draft-resync.md,
Observation 10). Attached through Mail's scripting dictionary instead,
a file on a reply or forward sent the original unquoted and cost a
forward the original's files (Observation 11).

**Limitations:** en-US Mail UI labels; sends require Mail.app UI
automation permission.

---

## Tool Combinations

### Example Workflows

**Inbox Zero Workflow:**

```python
# 1. Find all unread messages
unread = search_messages(account="Gmail", read_status=False)

# 2. For each message, get full details
for msg in unread["messages"]:
    full_msg = get_message(message_id=msg["id"])
    # Process message...

# 3. Mark processed messages as read
processed_ids = [msg["id"] for msg in unread["messages"]]
update_message(message_ids=processed_ids, read_status=True)
```

**Email Response Workflow:**

```python
# 1. Search for specific email
results = search_messages(
    account="Gmail",
    sender_contains="client@company.com",
    subject_contains="proposal",
    limit=1
)

# 2. Get full message
original = get_message(message_id=results["messages"][0]["id"])

# 3. Send the reply (email_send_html sends without saving a draft first)
email_send_html(
    reply_to=results["messages"][0]["id"],
    body="<p>Thank you for your proposal...</p>",
)
```

---

## Phase 4 Tools (v0.5.0)

### list_rules

List all Mail.app rules. Returns each rule's 1-based positional index, name, and enabled state. The `index` is the handle the mutation tools (`update_rule`, `delete_rule`) use to address a specific rule.

**Parameters:** None.

**Returns:**

```json
{
  "success": true,
  "rules": [
    {"index": 1, "name": "Junk filter", "enabled": true},
    {"index": 2, "name": "News From Apple", "enabled": false}
  ],
  "count": 2
}
```

**Field notes:**

- `index`: 1-based positional index, matching Mail.app's AppleScript reference (`rule N`). Indexes can shift if rules are reordered or deleted in Mail's UI between calls — re-fetch the list before mutating.
- `name`: Rule display name. **Not guaranteed unique** — Mail.app allows multiple rules with the same name. Use `index`, not `name`, for unambiguous addressing.
- `enabled`: Reflects the rule's toggle in Mail.app's Rules preferences.

---

### create_rule

Create a new rule. Appended at the end of the rules list.

**Parameters:**

- `name` (str, required): Display name. Need not be unique.
- `conditions` (list, required, ≥1): List of `{field, operator, value}` records. `field` ∈ `from`, `to`, `subject`, `body`, `any_recipient`, `header_name`. `operator` ∈ `contains`, `does_not_contain`, `begins_with`, `ends_with`, `equals`. When `field=="header_name"`, an additional `header_name` key is required to specify which header to test.
- `actions` (dict, required, ≥1 action): Any subset of `move_to`, `copy_to`, `mark_read`, `mark_flagged`, `flag_color`, `delete`, `forward_to`. `move_to`/`copy_to` take `{account, mailbox}`. `flag_color` ∈ `none`, `red`, `orange`, `yellow`, `green`, `blue`, `purple`, `gray` and is only meaningful with `mark_flagged: true`. `forward_to` is a list of email addresses, one address per entry, each on the outbound allowlist.
- `match_logic` (str, default `"all"`): `"all"` requires every condition; `"any"` requires at least one.
- `enabled` (bool, default `true`): Whether the rule is active immediately.

**Returns:**

```json
{"success": true, "rule_index": 7, "name": "From OmniFocus support"}
```

No confirmation prompt — creation is additive and the rule can be deleted afterward.

**A forwarding rule is a standing send.** Every message the rule matches, from then on, goes to its `forward_to` addresses with nobody reading each one, so those addresses meet the outbound allowlist exactly as a send's recipients do: an off-list address is refused with `error_type: "outbound_disallowed"`, an unreadable allowlist with `allowlist_unavailable`, and in either case nothing is installed. Under `MAIL_TEST_MODE`, `forward_to` may name only RFC 2606 reserved domains (`safety_violation` otherwise). A send may also reach the one address `MAIL_TEST_LOOPBACK` names; a rule may not, because it forwards every match for as long as it exists.

**Example:**

```python
create_rule(
    name="File OmniFocus replies",
    conditions=[{"field": "from", "operator": "contains", "value": "@omnifocus.com"}],
    actions={"move_to": {"account": "Personal", "mailbox": "Support"}, "mark_read": True},
)
```

---

### update_rule

Patch a rule's properties. Only the fields you pass are changed. Also serves as the enable/disable mechanism — pass `enabled=True|False` (the standalone `set_rule_enabled` tool was folded into this one in #130).

**Parameters:**

- `rule_index` (int, required): 1-based index from `list_rules`.
- `name` (str, optional): New display name.
- `enabled` (bool, optional): New enabled state.
- `match_logic` (str, optional): `"all"` or `"any"`.
- `actions` (dict, optional): When provided, **replaces** the rule's actions wholesale (per the same schema as `create_rule`'s `actions`). A `forward_to` is held to the outbound allowlist as in `create_rule`, and is refused before the confirmation prompt, so you are not asked to confirm a change that would then be blocked.

**Conditional confirmation:** prompts the user via MCP elicitation only when the patch touches `conditions`, `actions`, or `match_logic` (irreversible replacements). Patches limited to `enabled` and/or `name` skip the prompt — both are trivially reversible.

**Bound to the rule that was named:** `rule_index` is a position, and positions move when a rule is created, deleted or reordered — including while a confirmation prompt is open. The update applies only if the rule at that index still has the name the tool resolved (and showed you, when it prompted); the check and the change happen in one AppleScript call. Otherwise nothing is changed and the tool returns `error_type: "rule_changed"` — re-run `list_rules` and try again.

**Returns:**

```json
{"success": true, "rule_index": 7}
```

**Limitations:**

- **`conditions` cannot be replaced.** Mail.app on macOS Tahoe (16.0 / macOS 26) has a recursion bug in `-[MFMessageRule(Applescript) removeFromCriteriaAtIndex:]`: any AppleScript path that removes a rule condition (delete by index, delete every, or assignment of a new list) crashes Mail. `update_rule` raises `MailUnsupportedRuleActionError` if `conditions=` is passed. To change a rule's conditions, delete it with `delete_rule` and recreate with `create_rule`.
- Rules whose existing actions include unsupported types (`run AppleScript`, `redirect message`, `play sound`, `notify`, `reply text`, color-message highlights) raise `MailUnsupportedRuleActionError` to avoid clobbering settings outside our schema.

---

### delete_rule

Delete a rule by index.

**Parameters:**

- `rule_index` (int, required): 1-based index from `list_rules`.

**Confirmation:** elicits user confirmation before deletion, naming the rule at the index.

**Bound to the rule that was confirmed:** the delete applies only if the rule at `rule_index` still has the name shown in the prompt — checked in the same AppleScript call as the delete. A rule that moved while the prompt was open is not deleted; nothing is, and the tool returns `error_type: "rule_changed"` with both names. Re-run `list_rules` and confirm again.

**Returns:**

```json
{"success": true, "rule_index": 7, "deleted_name": "File OmniFocus replies"}
```

---

### list_accounts

List all configured email accounts in Apple Mail, with identity, type, and enabled state. Account ids are stable across name changes — prefer them over names when chaining into other tools.

**Parameters:** None.

**Returns:**

```json
{
  "success": true,
  "accounts": [
    {
      "id": "B21B254B-CC54-4DA4-B3D9-793E57A8E908",
      "name": "Gmail",
      "email_addresses": ["me@gmail.com"],
      "account_type": "imap",
      "enabled": true
    }
  ],
  "count": 1
}
```

**Field notes:**

- `id`: Account UUID. Stable across display-name changes; future tools may accept this in place of `name`.
- `account_type`: One of `imap`, `pop`, `iCloud`, `hotmail`, `iCal`, `smtp`. Derived from Mail's internal type constant.
- `enabled`: `false` for accounts the user has disabled in Mail.app preferences.

**Examples:**

```python
# Discover accounts before chaining into a mailbox listing
accounts = list_accounts()
first_enabled = next(a for a in accounts["accounts"] if a["enabled"])
list_mailboxes(first_enabled["name"])
```

---

## Email Templates (v0.5.0)

Store and reuse common reply / forward / send bodies. Templates live as
plain-text files on disk that you can edit in any editor; the tools
provide a programmatic CRUD layer plus a render step that does
placeholder substitution and pulls reply-context fields out of a
referenced message.

### Storage

Templates are files at `~/.apple_mail_mcp/templates/<name>.md`. Override
the location with the `APPLE_MAIL_MCP_HOME` environment variable
(`templates/` is appended automatically). The directory is created on
first save.

### File format

```
subject: Re: {original_subject}

Hi {recipient_name},

Thanks for reaching out.
```

The optional header block (`key: value` lines) is terminated by a blank
line; everything after is the body. The only recognized header in v1 is
`subject:`. Placeholders use Python `str.format` syntax: `{name}`. To
include a literal brace, double it: `{{` / `}}`.

### Placeholder substitution

`render_template` returns the rendered subject (or null) and body. The
following variables are auto-populated:

| Variable | When | Source |
|----------|------|--------|
| `today` | always | Current date, ISO format `YYYY-MM-DD` |
| `recipient_name` | when `message_id` provided | Display name parsed from the original sender |
| `recipient_email` | when `message_id` provided | Email parsed from the original sender |
| `original_subject` | when `message_id` provided | The original message's subject |

User-supplied `vars` always override auto-fills on conflict. Any
placeholder that's neither auto-populated nor user-supplied raises
`MailTemplateMissingVariableError` listing every unfilled name.

### list_templates

List all stored templates. Returns each template's name and subject
(may be null).

```json
{
  "success": true,
  "templates": [
    {"name": "polite-decline", "subject": "Re: {original_subject}"},
    {"name": "status-update", "subject": null}
  ],
  "count": 2
}
```

### get_template

Read a single template by name. Returns name, subject (may be null),
body, and the sorted list of placeholders found across subject + body.

```json
{
  "success": true,
  "name": "polite-decline",
  "subject": "Re: {original_subject}",
  "body": "Hi {recipient_name},\n\nUnfortunately I won't be able to take this on.\n",
  "placeholders": ["original_subject", "recipient_name"]
}
```

### save_template

Create a template, or replace one when explicitly asked to. Returns
`created: true` for new templates, `created: false` when an existing
template was replaced.

```python
save_template(
    name="polite-decline",
    body="Hi {recipient_name},\n\nUnfortunately I won't be able to take this on.\n",
    subject="Re: {original_subject}",
)

# A name that is already taken is refused unless the caller says so:
save_template(name="polite-decline", body="...", overwrite=True)
```

Without `overwrite=True`, saving to a name that already exists returns
`error_type: "template_exists"` and nothing on disk changes; the check
and the write are one exclusive create, so two callers racing on the
same name cannot both believe they created it.

No confirmation prompt: creating is additive, and replacing requires
the caller to name that intent. Names must match
`^[a-zA-Z0-9_-]{1,64}$`; anything outside that range (spaces, slashes,
dots, oversized) raises `invalid_template_name`.

### delete_template

Remove a template by name. **Elicits user confirmation** before deleting.

```json
{"success": true, "name": "polite-decline"}
```

### render_template

Render a template into ready-to-send text. **No side effects** — the
caller passes the rendered subject + body to `draft_create`, and sends
the draft with `draft_send`. For most workflows, use
`draft_create(template_name=...)` directly, which folds rendering into
creating the draft.

```python
# Render into a draft, then send it:
r = draft_create(reply_to="<abc@example.com>", template_name="polite-decline")
draft_send(draft_id=r["draft_id"])

# Standalone render for a "preview" workflow (no draft created):
rendered = render_template(
    name="status-update",
    vars={"project": "Q3 plan", "status": "on track"},
)
# rendered = {"success": True, "subject": "...", "body": "...", "used_vars": {...}}
```

User-supplied `vars` override auto-fills. Missing placeholders return
`missing_template_variable` error. Bad message IDs surface as
`message_not_found`.

---

## API Stability

- **Phase 1 (v0.1.x)**: Core tools stable
- **Phase 2 (v0.2.x)**: Attachments + management
- **Phase 3 (v0.3.x)**: Reply/forward
- **Phase 4 (v0.5.x)**: Discovery (list_accounts), email templates
- **Phase 5+**: Further enhancements, backward compatible

Breaking changes will only occur in major versions (1.0.0, 2.0.0, etc.).

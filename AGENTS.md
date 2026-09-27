# Apple Mail MCP Server

An MCP server bridging Claude and Apple Mail via AppleScript on macOS.

**Stack:** Python 3.10+, FastMCP, AppleScript (via `osascript`)
**Version:** v0.8.1 (gated by `./scripts/check_version_sync.sh`)
**Test and coverage counts are deliberately not recorded here** — they drift with every change and a stale number is worse than none. Get them from `make test` / `make coverage`.

## Commands

```bash
make test                  # Unit tests (~1s, mocked AppleScript)
make test-integration      # Real Mail.app tests (requires test account)
make test-e2e              # End-to-end MCP tool tests
make lint                  # Ruff linting
make format                # Ruff formatting
make typecheck             # Mypy strict mode
make check-all             # All checks (lint, typecheck, test, complexity, version-sync, parity)
make coverage              # Coverage report
./scripts/check_complexity.sh          # Cyclomatic complexity check
./scripts/check_client_server_parity.sh  # Verify all connector methods are exposed
./scripts/check_version_sync.sh        # Version consistency across files
```

**Running the server:** `uv run python -m apple_mail_mcp.server` (stdio) or via Claude Desktop config. As a fleet daemon (README, "Running as a fleet daemon"):

```bash
uv run mail-serve          # Resident daemon under pm2: HTTP on 127.0.0.1:41108 (--port to change)
uv run mail-proxy          # What a session launches: stdio proxy to the daemon (APPLE_MAIL_SERVER_URL overrides)
```

## API Surface (25 MCP tools)

**Core:** list_mailboxes, search_messages, get_messages, update_message
**Drafts lifecycle (v2 — verb-split):** draft_create, draft_update, draft_delete, draft_send
**Sending:** email_send_html (preferred send path; see docs/reference/TOOLS.md)
**Mailbox CRUD:** create_mailbox, update_mailbox (rename + move via IMAP), delete_mailbox (IMAP-only)
**Attachments & Management:** save_attachments, delete_messages
**Discovery & Rules:** list_accounts, list_rules, get_thread, create_rule, update_rule, delete_rule
**Templates:** list_templates, get_template, save_template, delete_template, render_template

### Drafts v2 — correct create → send pattern

Sending is ALWAYS a separate call. There is no auto-send. The split exists
so the outbound recipient allowlist policy gate sits on one obvious tool
(``draft_send``) and so failed sends leave the draft intact for review.

**Minimum lifecycle — 2 calls:**

```
draft_create(...)              → {"draft_id": "ABCD"}
draft_send(draft_id="ABCD")    → {"sent_message_id": "WXYZ"}
```

**With optional refinement (e.g., agent revising before sending):**

```
draft_create(...)              → {"draft_id": "ABCD"}
draft_update(draft_id="ABCD",  → {"draft_id": "EFGH"}   # id CHANGES
             body="revised")
draft_send(draft_id="EFGH")    → {"sent_message_id": "WXYZ"}
```

``sent_message_id`` is the Mail id of the copy the send filed in Sent,
beside its ``sent_rfc_message_id``; both are ``""``, with a
``warnings`` entry saying why, when that copy could not be identified.
Mail accepted the message either way.

``draft_update`` is implemented as recreate-then-delete; the returned id
is a NEW id, and the old draft is removed only after the new one exists,
so a failed update or send leaves it in Drafts under the id you hold.
Always use the returned id for the next call. Off-allowlist
recipients are fine on saved drafts; they are only blocked at
``draft_send``, and a blocked send is a pure no-op on Mail.app state.

The four ``draft_*`` tools are the implementation; there is no internal
create/update/delete layer beneath them. ``draft_update`` and
``draft_send`` rebuild the draft through the connector's
``create_draft``, and on a reply or forward they hand it only the
caller's own text and attachments, kept in the draft's seed record
(``drafts.py``), never what Mail reads back, which already carries the
quoted original and a forward's own attachments.

## Core Principles

- **TDD always** — RED/GREEN/REFACTOR. Tests before implementation.
- **Backend + frontend together** — Every feature touches `mail_connector.py` AND its tool module under `tools/`. Verify with `check_client_server_parity.sh`.
- **Sanitize everything twice** — All user input: `sanitize_input()` then `escape_applescript_string()` before AppleScript.
- **Structured responses** — Every tool returns `{"success": bool, ...}`. Errors include `error` and `error_type`.
- **Security checklist per feature** — see [`docs/guides/SECURITY_CHECKLIST.md`](docs/guides/SECURITY_CHECKLIST.md) for the canonical reference (6 concerns: input sanitization, AppleScript escaping, path-traversal-safe name validation, rate limiting, audit logging, the outbound allowlist on every path by which mail leaves). Don't duplicate guidance here; link out instead.
- **If you touched AppleScript, write integration tests** — Unit tests mock `_run_applescript()` and CANNOT catch AppleScript bugs.

## AppleScript Gotchas

**JSON output from AppleScript:** Scripts emit JSON via ASObjC + `NSJSONSerialization` (wrap with `_wrap_as_json_script`, parse with `parse_applescript_json`). Always quote the `name` record key as `|name|:` — the bare form is silently dropped during NSDictionary conversion. Coerce `missing value` to safe defaults (`{}` / `0`) before serializing. See applescript-mail skill for details. Observed 2026-09-27: a `resultData` holding `|mv_in_record|:missing value` and `|mv_in_list|:{1, missing value, "x"}`, run through `_wrap_as_json_script`, came back as `"mv_in_list":[1,null,"x"],"mv_in_record":null`; when the rejection happens is not established, so coercing stays the safe practice.

**Gmail mode:** Gmail's label-based system doesn't support standard IMAP move. The `update_message` tool has a `gmail_mode` parameter that uses copy+delete instead of move.

**Message ID lookup:** Finding a message by ID requires searching across all accounts and mailboxes. AppleScript `whose` clauses are used for efficiency.

**String escaping:** Always use `escape_applescript_string()` for user text. Unescaped quotes/backslashes break AppleScript silently.

**Attachment paths:** Use POSIX file references (`POSIX file "/path/to/file"`) in AppleScript. Path objects converted via `.as_posix()`.

**Timeout:** Default 60s, configurable via `AppleMailConnector(timeout=N)`. Some operations on large mailboxes may need more.

## Performance Constraints

- Each `osascript` subprocess call: 100-300ms overhead minimum
- Search, AppleScript path: about 1s for a 50-row page, 0.3s for a subject filter that matches nothing (the test account's INBOX, 2026-09-27). No `whose` clauses: it reads each property for many messages in one event, a filter's property for the whole mailbox and each row property once per run of matched positions (see `_search_messages_applescript`), so its filter cost grows with the mailbox and its row cost with the rows
- Send: ~1-2s
- Read: <1s per message
- Bulk operations capped at 100 items

## User Data on Disk

- All persistent user data lives under `~/.apple_mail_mcp/`. Override the location with `APPLE_MAIL_MCP_HOME=/some/path` (the subdirectory layout is appended automatically).
- Current layout: `templates/` (one `<name>.md` file per email template, see `src/apple_mail_mcp/templates.py`), `drafts/` (seed metadata per draft, `src/apple_mail_mcp/drafts.py`), `compose_windows/` (one record per compose window the connector opened and how it ended, which tending reads, `src/apple_mail_mcp/compose_ledger.py`), `audit.jsonl` plus one rotated generation `audit.jsonl.1` (one line per logged operation, `audit_log_path()` in `src/apple_mail_mcp/security.py`), and `mail_automation.lock` (the cross-process Mail automation lock).
- **`audit.jsonl` is personal data at rest**, not ordinary logging: it records who the user corresponds with and about what (recipients, subjects, sender account; never bodies). It lives outside the repository and is never committed, never pasted into a message or a report, and not something an agent reads to answer a question about the user's mail. It is bounded at about twice `AUDIT_ROTATE_BYTES` on disk. `compose_windows/` is too: a compose window's name is the subject of the mail in it. Records of closed windows go after seven days.
- Names that get used as filename stems must be regex-validated **before** building any path — see `_validate_name` in `templates.py` for the path-traversal-safe pattern. Don't `Path(user_input)` directly.
- Storage objects should resolve their root at use time, not import time, so env-var overrides and test-time monkeypatching are honored. Example: `get_template_store()` in `tools/templates.py`.

## Testing Requirements

| Type | When Required | How |
|------|--------------|-----|
| Unit tests | Every code change | `make test` |
| Integration tests | New/modified AppleScript | `make test-integration` |
| E2E tests | New/modified tools | `make test-e2e` |

**Hard rule:** If you wrote or modified AppleScript in the connector, integration tests must cover it before merge.

**Integration test safety:** When running tests via `server.py` tools, set `MAIL_TEST_MODE=true` and `MAIL_TEST_ACCOUNT=<test account name>`. The safety gate blocks destructive operations on non-test accounts (and refuses `delete_messages`/`update_message` that name no account at all, since message ids reach every account), refuses a `from_account` that is not the test account on the draft and send tools, refuses `draft_delete`/`draft_update`/`draft_send` on a draft that sits in another account (or one whose account Mail cannot name), and blocks sends to non-reserved recipient domains (must be @example.com, .test, .invalid, .localhost, etc.) on every send path, a rule's `forward_to` included. The optional `MAIL_TEST_LOOPBACK=<address>` admits that one real address as a send recipient (never as a rule's `forward_to`), so the read-back tests in `tests/integration/test_loopback.py` can read delivered mail back from the test account's INBOX; they skip without it. See `check_test_mode_safety` in [src/apple_mail_mcp/security.py](src/apple_mail_mcp/security.py).

## Operating Discipline

@docs/DISCIPLINE.md

## Branch Convention

`{type}/issue-{num}-{description}` — e.g., `feature/issue-42-thread-support`, `fix/issue-99-timeout`

CHANGELOG.md is only updated on release branches, never on feature branches.

## Skills

Load these skills when working in their domains:

- **release** — Full release workflow: milestone check, version bump, changelog, validation, tagging, PR
- **applescript-mail** — Apple Mail AppleScript patterns, quirks, workarounds, JSON emission via ASObjC
- **api-design** — Tool design philosophy, decision tree for new tools
- **integration-testing** — Real Mail.app testing, why mocks miss AppleScript bugs
- **performance-patterns** — Operation timings, the cost of an Apple event, bulk property reads, batch patterns, Gmail notes

## Key Files

- `src/apple_mail_mcp/mail_connector.py` — Core AppleScript client (~1120 lines)
- `src/apple_mail_mcp/server.py` — the FastMCP instance, the connector, confirmation, the error envelope and `main`
- `src/apple_mail_mcp/tools/` — the MCP tools, one module per domain
- `src/apple_mail_mcp/security.py` — Input validation, audit logging, confirmation flows
- `src/apple_mail_mcp/utils.py` — Pure functions: escaping, parsing, validation
- `src/apple_mail_mcp/exceptions.py` — Custom exception hierarchy
- `docs/reference/TOOLS.md` — Complete API reference

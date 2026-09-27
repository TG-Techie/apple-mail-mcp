# Architecture

## Component Diagram

```
Claude Desktop / MCP Client
        |
        | (MCP JSON-RPC over stdio)
        v
server.py (FastMCP) + tools/
  |-- 25 tools, one module per domain under tools/
  |-- Input validation
  |-- Response formatting
  |-- Error wrapping (exceptions -> dicts)
        |
        v
mail_connector.py (AppleMailConnector)
  |-- AppleScript generation
  |-- subprocess.run(["osascript", "-"])
  |-- Output parsing (JSON via ASObjC NSJSONSerialization)
  |-- Error routing (stderr -> typed exceptions)
  |-- Tries an IMAP fast path first for a few bulk operations,
  |   falling back to AppleScript on any IMAP-specific failure
        |                                   |
        v                                   v
Apple Mail.app                    imap_connector.py (ImapConnector)
  (via macOS Automation)            (direct IMAPClient session,
                                      credentials from Keychain)
```

Both paths end up mutating the same mailboxes; the IMAP path exists
purely for speed on operations AppleScript's `whose` clauses make slow
on large mailboxes (move-only, read-only, flag-only, and delete
patches with `account` + `source_mailbox` given). See
`docs/plans/2026-04-23-imap-connector-design.md` and
`performance-patterns` for when each path is taken.

## Two ways to run it: a server per client, or one daemon for all

The diagram above is the stdio shape: each MCP client launches its own
`apple-mail-mcp` process. Where many agent sessions share one Mail, the
same server runs instead as one resident daemon with a thin proxy per
session (README, "Running as a fleet daemon"):

```
session A --stdio--> mail-proxy --+
session B --stdio--> mail-proxy --+--HTTP 127.0.0.1:41108/mcp--> mail-serve (server.py, one process)
session C --stdio--> mail-proxy --+                                    |
                                                                       v
                                                                  Apple Mail.app
```

- `serve.py` (`mail-serve`) runs `server.mcp` over streamable HTTP on
  loopback only. Sync tools run in anyio's worker threads, as fastmcp
  dispatches them; the tools that reach Mail and also ask for
  confirmation are decorated with `_in_tool_threadpool` so their bodies
  run in the same pool and only the confirmation question returns to
  the event loop (`_confirm_from_threadpool`). Otherwise one session's
  osascript call would stall every session.
- `proxy.py` (`mail-proxy`) is what a session launches. It declares
  nothing of its own: tool listings, calls and elicitation are forwarded
  to the daemon, and the daemon's `instructions` are fetched when the
  proxy starts (docs/DISCIPLINE.md, "MCP context exposure"). With the
  daemon down a session's handshake fails outright rather than
  reporting an empty tool list (`provider_error_strategy="raise"`).
- The cross-process Mail lock (`_acquire_mail_lock`) serializes the
  daemon's own threads and any stdio server or test run beside it.
- The daemon tends Mail's compose windows (`tender.py`): a pass at
  start, every 15 minutes, and soon after a composition leaves its window
  open. A pass closes only the windows the compose ledger says the
  connector opened and nothing closed, and leaves and counts every other
  (docs/research/compose-window-tending.md). `--tend-interval 0` turns it
  off. A stdio server records its windows in the same ledger but does not
  tend.

What one process for all sessions changes, and is not yet decided, is in
the design queue: the per-process rate-limit budget and the in-memory
operation log become fleet-wide.

## Module Responsibilities

| Module | Role | Local dependencies |
|--------|------|-------------|
| `server.py` | The root the tools hang off: the FastMCP instance and its instructions, the connector (`server.mail`) and IMAP pool, the user's confirmation and the thread-pool helpers, the error table and `envelope` every tool answers through, and `main`. Imports the tool modules at its end | `mail_connector`, `imap_connector` (pool only), `security`, `exceptions`, `tools` |
| `tools/accounts_rules.py` | `list_accounts` and the rule tools; a forwarding rule's `forward_to` meets the outbound allowlist here | `server`, `outbound_allowlist`, `security`, `exceptions` |
| `tools/mailboxes.py` | The mailbox tools: list, create, rename or move, delete | `server`, `security` |
| `tools/messages.py` | The message tools: search, read, thread, update, save attachments, delete | `server`, `security`, `exceptions` |
| `tools/templates.py` | The template tools over `TemplateStore` | `server`, `security`, `templates` |
| `tools/drafts.py` | The draft tools, `draft_create` to `draft_send`; a rebuild takes the caller's own part from the seed record | `server`, `drafts`, `security`, `exceptions`, `tools.send`, `tools.templates` |
| `tools/send.py` | `email_send_html`, and the gates it shares with `draft_send`: the outbound allowlist, the user's confirmation, the checks on files to attach | `server`, `outbound_allowlist`, `security`, `exceptions` |
| `mail_connector.py` | All AppleScript generation and execution (`AppleMailConnector`); dispatches to the IMAP fast path for a few bulk ops; records every compose window it opens in the compose ledger, and runs a tending pass (`tend_compose_windows`) | `compose_ledger`, `compose_tending`, `drafts`, `imap_connector`, `keychain`, `outbound_allowlist`, `utils`, `exceptions` |
| `compose_ledger.py` | The record of every compose window the connector opens, by Mail's window id and process, and how each ended (sent, saved, salvaged, discarded, gone, or left open), one file per window under `<root>/compose_windows/` | `exceptions` |
| `compose_tending.py` | What a tending pass does, decided from an inventory of the compose windows and the ledger (`plan_tending`); pure | `compose_ledger` |
| `tender.py` | The daemon's tending thread and the logged, rate-limited pass it runs | `security`; `server` (imported at run time only) |
| `imap_connector.py` | Stateless IMAP client wrapper (`ImapConnector`) and a pooled-connection helper (`ImapConnectionPool`); deliberately unaware of Mail.app and Keychain — callers hand it resolved `(host, port, email, password)` | `exceptions` |
| `keychain.py` | Reads/writes IMAP passwords in the macOS Keychain under the `apple-mail-mcp.imap.<account>` service name; backs the `apple-mail-mcp setup-imap` CLI and the IMAP fallback path | `exceptions` |
| `outbound_allowlist.py` | The single point of truth for which recipient addresses may receive outbound mail. Sourced from a YAML config (`APPLE_MAIL_MCP_COMMS_CONFIG`); fails closed if that config is missing or unreadable. Consulted by the tools (`tools/send.py` refuses an off-list send before Mail is touched and skips elicitation for pre-trusted recipients; `tools/accounts_rules.py` checks a rule's `forward_to`) and by `mail_connector.py` (as the hard send-time block) | `exceptions` |
| `security.py` | Rate limiting, audit logging (`OperationLogger`), attachment validation, and the `MAIL_TEST_MODE` safety gate (`check_test_mode_safety`) that confines destructive/send/rule operations to a named test account and reserved test domains | `utils` |
| `drafts.py` | Persists seed metadata (`seed_kind`, `seed_id`, `reply_all`) and the caller's own text and attachment names per reply or forward draft under `<root>/<draft_id>.json`, since Mail.app forbids mutating a saved draft and `draft_update` and `draft_send` rebuild it | `exceptions` |
| `templates.py` | Email template storage and `str.format`-style rendering (`TemplateStore`, `Template`); one `<name>.md` file per template under `<root>/templates/` | `exceptions` |
| `serve.py` | The `mail-serve` console entry: the resident daemon, `server.mcp` over HTTP on loopback, and its tender | `tender`; `server` (imported at run time only) |
| `proxy.py` | The `mail-proxy` console entry: a per-session stdio proxy that forwards everything to the daemon and serves its instructions | `serve` (for the port and path); never imports `server` |
| `cli.py` | The `apple-mail-mcp setup-imap` subcommand; the no-subcommand path starts the MCP server via `server.main()` and is unaffected by this module | `mail_connector`, `imap_connector`, `keychain`, `exceptions` |
| `utils.py` | Pure functions: AppleScript string escaping/sanitizing, JSON parsing (`parse_applescript_json`), flag/rule field mapping, email/name validation | stdlib only |
| `exceptions.py` | The typed exception hierarchy every other module raises and `server.py` maps back to `{"error", "error_type"}` | none |

## Design Decisions

**Two-layer separation, plus a narrow IMAP escape hatch:** the server layer, `server.py` and the tool modules under `tools/`, is thin (MCP plumbing: gates, validation, response shape); `mail_connector.py` is thick (domain logic). Business logic never goes in the server layer. A tool module reads the connector as `server.mail` when a tool runs, and another tool module's helpers the same way, through the module (`send.confirm_send`): every tool module loads inside `server`'s own import, so the one imported first is still half-loaded while the rest load. `imap_connector.py` is not a third domain layer — it's a private acceleration path `mail_connector.py` reaches for when it has the credentials, always with an AppleScript fallback.

**Single execution point:** All AppleScript runs through `_run_applescript()`. This is the mock boundary for unit tests and the single place where timeout/error handling lives.

**Structured responses:** Every tool returns `{"success": bool, ...}`. Errors include `error` (message) and `error_type` (category). No exceptions reach the LLM.

**JSON output via ASObjC:** AppleScript results are serialized with `NSJSONSerialization` (`_wrap_as_json_script` in `mail_connector.py`) and parsed on the Python side with `parse_applescript_json` (`utils.py`). Record keys that collide with AppleScript selectors (e.g. `name`) must be written `|name|:` in the AppleScript literal or NSDictionary conversion silently drops them — see the applescript-mail skill.

**IMAP fast paths, AppleScript as the universal fallback:** for move-only, read-only, flag-only, and delete patches where the caller supplies both `account` and `source_mailbox`, `mail_connector.py` tries direct IMAP (via `imap_connector.py`, credentials from `keychain.py`) before falling back to the AppleScript `whose`-clause scan. Any IMAP-specific failure (missing Keychain entry, login error, unsupported capability, transient OS error) falls straight through to AppleScript rather than surfacing to the caller.

**Outbound allowlist as a hard perimeter:** `outbound_allowlist.py` is consulted on every path by which mail can leave — `email_send_html`, `draft_send`, and a rule's `forward_to` — and fails closed if its backing config is missing or unreadable. This is enforced in `mail_connector.py` at the point of dispatch, not only in `server.py`, so no new send-capable tool can bypass it by skipping a server-side check.

**Test-mode safety gate:** with `MAIL_TEST_MODE=true`, `security.check_test_mode_safety` confines destructive operations, named senders, and rule mutations to the account named by `MAIL_TEST_ACCOUNT`, and confines every send path to RFC 2606 reserved test domains. This is what makes `make test-integration` and `make test-e2e` safe to run against a real Mail.app.

**Gmail compatibility:** `update_message`'s `gmail_mode` parameter handles Gmail's label-based system (copy+delete instead of a native move) for the move branch of a patch.

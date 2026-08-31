# Operating discipline (apple-mail-mcp)

Project-specific discipline only. The general rules are fleet-wide and
live in one place:

- `~/AgentAccessEnv/fleet-config/operating-values.md` — how work is done
  (fail loudly, engineer structurally, scope and standing changes).
- `~/AgentAccessEnv/fleet-config/epistemics.md` — how you know what you
  claim, and how you record it (never presume/check, observation over
  conclusion, labeled derivations), with Jonah's 2026-08-23 verbatim
  words.
- `~/AgentAccessEnv/fleet-config/communication.md` — how it is spoken
  about (the two reader regimes, message shape, cadence, channels, asks,
  signals).
- `~/AgentAccessEnv/fleet-config/authorization.md` — what requires
  Jonah's approval, and how a standing change is granted and recorded.

Those four are @-imported into every session by `~/.claude/CLAUDE.md`.
Two companions sit beside them in the same directory and deliberately do
NOT load, so they cost no context: `comms-quotes.md`, Jonah's verbatim
words behind the comms rules, dated and sourced; and
`open-questions.md`. Read those from disk when you need them.

Do not restate any of them here. Copies drift: an agent-authored copy
of his comms doctrine was cited as his ruling for six days before
dusky-thorn traced it back on 2026-08-30. If a general rule seems missing from the fleet
files, raise it with whoever owns that thread to Jonah — do not write a
local version.

What follows is only what those files do not carry.

## Operator-thread etiquette not yet in the fleet file

From a rundown dusky-thorn gave me on 2026-08-26 at Jonah's direction,
after I twice misjudged who a thread message was for. This is an agent's
statement of his direction, not his verbatim words. These three points
are on the gaps list going to Jonah; if he rules them into
`communication.md`, delete them from here rather than keeping both.

- **Unnamed messages continue the active thread.** Read a message with
  no name prefix as directed at whoever Jonah was mid-conversation with,
  not at me. When genuinely ambiguous, ask ("was that for me?") rather
  than acting — acting on a misread creates duplicate work someone has
  to walk back. `communication.md` covers being addressed; it does not
  say who an unnamed follow-up belongs to.
- **Nothing is broadcast-to-all unless Jonah says "everyone".**
  `communication.md` rules on whether a message belongs in a
  multi-human thread; this is the narrower point that his messages
  default to one addressee.
- **Agent-to-agent coordination goes out-of-band**, over cross-session
  messaging — the iMessage thread is for Jonah. `communication.md` sets
  the agent-to-agent budget but does not name the mechanism.

## MCP context exposure (Jonah, 2026-08-26; relayed by dusky-thorn)

Directives on how much text an MCP server puts into agents' context.
Not in the fleet files; specific to building MCP servers.

- Mind what instruction/tool-description text the server exposes to
  agents that have NOT been directed to use it — the concern generalizes
  beyond any one MCP.
- Keep the base `instructions` block small. Tool search covers tool
  descriptions on demand; the base block is what every connected session
  always pays for. (Claude Code also silently truncates it at 2KB.)
- A per-session proxy's instructions must not be a separate
  hand-maintained text — proxy the daemon's own, appended JIT /
  non-cached at connection time, so a daemon update never leaves the
  proxy serving stale instructions or a stale version.

Grounded findings under these (2KB truncation, instructions never
deferred, initialize-handshake freshness bound) live with provenance in
imessage-mcp `docs/DECISION-LOG.md`.

## Epistemics, worked example in this repo

The doctrine is `epistemics.md` (record the observation not the
conclusion; a probe that finds nothing means "nothing found", never
"ruled out"; label derivations). It moved there from
`operating-values.md` in the 2026-08-30 three-way split. The raw record
its verbatim quotes were first written down in is imessage-mcp
`docs/EPISTEMICS.md`.

Held to that standard here: `docs/research/attachment-property-10000.md`
— observations with re-check commands first, derivations labeled last,
two marked UNVERIFIED. Follow that shape for research notes in this repo.

## Email (this project)

Outbound email is governed by the agent-email-discretion skill
(`~/iCloud/AgentAccessSync/skills/`): mandatory signature block with
runtime identifiers, allowlist gates, failure-event logging, draft caps.
Email is a delivery tool, not a comms channel — errors and questions go
to chat.

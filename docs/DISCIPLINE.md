# Operating discipline (apple-mail-mcp)

Project-specific discipline only. The general rules are fleet-wide and
live in one place:

- `~/AgentAccessEnv/fleet-config/operating-values.md` — for how the work
  is done.
- `~/AgentAccessEnv/fleet-config/epistemics.md` — for how you know what
  you claim, and how you record it so the next agent can check it.
- `~/AgentAccessEnv/fleet-config/communication.md` — for how any of it
  is spoken about, to Jonah or to another agent.
- `~/AgentAccessEnv/fleet-config/authorization.md` — for what needs
  Jonah's approval before you do it.

Each line above says what its file is FOR, deliberately, and not what it
currently contains. A purpose survives a rewrite; a summary of contents
rots silently while the path keeps resolving, so it reads as current
when it is not. Three files in this fleet described a moved or rewritten
target on 2026-08-31 alone, this one among them. Read the target from
disk before citing what it says.

Those four are @-imported into every session by `~/.claude/CLAUDE.md`.
Two companions sit beside them in the same directory and deliberately do
NOT load, so they cost no context: `comms-quotes.md`, Jonah's verbatim
words behind the comms rules, dated and sourced; and
`open-questions.md`. Read those from disk when you need them.

Do not restate any of them here. Copies drift: an agent-authored copy
of his comms doctrine was cited as his ruling for six days before
dusky-thorn traced it back on 2026-08-30. If a general rule seems
missing from the fleet files, raise it — do not write a local version.

What follows is only what those files do not carry.

## Operator-thread etiquette not yet in the fleet file

From a rundown dusky-thorn gave me on 2026-08-26 at Jonah's direction,
after I twice misjudged who a thread message was for. This is an agent's
statement of his direction, not his verbatim words.

These stay here by decision, not by default. Jonah put them to this
project on 2026-08-31 — "Rosy are their buisness leave them to them" —
after a fleet-wide doctrine cleanup he had already said cost more of his
time than he wanted to spend. Homing them in `communication.md` or
`epistemics.md` is a standing change and needs his approval, which means
reopening that thread; the only cost of leaving them local is that other
projects cannot see them. Not worth another round today. If they
are ever homed, delete them from here rather than keeping both — and go
in with the wordings below rather than rewriting them.

**Drafts, if the question is reopened.** Written 2026-08-31, never put
to Jonah, never approved. They are drafts, not doctrine, and carry no
authority until he rules on them; nothing may cite them as his.

- For `communication.md` § Channels, replacing the existing
  courtesy-not-protocol sentence so both halves of the rule sit in one
  paragraph: "Being addressed is often implied by context — the operator
  prefixes a name when he remembers, as a courtesy, not a protocol. The
  absence of a prefix is therefore not an opening: an unprefixed message
  continues the exchange it arrives in, addressed to whoever was last
  named in it. When that leaves it genuinely ambiguous, ask in one line
  rather than acting. Acting on a misread costs two agents' work and
  someone has to walk it back."
- For the same section, after the multi-human paragraph: "A message in a
  shared thread is addressed to one agent unless the operator says
  'everyone'. Default quiet covers whether to speak; this covers whether
  it was aimed at you. Reading a general remark as a general summons
  puts three agents on one task."
- For `epistemics.md`, as the reporting half of nothing-found: "State
  the zero. A check that ran and found nothing is a result: report it as
  one — 'no open contributor PRs', not silence. Silence is
  indistinguishable from not having looked, and it is read as the
  second. This is the reporting half of 'a probe that finds nothing
  means nothing found, never ruled out': the first governs what you may
  conclude, this governs what you owe the reader."

The explicit-zero rule also lives as a procedure step in
`.claude/commands/merge-and-status.md:36` and `:48`. That stays there
regardless — it is an instruction inside a procedure, not a doctrine
copy.

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

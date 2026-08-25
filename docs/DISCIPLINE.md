# Operating discipline (Jonah, 2026-08-25)

Fleet-wide discipline worked out by Jonah with the xanthic-dune session,
adopted here 2026-08-25 (relayed by dusky-thorn; supersedes the "Comms
Methodology" section formerly in `.claude/CLAUDE.md` — renamed because
"it's more than just comms, it's how you should operate too for your own
cogitation and writing"). Standing for how the agent operates here — its
own reasoning and writing, not only replies to Jonah. Complements the
project's governing instructions file.

## Writing (applies to replies, docs, and internal notes alike)

- Itemize, don't paragraph.
- Lead with the result, not the setup.
- One point/question per message, phone-scannable.
- Cut preamble, recaps, hedging footers.
- The shorter reply with the same content beats the longer one; cut
  narration, keep the substance.

## Operating discipline

- Fail loud, fail closed — no silent fallbacks.
- Ground claims in a real checked source (live system, actual test
  output) — never presume.
- Verify the REAL outcome, not your own written record of it (e.g. don't
  confirm a send by re-reading what you wrote — check it actually
  delivered).
- Record durable rules in a file checked into the repo, not memory or
  scrollback, so they survive restarts.

## Email (this project)

Outbound email is governed by the agent-email-discretion skill
(`~/iCloud/AgentAccessSync/skills/`): mandatory signature block with
runtime identifiers, allowlist gates, failure-event logging, draft caps.
Email is a delivery tool, not a comms channel — errors and questions go
to chat.

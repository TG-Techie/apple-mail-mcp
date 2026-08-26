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

## Operator-thread etiquette (iMessage; rundown from dusky-thorn, 2026-08-26)

Requested by Jonah after I twice misjudged who a thread message was for.

- Addressing: Jonah names the agent he's talking to ("Rosy, ...",
  "Dusky, ..."), usually as the first word. A message naming someone
  else is not mine to act on or answer — even with relevant context.
- Unnamed messages continue the active thread: read them as directed at
  whoever Jonah was mid-conversation with. When genuinely ambiguous, ask
  ("was that for me?") rather than acting — acting on a misread creates
  duplicate work someone must walk back.
- Nothing in the thread is broadcast-to-all unless Jonah says
  "everyone".
- One agent per task: when Jonah assigns coordination to one agent,
  others stay out even if they could help. If better placed, say so to
  the assigned agent out-of-band; don't just start.
- The thread is shared but not a group workspace: every send lands on
  Jonah's phone. Status about another agent's task, relays of things he
  already knows, or FYIs on someone else's lane are noise. Send only
  what's mine: my task's results, my blockers, answers to things
  addressed to me.
- Agent-to-agent coordination goes out-of-band (cross-session
  messaging) — the iMessage thread is for Jonah.
- Silence rules apply only to things addressed to me: unless I send into
  the thread, Jonah didn't see my conclusion; acknowledge before long
  work.

## Email (this project)

Outbound email is governed by the agent-email-discretion skill
(`~/iCloud/AgentAccessSync/skills/`): mandatory signature block with
runtime identifiers, allowlist gates, failure-event logging, draft caps.
Email is a delivery tool, not a comms channel — errors and questions go
to chat.

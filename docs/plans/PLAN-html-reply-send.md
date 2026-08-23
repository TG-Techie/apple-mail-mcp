# Plan — HTML email replies (threads and/or people)

> **Status 2026-07-21: IMPLEMENTED** (Phases 0–4, uncommitted). Jonah's
> rulings: paste above quote; hard fail on any off-list recipient, no
> exceptions; NO `reply_all` parameter (reply-all = explicit `to`/`cc`
> from the thread, directed in the tool docs); shape = extend
> `email_send_html`. Notable deviation from the draft plan: recipients
> are read from the outgoing-message MODEL (`address of to recipients`),
> not UI fields (which return display pills, not addresses), and the
> mailto path now sends directly from the live compose window (the
> close/reopen dance was itself the vanished-send bug — see the grounding
> report). Integration: 5/5 green incl. on-the-wire In-Reply-To/References
> verification. Outstanding: one-time iOS purple-bar visual re-check.

Prepared 2026-07-20. Grounded in [UI_GROUNDING_MAIL_SEND.md](../reference/UI_GROUNDING_MAIL_SEND.md)
(empirical AX map, silent-failure catalog) and the current code
(`_send_new_via_eml`, `_send_html_email` in `mail_connector.py`). Everything
UI-touching follows the session's standing rule: **every UI action gets a
mechanical read-back in the AppleScript itself** — explore, verify
precondition, act, verify postcondition; loud descriptive failure otherwise.

## Goal

Send an HTML email **as a reply** — into a thread (threading headers intact)
and/or to explicit people (recipient overrides) — with the same allowlist
policy guarantees as every other send.

## API surface (per api-design decision tree: extend, don't add)

Extend the existing `email_send_html` tool:

```
email_send_html(
    to=[...],                  # optional when reply_to given (auto-derived)
    subject="...",            # optional when reply_to given (Mail derives "Re: …")
    body="<html…>",           # unchanged, HTML string
    cc=[...], bcc=[...],
    from_account=None,         # BECOMES HONORED (AXPopUpButton is settable)
    reply_to=None,             # NEW: message id (Mail internal or RFC 5322 per #205)
    reply_all=False,           # NEW
)
```

No new tool. `draft_*` lifecycle untouched. Validation: `reply_to=None`
requires `to` + `subject` (current behavior); `reply_all=True` requires
`reply_to`.

## Policy invariant (non-negotiable)

The allowlist gate runs on the **full, explicit, mechanically-read recipient
set** before anything is pasted or sent. For replies, Mail derives the
recipients — so the flow reads them back out of the compose window's To/Cc
fields, extracts addresses, and passes them through
`assert_recipients_allowed_for_send` explicitly (the gate already refuses
auto-derived reply recipients — this satisfies it rather than bypassing it).
If any recipient is off-list (likely with `reply_all`): **fail loud, discard
the compose window (verified), send nothing**. No silent dropping of
off-list participants.

## Phases

### Phase 0 — Verified-action send primitives (prereq; fixes today's bug)

Shared AppleScript fragments (Python-side builders, unit-testable):

1. `find_compose_window` — locate by **window-set diff** (names before vs
   after opening), not `window 1` and not subject guessing. Returns the
   window's actual name for later postcondition checks.
2. `assert_send_ready` — Send button exists **and `enabled=true`** (the
   grounding report's headline finding: clicking a disabled button is a
   silent no-op — the vanished-send mechanism). On failure: return the
   toolbar descriptions + field values actually present.
3. `verified_send` — click Send by AX reference, then poll (15×1s idiom):
   compose window gone AND message with that subject in the account's Sent
   mailbox. A sheet appearing = error carrying the sheet's static texts.
   Only then `SENT`.
4. `discard_compose` — close button → wait for "Save this message as a
   draft?" sheet → click `Don’t Save` (**U+2019 curly apostrophe**; there is
   no "Delete Draft") → verify window gone. (Both Mail-dictionary discard
   routes fail silently — documented in the grounding report.)
5. Clipboard save/restore moved to a guaranteed path (currently leaked on
   mid-script AppleScript errors — restore must run on every exit).

Retrofit `_send_new_via_eml` and `_send_html_email` onto these primitives.

Tests: unit (script-builder output contains each check); integration
(MAIL_TEST_MODE send to reserved domain; deliberately-empty recipients →
expect loud `SEND_DISABLED`-class error and recoverable state).

### Phase 1 — Open + identify the reply compose window

- `reply (message …) opening window yes reply all <bool>` via the message
  lookup that already exists (coordinates with the in-flight `id_where`
  refactor in the working tree).
- Identify the new window by set-diff; capture its derived name ("Re: …")
  for Phase 3's Sent-mailbox postcondition.
- Poll until the To field is non-empty (Mail populates async — this is the
  timing race that likely ate the 17:40 send), read To/Cc values back,
  extract addresses → run the policy gate.
- `to`/`cc`/`bcc` args override: clear field, type, **read value back and
  compare** before proceeding.

### Phase 2 — HTML injection into the reply window

- Reuse the clipboard pattern (`public.html` flavor, save/restore).
- Paste at top: `cmd+up` first, so Mail's quoted original stays below the
  new HTML (default: preserve the quote — see open question 1).
- "Make Rich Text": check which menu item exists (`Make Rich Text` vs
  `Make Plain Text`) instead of blind try — that's the mechanical read of
  current format state.
- **Paste read-back (needs a probe):** embed a short unique sentinel in the
  HTML, then read the WebArea's text content to confirm the paste landed
  before sending. The AX read path for WebArea text needs one empirical
  probe (grounding report mapped the nesting: group > scroll area >
  AXWebArea); if text isn't cleanly readable, fall back to detecting the
  WebArea's `AXValue`/child-count change across the paste. Do the probe
  first, then lock the mechanism.

### Phase 3 — Verified send + from_account

- Phase 0's `verified_send`, unchanged.
- `from_account`: set the From `AXPopUpButton` (mechanically settable,
  value readable — grounding verified), read the value back, fail loud on
  mismatch. This removes the "from_account is ignored" limitation from
  BOTH html-send and mailto-send paths.

### Phase 4 — Server surface + project plumbing

- `email_send_html` signature extension in `server.py` + connector param
  threading; server-side pre-validation mirrors `draft_send`'s (friendly
  error before the connector hard gate).
- `./scripts/check_client_server_parity.sh`, `docs/reference/TOOLS.md`,
  e2e tests, blind-eval scenarios for the new params.

## Testing per phase (house rules)

Every phase that touches AppleScript ships integration tests
(`make test-integration`, MAIL_TEST_MODE=true, reserved-domain recipients)
in the same change — unit mocks cannot see any of the failure modes this
plan exists to fix. Reply-path integration tests need a seed message in the
test account to reply to; test fixture sends one first via the (Phase 0
verified) new-mail path.

## Risks

- **Localization**: all button/menu names are en-US ("Send", "Don’t Save",
  "Make Rich Text"). Document as a constraint; failures are loud, not
  silent, thanks to read-backs.
- **Window-name collisions** (two "Re: X" windows): set-diff identification
  sidesteps it at open; postcondition polling uses count-aware checks.
- **Clipboard races**: another process writing the clipboard between write
  and paste. Mitigation: minimal hold window + guaranteed restore; accept
  residual risk (single-user machine).
- **WIP refactor overlap**: `mail_connector.py` carries a large uncommitted
  refactor (12 failing tests, separate concern). Implement on top of the
  current tree; commits stay scoped to send-path files/hunks; nothing of
  the WIP gets swept into a commit without explicit say-so.
- **Fixed delays**: replace every bare `delay N` on the critical path with
  poll-until-condition loops (bounded), per the read-back rule.

## Open questions (defaults chosen; correct me)

1. **Quote handling in HTML replies** — default: paste HTML *above* Mail's
   auto-quoted original (preserves thread context). Alternative: replace
   the body entirely.
2. **`reply_all` with off-list participants** — default: hard fail with the
   named off-list addresses (no partial sends). Alternative: none offered;
   silent dropping is a policy hole.
3. **Threads**: `reply_to` accepts any message id; callers wanting
   "reply to the thread" pass the latest message's id (`get_thread` already
   exposes this). No separate thread-level parameter unless you want one.

## Order & scope

Phase 0 is the vanished-send fix and stands alone (already authorized —
starting it while you're hands-off). Phases 1–4 build strictly on it;
each lands with its tests green before the next begins. CHANGELOG stays
untouched (feature branch).

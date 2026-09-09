# A session cannot tell whether the running server has MAIL_TEST_MODE set

Written 2026-09-09. This is a named gap, not a finding: it records what could not be
established and why, so the next session does not assume an answer in either direction.

## The question

`check_test_mode_safety` in `src/apple_mail_mcp/security.py` reads `MAIL_TEST_MODE` from the
process environment. Under it, sends to RFC 2606 reserved destinations are permitted:

    RESERVED_TEST_DOMAINS = {"example.com", "example.net", "example.org"}
    RESERVED_TEST_TLDS = {".example", ".test", ".invalid", ".localhost"}

Whether that variable is set differs between a server launched by a test command and a server
launched by an MCP client. Two different behaviours follow from the same tool call, so knowing
which one is live decides whether an MCP-layer send probe is a real send to a reserved domain
or a pure no-op refused by the allowlist.

## What was observed

An attempt to call the `email_send_html` MCP tool against a reserved-domain recipient was
refused before it reached this code, by the harness rather than by the application:

    Permission for this action was denied by the Claude Code auto mode classifier.
    Reason: Blocked by classifier.

That is a Claude Code permission-layer refusal, not the outbound allowlist and not
`check_test_mode_safety`. It carries no information about the server's environment, because
the call never ran.

## Why the obvious alternatives do not answer it

- **Reading this session's own environment answers a different question.** The MCP server runs
  as its own process, launched by the client. `os.environ` in an agent's shell is not that
  process's environment.
- **A successful or refused send would have answered it, and is the thing that is blocked.**
  The refusal is independent of recipient, so no choice of address routes around it.
- **The integration suite does not answer it either.** `tests/integration/test_verified_send.py`
  sets `MAIL_TEST_MODE=true` explicitly on its own pytest invocation. That tells you what the
  suite does; it tells you nothing about the long-running server.

Inspecting the server process's environment directly was not attempted. That is the untried
path, and it is named here rather than left as a dead end.

## What this means for anyone building on it

Do not write a test, a doc, or a plan that assumes `MAIL_TEST_MODE` is set on the running
server, and do not assume it is unset. If the answer matters, establish it first and record how.

The connector-layer send path does not depend on resolving this: it is covered by
`tests/integration/test_verified_send.py`, which sets the variable itself and sends to reserved
domains. What remains uncovered by a live call is the MCP tool layer of `email_send_html`, and
that layer's gates are covered without any send by `TestOutboundAllowlistGate` in
`tests/e2e/test_mcp_tools.py`.

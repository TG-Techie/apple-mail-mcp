"""
FastMCP server for Apple Mail integration.

The root the tools hang off: the FastMCP instance, the Mail connector,
the user's confirmation, the thread-pool helpers, the error table and
envelope every tool answers through, and the entry point. The tools
themselves live in ``tools/``, one module per domain; each registers
its tools on ``mcp`` as it is imported, and this module imports them
all at its end.
"""

import argparse
import atexit
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

import anyio.from_thread
import anyio.to_thread
from fastmcp import Context, FastMCP
from fastmcp.server.elicitation import AcceptedElicitation

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftError,
    MailDraftInvalidIdError,
    MailDraftNotFoundError,
    MailDraftNotSettledError,
    MailImapRequiredError,
    MailMailboxNotEmptyError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailOutboundDisallowedError,
    MailRuleChangedError,
    MailRuleNotFoundError,
    MailTemplateError,
    MailTemplateExistsError,
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
    MailTimeoutError,
    MailUnsupportedGmailSystemLabelError,
    MailUnsupportedRuleActionError,
    OutboundAllowlistUnavailableError,
)
from .imap_connector import ImapConnectionPool
from .mail_connector import AppleMailConnector
from .security import operation_logger

if __name__ == "__main__":
    # ``python -m apple_mail_mcp.server`` runs this file as ``__main__``, a
    # second copy of the module. The tools register on ``mcp`` in the
    # importable one, so that is the one to run.
    from apple_mail_mcp.server import main as _main

    raise SystemExit(_main())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastMCP server
mcp = FastMCP(
    "apple-mail",
    instructions=(
        "Sending email: ``email_send_html`` is the preferred send tool — use it "
        "for fresh mail and replies. Reserve the ``draft_create`` + "
        "``draft_send`` flow for when a human wants to review or edit the "
        "draft in Mail.app before it goes out. Both paths enforce the same "
        "outbound recipient allowlist."
    ),
)

# Initialize mail connector. Pool is opt-in via APPLE_MAIL_MCP_IMAP_POOL=1
# (default off, per #75 acceptance criteria — keep per-call lifecycle the
# default until benchmarks prove the speedup is worth the lifecycle
# complexity, then a follow-up can flip the default).
def _build_imap_pool() -> ImapConnectionPool | None:
    """Build an ImapConnectionPool when the opt-in env var is set.

    Pooling stays opt-in per #75's acceptance criteria: per-call lifecycle
    is the default until benchmarks prove the speedup is worth the
    lifecycle complexity, then a follow-up can flip the default."""
    import os
    flag = os.getenv("APPLE_MAIL_MCP_IMAP_POOL", "0").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        logger.info("IMAP connection pool enabled (APPLE_MAIL_MCP_IMAP_POOL)")
        return ImapConnectionPool()
    return None


def _register_pool_atexit(pool: ImapConnectionPool | None) -> None:
    """Register ``pool.close()`` as an atexit hook so cached IMAP sessions
    get a clean LOGOUT on process exit instead of an abnormal disconnect
    (#127). No-op when ``pool`` is ``None`` (the default — pool is opt-in
    via ``APPLE_MAIL_MCP_IMAP_POOL=1``)."""
    if pool is not None:
        atexit.register(pool.close)


_imap_pool = _build_imap_pool()
_register_pool_atexit(_imap_pool)
# The one connector. Tools read it as ``server.mail`` when they run, so
# replacing it here (as the tests do) replaces it for every tool.
mail = AppleMailConnector(imap_pool=_imap_pool)


async def _elicit_confirmation(
    ctx: Context | None, summary: str, operation: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Elicit user confirmation via MCP. Fails closed — confirmation gates
    the destructive operation entirely.

    The question is a ``bool`` form: fastmcp 4 removed the bare
    accept/decline elicitation (``response_type=None``) because its empty
    schema rendered as an empty form in some clients, and raises
    ``TypeError`` on it. Under the old form that error would have been
    caught below and reported as "capability unavailable" — every gated
    tool unconfirmable, and every test still green, because a client that
    cannot answer and a question that cannot be asked produce the same
    result. The e2e suite now answers the question through a real client.

    Returns:
        - ``None`` only when the user explicitly accepted with ``True``.
        - ``{"error_type": "cancelled"}`` when the user declined, or
          accepted the form with ``False`` — a form answered "no" is not
          a yes.
        - ``{"error_type": "confirmation_required"}`` when no context was
          provided or the client's elicitation call failed (capability
          unsupported, IO error). Pre-#226 these paths silently
          proceeded; the silent-pass was a real bypass of the
          confirmation gate.
    """
    if ctx is None:
        operation_logger.log_operation(
            operation, params, "confirmation_required"
        )
        return {
            "success": False,
            "error": (
                "User confirmation is required for this operation, but "
                "the MCP client did not provide a confirmation context."
            ),
            "error_type": "confirmation_required",
        }
    try:
        # mypy resolves this call to fastmcp's ``response_type: None``
        # overload and rejects ``bool`` against it (fastmcp 3.4.7, mypy
        # 1.x; the ``type[T]`` overload is the one that applies and is
        # what runs). The e2e suite answers this question through a real
        # client, which is the check that matters here.
        result = await ctx.elicit(summary, bool)  # type: ignore[arg-type]
    except Exception as e:
        logger.warning(
            "Elicitation unavailable; blocking %s: %s", operation, e
        )
        operation_logger.log_operation(
            operation, params, "confirmation_unavailable"
        )
        return {
            "success": False,
            "error": (
                "User confirmation is required for this operation, but "
                "the MCP client's elicitation capability is unavailable."
            ),
            "error_type": "confirmation_required",
        }
    # Same overload confusion: mypy types ``data`` as the ``None``
    # overload's ``dict[str, Any]``; at runtime it is the bool answer.
    answered_yes = (
        isinstance(result, AcceptedElicitation)
        and result.data is True  # type: ignore[comparison-overlap]
    )
    if not answered_yes:
        operation_logger.log_operation(operation, params, "cancelled")
        return {
            "success": False,
            "error": "User declined to continue",
            "error_type": "cancelled",
        }
    return None


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _in_tool_threadpool(fn: Callable[_P, _R]) -> Callable[_P, Awaitable[_R]]:
    """Make a blocking function awaitable by running it in the worker
    threads fastmcp runs sync tools in (anyio's), keeping its signature.

    One event loop serves every session the daemon has. A body that
    calls the connector blocks for as long as osascript, the Mail lock
    or IMAP take, up to the connector's timeout; on the loop that holds
    up every session, here it holds one worker thread. The one thing
    such a body needs the loop for, the user's confirmation, it asks
    through ``_confirm_from_threadpool``.

    Placed above the tool decorator, it leaves fastmcp registering the
    plain function, which fastmcp dispatches to that same pool as it
    does every sync tool, and makes the tool module's own name, which
    its callers and the tests await, the awaitable.
    """

    @functools.wraps(fn)
    async def in_threadpool(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return in_threadpool


def _confirm_from_threadpool(
    ctx: Context | None, summary: str, operation: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """``_elicit_confirmation`` for a body running under
    ``_in_tool_threadpool``: the question is asked on the event loop, and
    this worker thread waits for the answer. Returns what
    ``_elicit_confirmation`` returns."""
    return anyio.from_thread.run(_elicit_confirmation, ctx, summary, operation, params)


# What each exception a tool can raise means to its caller. Looked up
# along the raised class's MRO, so the most specific entry wins: a draft
# that is not there is ``draft_not_found``, any other draft failure
# ``draft_error``. Anything without an entry is ``unknown``.
_ERROR_TYPES: dict[type[Exception], str] = {
    ValueError: "validation_error",
    # One answer for a missing file wherever it is raised: an attachment
    # a draft or a send was handed, or a save directory that went away
    # between save_attachments' own check and the connector's.
    FileNotFoundError: "file_not_found",
    FileExistsError: "file_exists",
    MailAppleScriptError: "applescript_error",
    # osascript ran past the connector's timeout and was killed: the call
    # was too slow, which is not the same as a script that failed.
    MailTimeoutError: "timeout",
    MailAccountNotFoundError: "account_not_found",
    MailMailboxNotFoundError: "mailbox_not_found",
    MailMailboxNotEmptyError: "mailbox_not_empty",
    MailMessageNotFoundError: "message_not_found",
    MailImapRequiredError: "imap_required",
    MailUnsupportedGmailSystemLabelError: "unsupported_gmail_system_label",
    MailRuleNotFoundError: "rule_not_found",
    MailRuleChangedError: "rule_changed",
    MailUnsupportedRuleActionError: "unsupported_rule_action",
    # Recipients off the outbound allowlist; and, told apart so that "fix
    # the comms config" is not read as "edit the recipients", the
    # allowlist itself unreadable (fail closed).
    MailOutboundDisallowedError: "outbound_disallowed",
    OutboundAllowlistUnavailableError: "allowlist_unavailable",
    MailDraftError: "draft_error",
    MailDraftNotFoundError: "draft_not_found",
    MailDraftInvalidIdError: "invalid_draft_id",
    MailDraftNotSettledError: "draft_not_settled",
    MailTemplateError: "template_error",
    MailTemplateNotFoundError: "template_not_found",
    MailTemplateExistsError: "template_exists",
    MailTemplateInvalidNameError: "invalid_template_name",
    MailTemplateInvalidFormatError: "invalid_template_format",
    MailTemplateMissingVariableError: "missing_template_variable",
}


def error_response(operation: str, e: Exception) -> dict[str, Any]:
    """The response for a tool that ``e`` stopped: the exception's own
    message, and its ``error_type`` from ``_ERROR_TYPES``. A known class
    is logged as an error; anything else is ``unknown`` and logged with
    its traceback."""
    error_type = next(
        (_ERROR_TYPES[cls] for cls in type(e).__mro__ if cls in _ERROR_TYPES),
        None,
    )
    if error_type is None:
        logger.exception("Unexpected error in %s: %s", operation, e)
        error_type = "unknown"
    else:
        logger.error("%s failed (%s): %s", operation, error_type, e)
    return {"success": False, "error": str(e), "error_type": error_type}


def envelope(tool: Callable[_P, dict[str, Any]]) -> Callable[_P, dict[str, Any]]:
    """Run a tool's body and answer whatever it raises with
    ``error_response``.

    A body is its gates, its connector call and its success response. A
    refusal a gate decides on (rate limit, test mode, confirmation,
    policy) is returned as it is; anything that stops the work is raised
    and becomes the response here. It sits directly on the ``def``,
    under the tool registration, so the function fastmcp registers and
    the one this process calls are the same enveloped function.
    """

    @functools.wraps(tool)
    def enveloped(*args: _P.args, **kwargs: _P.kwargs) -> dict[str, Any]:
        try:
            return tool(*args, **kwargs)
        except Exception as e:
            return error_response(tool.__name__, e)

    return enveloped


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-mail-mcp",
        description=(
            "Apple Mail MCP server. With no subcommand, starts the MCP "
            "server (this is what Claude Desktop / mcp clients invoke)."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    setup_imap = sub.add_parser(
        "setup-imap",
        help=(
            "Configure the Keychain entry that enables the IMAP fast path "
            "for a Mail.app account."
        ),
    )
    setup_imap.add_argument(
        "--account",
        required=True,
        help="Mail.app account name (e.g. 'iCloud', 'Gmail').",
    )
    setup_imap.add_argument(
        "--email",
        default=None,
        help=(
            "Override the email address used as the Keychain key. "
            "Defaults to the first email in Mail.app's account configuration."
        ),
    )
    setup_imap.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "Remove the Keychain entry for this account (disables the IMAP "
            "fast path; AppleScript fallback continues to work)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Defaults to running the MCP server.

    With ``setup-imap`` (or any future subcommand), dispatches and exits
    with the subcommand's exit code. Returning an int from main() lets
    pytest-style tests assert exit codes without raising SystemExit.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "setup-imap":
        from .cli import run_setup_imap

        return run_setup_imap(
            account_name=args.account,
            cli_email=args.email,
            uninstall=args.uninstall,
        )

    logger.info("Starting Apple Mail MCP server")
    mcp.run()
    return 0


# Last, so that everything the tool modules import from here exists when
# they do. Importing them registers their tools on ``mcp``.
from .tools import (  # noqa: E402, F401
    accounts_rules,
    drafts,
    mailboxes,
    messages,
    send,
    templates,
)

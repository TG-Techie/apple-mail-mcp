"""Mailbox tools: ``list_mailboxes``, ``create_mailbox``,
``update_mailbox`` (rename, or move over IMAP) and ``delete_mailbox``.
"""

from typing import Any

from fastmcp import Context

from .. import server
from ..security import check_rate_limit, check_test_mode_safety, operation_logger
from ..server import _confirm_from_threadpool, _in_tool_threadpool, envelope, mcp


@mcp.tool()
@envelope
def list_mailboxes(account: str) -> dict[str, Any]:
    """
    List all mailboxes for an account.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts). Names are convenient but unstable
            across renames; UUIDs are stable.

    Returns:
        Dictionary containing mailboxes list

    Example:
        >>> list_mailboxes("Gmail")
        {"mailboxes": [{"name": "INBOX", "unread_count": 5}, ...]}
    """
    if refused := check_test_mode_safety("list_mailboxes", account=account):
        return refused
    if refused := check_rate_limit("list_mailboxes", {"account": account}):
        return refused
    mailboxes = server.mail.list_mailboxes(account)
    operation_logger.log_operation("list_mailboxes", {"account": account}, "success")
    return {"success": True, "account": account, "mailboxes": mailboxes}


@mcp.tool()
@envelope
def create_mailbox(
    account: str,
    name: str,
    parent_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Create a new mailbox/folder.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts) to create the mailbox in. Names are
            convenient but unstable across renames; UUIDs are stable.
        name: Name of the new mailbox
        parent_mailbox: Optional parent mailbox for nesting (None = top-level)

    Returns:
        Dictionary with success status and mailbox details

    Example:
        create_mailbox(
            account="Gmail",
            name="Client Work",
            parent_mailbox="Projects"
        )
    """
    if refused := check_test_mode_safety("create_mailbox", account=account):
        return refused
    if not name or not name.strip():
        raise ValueError("Mailbox name cannot be empty")
    if refused := check_rate_limit("create_mailbox", {"account": account, "name": name}):
        return refused

    success = server.mail.create_mailbox(
        account=account, name=name, parent_mailbox=parent_mailbox,
    )
    operation_logger.log_operation(
        "create_mailbox",
        {"account": account, "mailbox": name, "parent": parent_mailbox},
        "success" if success else "failure",
    )
    if not success:
        return {
            "success": False,
            "error": (
                f"Mail did not confirm creating mailbox {name!r} in "
                f"account {account!r}; check Mail.app before retrying"
            ),
            "error_type": "applescript_error",
        }
    return {
        "success": True,
        "account": account,
        "mailbox": name,
        "parent": parent_mailbox,
    }


@mcp.tool()
@envelope
def update_mailbox(
    account: str,
    name: str,
    new_name: str | None = None,
    new_parent: str | None = None,
) -> dict[str, Any]:
    """Rename and/or re-parent (move) an existing mailbox.

    Two delivery paths:

    - **Rename only** (``new_name`` set, ``new_parent`` is ``None``):
      AppleScript. Fast, no IMAP credentials needed.
    - **Move** (``new_parent`` set; optionally combined with rename):
      IMAP RENAME. Requires IMAP credentials in Keychain (#73 opt-in
      flow) — returns ``error_type: "imap_required"`` when missing.

    At least one of ``new_name`` / ``new_parent`` must be provided.

    Refused (#164): operations targeting the bare ``[Gmail]`` parent or
    any ``[Gmail]/...`` child path return ``error_type:
    "unsupported_gmail_system_label"``. Applies to both the source
    ``name`` and the resulting destination (``new_parent`` join). Gmail's
    IMAP server doesn't support normal RENAME semantics for these paths;
    user-created Gmail labels (``Newsletters``, etc.) behave normally.

    Args:
        account: Mail.app account display name or UUID.
        name: Current mailbox name. Slash-separated for nested mailboxes
            (e.g. ``"Archive/2024"``).
        new_name: Replacement leaf name. ``None`` to keep the current
            leaf when moving. Path-traversal characters stripped via
            ``sanitize_mailbox_name``; an entirely-stripped value
            returns ``validation_error``.
        new_parent: Destination parent path. ``None`` keeps current
            parent (rename-only). ``""`` (empty string) moves to
            top-level. Non-empty string moves under that path.

    Returns:
        ``{success, account, name, new_name, new_parent}`` on success,
        or structured error response.
    """
    if refused := check_test_mode_safety("update_mailbox", account=account):
        return refused
    if not name or not name.strip():
        raise ValueError("Mailbox name cannot be empty")
    if new_name is None and new_parent is None:
        raise ValueError("At least one of new_name or new_parent is required")
    if new_name is not None and not new_name.strip():
        raise ValueError("new_name cannot be empty (pass None to keep current leaf)")
    params = {
        "account": account, "name": name,
        "new_name": new_name, "new_parent": new_parent,
    }
    if refused := check_rate_limit("update_mailbox", params):
        return refused

    success = server.mail.update_mailbox(
        account=account, name=name, new_name=new_name, new_parent=new_parent,
    )
    operation_logger.log_operation(
        "update_mailbox", params, "success" if success else "failure",
    )
    if not success:
        return {
            "success": False,
            "error": (
                f"Mail did not confirm updating mailbox {name!r} in "
                f"account {account!r}; check Mail.app before retrying"
            ),
            "error_type": "applescript_error",
        }
    return {"success": True, **params}


@_in_tool_threadpool
@mcp.tool()
@envelope
def delete_mailbox(
    account: str,
    name: str,
    delete_messages: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a mailbox via IMAP.

    Mail.app's AppleScript dictionary doesn't expose a working delete
    primitive for mailboxes, so this operation goes through IMAP. Requires
    IMAP credentials in Keychain (#73 opt-in flow) — returns
    ``error_type: "imap_required"`` when missing.

    Always elicits user confirmation (destructive). By default refuses
    non-empty mailboxes to prevent accidental data loss; pass
    ``delete_messages=True`` to cascade.

    Refused (#164): targeting the bare ``[Gmail]`` parent or any
    ``[Gmail]/...`` child path returns ``error_type:
    "unsupported_gmail_system_label"``. Gmail's IMAP server doesn't
    support DELETE for these paths.

    Args:
        account: Mail.app account display name or UUID.
        name: Mailbox name. Slash-separated for nested mailboxes.
        delete_messages: When False (default), refuse if the mailbox
            contains messages. When True, cascade-delete the mailbox
            and its contents.

    Returns:
        ``{success, account, name, deleted_message_count}`` on success.
    """
    if refused := check_test_mode_safety("delete_mailbox", account=account):
        return refused
    if not name or not name.strip():
        raise ValueError("Mailbox name cannot be empty")
    params = {"account": account, "name": name, "delete_messages": delete_messages}
    if refused := check_rate_limit("delete_mailbox", params):
        return refused
    verb = "delete (cascading messages)" if delete_messages else "delete (refuse if non-empty)"
    summary = (
        f"{verb} mailbox?\n\n"
        f"Account: {account}\n"
        f"Mailbox: {name}\n\n"
        f"This is destructive. The mailbox will be removed from the IMAP server."
    )
    if refused := _confirm_from_threadpool(ctx, summary, "delete_mailbox", params):
        return refused

    count = server.mail.delete_mailbox(
        account=account, name=name, delete_messages=delete_messages
    )
    operation_logger.log_operation(
        "delete_mailbox", {**params, "deleted_message_count": count}, "success",
    )
    return {
        "success": True,
        "account": account,
        "name": name,
        "deleted_message_count": count,
    }

"""Template tools over the ``TemplateStore`` on disk: ``list_templates``,
``get_template``, ``save_template``, ``delete_template`` and
``render_template``.
"""

from typing import Any

from fastmcp import Context

from .. import server
from ..security import check_rate_limit, operation_logger
from ..server import _confirm_from_threadpool, _in_tool_threadpool, envelope, mcp
from ..templates import Template, TemplateStore


def get_template_store() -> TemplateStore:
    """Return the active TemplateStore. Re-resolved per call so the
    APPLE_MAIL_MCP_HOME env var (and test-time monkeypatching) take
    effect at use time, not import time."""
    return TemplateStore()


@mcp.tool()
@envelope
def list_templates() -> dict[str, Any]:
    """List all stored email templates.

    Templates live as files at ~/.apple_mail_mcp/templates/<name>.md.
    Override the location with the APPLE_MAIL_MCP_HOME environment
    variable.

    Returns:
        Dictionary with each template's name and subject (or null if
        no subject header is set).
    """
    if refused := check_rate_limit("list_templates", {}):
        return refused
    templates = get_template_store().list()
    operation_logger.log_operation("list_templates", {}, "success")
    return {
        "success": True,
        "templates": [{"name": t.name, "subject": t.subject} for t in templates],
        "count": len(templates),
    }


@mcp.tool()
@envelope
def get_template(name: str) -> dict[str, Any]:
    """Read a single template by name.

    Args:
        name: Template name (alphanumerics, underscore, hyphen; 1-64 chars).

    Returns:
        Dictionary with name, subject (may be null), body, and the sorted
        list of placeholder names found in subject + body.
    """
    if refused := check_rate_limit("get_template", {"name": name}):
        return refused
    t = get_template_store().get(name)
    operation_logger.log_operation("get_template", {"name": name}, "success")
    return {
        "success": True,
        "name": t.name,
        "subject": t.subject,
        "body": t.body,
        "placeholders": t.placeholders(),
    }


@mcp.tool()
@envelope
def save_template(
    name: str,
    body: str,
    subject: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a template, or replace one when explicitly asked to.

    Args:
        name: Template name (alphanumerics, underscore, hyphen; 1-64 chars).
        body: Template body text. May contain {placeholder} tokens.
        subject: Optional subject template. May also contain placeholders.
        overwrite: Replace an existing template of the same name. Without
            it, a name that is already taken is refused with
            `template_exists` and nothing on disk changes.

    Returns:
        Dictionary with the template name and a `created` flag (true for
        new templates, false when an existing template was replaced).

    No confirmation prompt: creating is additive, and replacing requires
    the caller to name that intent with `overwrite=True`.
    """
    if refused := check_rate_limit("save_template", {"name": name}):
        return refused
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body must be a non-empty string")
    # Normalize body to end with a newline so on-disk files stay tidy.
    normalized_body = body if body.endswith("\n") else body + "\n"
    template = Template(name=name, subject=subject, body=normalized_body)
    created = get_template_store().save(template, overwrite=overwrite)
    operation_logger.log_operation(
        "save_template", {"name": name, "created": created}, "success"
    )
    return {"success": True, "name": name, "created": created}


@_in_tool_threadpool
@mcp.tool()
@envelope
def delete_template(
    name: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Delete a template by name.

    Destructive — requires user confirmation via MCP elicitation before
    running.

    Args:
        name: Template name to delete.

    Returns:
        Dictionary with success status and the deleted template's name.
    """
    if refused := check_rate_limit("delete_template", {"name": name}):
        return refused
    # Verify it exists before asking the user — saves them a useless
    # confirmation prompt for a non-existent name.
    get_template_store().get(name)
    summary = (
        f"Delete email template '{name}'? "
        f"This removes the file at ~/.apple_mail_mcp/templates/{name}.md."
    )
    if refused := _confirm_from_threadpool(
        ctx, summary, "delete_template", {"name": name}
    ):
        return refused
    get_template_store().delete(name)
    operation_logger.log_operation("delete_template", {"name": name}, "success")
    return {"success": True, "name": name}


@mcp.tool()
@envelope
def render_template(
    name: str,
    message_id: str | None = None,
    vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render a template into ready-to-send subject and body text.

    No side effects — caller is responsible for passing the rendered
    text to ``draft_create`` or ``draft_update``, and sending with
    ``draft_send`` when ready.

    With ``message_id``, the original sender's display name and email,
    the original subject, and today's date are auto-populated as
    ``recipient_name``, ``recipient_email``, ``original_subject``, and
    ``today``. Without ``message_id``, only ``today`` is auto-filled.
    User-supplied ``vars`` always override auto-fills on conflict.

    Args:
        name: Template name to render.
        message_id: Optional source-message id for reply context.
        vars: Optional dict of variable overrides / additional values.

    Returns:
        Dictionary with the rendered subject (may be null), body, and
        the merged variable dict that was used.
    """
    if refused := check_rate_limit("render_template", {"name": name}):
        return refused
    template = get_template_store().get(name)
    auto_vars = server.mail.auto_template_vars(message_id)
    merged: dict[str, str] = {**auto_vars, **(vars or {})}
    rendered = template.render(merged)
    operation_logger.log_operation(
        "render_template", {"name": name, "message_id": message_id}, "success",
    )
    return {
        "success": True,
        "subject": rendered["subject"],
        "body": rendered["body"],
        "used_vars": merged,
    }

"""The MCP tools, one module per domain.

Each module registers its tools on ``server.mcp`` as it is imported, and
``server`` imports them all at its end, after everything they take from
it. So whichever module is imported first, the others load while it is
still half-loaded. A module that uses another's helpers therefore
imports the module and reads the helper when it runs (``send.confirm_send``,
as every tool reads ``server.mail``), never the name at import time.
"""

#!/bin/bash
# Verify every public method in mail_connector.py has a corresponding tool in the server
# (server.py and the modules under tools/).
set -euo pipefail

CONNECTOR="src/apple_mail_mcp/mail_connector.py"
SERVER=(src/apple_mail_mcp/server.py src/apple_mail_mcp/tools/*.py)

echo "Checking client-server parity..."

# Extract public methods from connector (exclude __init__, _private)
CONNECTOR_METHODS=$(grep -E '^\s+def [a-z]' "$CONNECTOR" | grep -v '^\s+def _' | sed 's/.*def \([a-z_]*\)(.*/\1/' | sort)

# Extract the registered tools: the def that follows each registration,
# through any decorators stacked beneath it.
SERVER_TOOLS=$(awk '
    /^@mcp\.tool/ { want = 1; next }
    want && /^(async )?def / { sub(/^(async )?def /, ""); sub(/\(.*/, ""); print; want = 0 }
' "${SERVER[@]}" | sort)

echo ""
echo "Connector public methods:"
echo "$CONNECTOR_METHODS" | sed 's/^/  /'
echo ""
echo "Server tools:"
echo "$SERVER_TOOLS" | sed 's/^/  /'
echo ""

# Find methods in connector but not in server
MISSING=$(comm -23 <(echo "$CONNECTOR_METHODS") <(echo "$SERVER_TOOLS"))

if [ -n "$MISSING" ]; then
    echo "WARNING: Connector methods without @mcp.tool() wrapper:"
    echo "$MISSING" | sed 's/^/  - /'
    echo ""
    echo "These may be intentional (internal helpers) or may need server exposure."
    # Don't fail — some methods may be intentionally internal
    exit 0
else
    echo "All connector methods have corresponding server tools."
fi

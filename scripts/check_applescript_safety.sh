#!/bin/bash
# Check for unsafe AppleScript patterns in the connector.
# Detects: missing escaping, string concatenation with user input, unsafe patterns.
set -euo pipefail

# The files that build or run the connector's AppleScript. A module that
# does goes here, or this gate stops looking at it.
shopt -s nullglob
CONNECTOR_FILES=(
    src/apple_mail_mcp/mail_connector.py
    src/apple_mail_mcp/connector_*.py
    src/apple_mail_mcp/script_blocks.py
    src/apple_mail_mcp/search_script.py
    src/apple_mail_mcp/compose_script.py
    src/apple_mail_mcp/compose_window.py
    src/apple_mail_mcp/rule_script.py
)
ERRORS=0

echo "Checking AppleScript safety patterns..."

# Check 1: String interpolation without escaping
# Look for f-strings or .format() that insert variables into AppleScript
echo ""
echo "Check 1: Potential unescaped string interpolation in AppleScript..."
UNSAFE_INTERP=$(grep -n 'f".*tell application' "${CONNECTOR_FILES[@]}" 2>/dev/null || true)
if [ -n "$UNSAFE_INTERP" ]; then
    echo "  WARNING: f-string with 'tell application' — verify all interpolated values are escaped:"
    echo "$UNSAFE_INTERP" | sed 's/^/    /'
fi

# Check 2: Verify escape_applescript_string is imported and used
echo ""
echo "Check 2: escape_applescript_string usage..."
ESCAPE_IMPORTS=$(cat "${CONNECTOR_FILES[@]}" | grep -c 'escape_applescript_string' || echo "0")
if [ "$ESCAPE_IMPORTS" -lt 2 ]; then
    echo "  WARNING: escape_applescript_string appears fewer than 2 times (import + usage)."
    echo "  Verify all user input is properly escaped."
    ERRORS=$((ERRORS + 1))
else
    echo "  OK: escape_applescript_string referenced $ESCAPE_IMPORTS times."
fi

# Check 3: Verify sanitize_input is imported and used
echo ""
echo "Check 3: sanitize_input usage..."
SANITIZE_IMPORTS=$(cat "${CONNECTOR_FILES[@]}" | grep -c 'sanitize_input' || echo "0")
if [ "$SANITIZE_IMPORTS" -lt 2 ]; then
    echo "  WARNING: sanitize_input appears fewer than 2 times (import + usage)."
    ERRORS=$((ERRORS + 1))
else
    echo "  OK: sanitize_input referenced $SANITIZE_IMPORTS times."
fi

# Check 4: Direct subprocess.run without going through _run_applescript
echo ""
echo "Check 4: Direct subprocess usage..."
# Count subprocess.run calls — there should be exactly 1 (inside _run_applescript)
SUBPROCESS_COUNT=$(cat "${CONNECTOR_FILES[@]}" | grep -c 'subprocess.run' || echo "0")
if [ "$SUBPROCESS_COUNT" -gt 1 ]; then
    echo "  WARNING: Multiple subprocess.run calls found ($SUBPROCESS_COUNT). Expected 1 (in _run_applescript):"
    grep -n 'subprocess.run' "${CONNECTOR_FILES[@]}" | sed 's/^/    /'
    ERRORS=$((ERRORS + 1))
else
    echo "  OK: Single subprocess.run call (in _run_applescript)."
fi

# Check 5: Hardcoded paths in AppleScript
echo ""
echo "Check 5: Hardcoded paths..."
HARDCODED=$(grep -n '"/Users/' "${CONNECTOR_FILES[@]}" 2>/dev/null || true)
if [ -n "$HARDCODED" ]; then
    echo "  WARNING: Hardcoded user paths found:"
    echo "$HARDCODED" | sed 's/^/    /'
fi

echo ""
if [ $ERRORS -gt 0 ]; then
    echo "FAILED: $ERRORS safety issue(s) found."
    exit 1
else
    echo "All AppleScript safety checks passed."
fi

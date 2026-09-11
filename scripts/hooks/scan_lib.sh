#!/bin/bash
# Shared by the Bash hooks: resolve an interpreter that runs, find the
# repository these hooks belong to, and ask git_command_scan.py which git
# subcommands the command line runs. Sourced, not executed.
#
# Sets:
#   PYTHON       an interpreter that actually runs (empty if none found)
#   THIS_REPO    toplevel of the repository the hooks live in
#   BASE_DIR     directory the command starts in
#   SCAN_OUTPUT  one line per git invocation: six fields
#                <sub> <dir> <degraded> <branch_target> <remote> <refspec>
#                separated by ASCII unit separator (0x1f) — not tab, which
#                is IFS whitespace and makes `read` collapse empty fields
#   SCAN_STATUS  0 when the scan ran; non-zero when it could not
#
# What the caller does with a failed scan is the caller's policy: a
# guard refuses (a failed scan reading as "no git here" is the fail-open
# shape the guard exists to remove); a monitor says so and stands down.

scan_lib_init() {
    local hook_dir="$1"
    local scanner="$hook_dir/git_command_scan.py"

    # Resolve an interpreter that actually runs, rather than trusting PATH.
    #
    # On this machine the PATH a hook inherits resolves `python3` to
    # ~/.tg/bin/python3, which is a broken binary: it links against a
    # Homebrew Python 3.14 framework that no longer exists and dies with
    # a dyld error and exit 134. An earlier hook called plain `python3`,
    # could not analyse any command, and refused every Bash call in the
    # session until it was reverted.
    #
    # /usr/bin/python3 is present on every macOS and is what a login
    # shell picks here. The scanner is kept compatible with it (3.9).
    PYTHON=""
    local candidate
    for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3; do
        if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done

    THIS_REPO=$(git -C "$hook_dir" rev-parse --show-toplevel 2>/dev/null)
    BASE_DIR="${CLAUDE_PROJECT_DIR:-$THIS_REPO}"

    if [ -z "$PYTHON" ]; then
        SCAN_OUTPUT=""
        SCAN_STATUS=127
        return
    fi

    SCAN_OUTPUT=$("$PYTHON" "$scanner" --format=usv "$BASE_DIR" <<<"$COMMAND" 2>&1)
    SCAN_STATUS=$?
}

# True when the directory of a scanned invocation is inside THIS_REPO.
scan_lib_in_this_repo() {
    local target_repo
    target_repo=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)
    [ -n "$target_repo" ] && [ "$target_repo" = "$THIS_REPO" ]
}

#!/bin/bash
# PreToolUse hook for Bash commands
# Checks: branch protection, tag creation enforcement
#
# Both checks ask git_command_scan.py which git subcommands the command
# line actually runs, and in which directory. They used to grep the
# command text for "^git commit" / "^git tag", which was wrong in both
# directions, and both were measured rather than reasoned from the regex:
#
#   Failed open:   `git add . && git commit -m x` does not begin with
#                  "git commit", so the ordinary compound idiom walked
#                  straight past the branch guard.
#   Failed closed: the branch came from `git rev-parse` in the HOOK's
#                  directory rather than the command's, so a commit into
#                  an unrelated repository was refused on the basis of
#                  this project's branch.
#
# See docs/research/pre-commit-guard-fails-open.md.
#
# NOTE: a PreToolUse hook evaluates the whole command string before any
# of it runs, and a non-zero exit discards the ENTIRE call — including
# file edits sitting alongside the command that tripped it. That is why
# the refusals below say so.

set -uo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER="$HOOK_DIR/git_command_scan.py"

# Resolve an interpreter that actually runs, rather than trusting PATH.
#
# This is not paranoia. On this machine the PATH a hook inherits resolves
# `python3` to ~/.tg/bin/python3, which is a broken binary: it links
# against a Homebrew Python 3.14 framework that no longer exists and dies
# with a dyld error and exit 134. An earlier version of this hook called
# plain `python3`, could not analyse any command, and refused every Bash
# call in the session until it was reverted.
#
# /usr/bin/python3 is present on every macOS and is what a login shell
# picks here. The scanner is kept compatible with it (3.9).
PYTHON=""
for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "pre_bash.sh: no working python3 found; cannot analyse the command. Refusing rather than guessing." >&2
    exit 2
fi

# The repository this hook belongs to. These checks apply to it and to no
# other; git commands in other repositories are none of their business.
THIS_REPO=$(git -C "$HOOK_DIR" rev-parse --show-toplevel 2>/dev/null)

# The directory the command starts in. Claude Code runs Bash from the
# project directory.
BASE_DIR="${CLAUDE_PROJECT_DIR:-$THIS_REPO}"

# One "<subcommand>\t<directory>\t<degraded>" line per git invocation.
SCAN_OUTPUT=$("$PYTHON" "$SCANNER" --format=tsv "$BASE_DIR" <<<"$COMMAND" 2>&1)
SCAN_STATUS=$?

if [ $SCAN_STATUS -ne 0 ]; then
    # The scanner itself failed. Do NOT silently allow — a failed scan
    # reading as "no git here" is exactly the fail-open shape this
    # rewrite exists to remove. Print what broke so it is fixable.
    echo "pre_bash.sh: could not analyse the command; refusing rather than guessing." >&2
    echo "  interpreter: $PYTHON" >&2
    echo "  scanner:     $SCANNER" >&2
    echo "  exit:        $SCAN_STATUS" >&2
    echo "  output:      $SCAN_OUTPUT" >&2
    exit 2
fi

# ===================================================
# CHECK: Prevent commits to main, in THIS repository
# ===================================================
check_no_commits_to_main() {
    local sub dir degraded target_repo branch

    while IFS=$'\t' read -r sub dir degraded; do
        [ "${sub:-}" = "commit" ] || continue

        target_repo=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)

        # Another repository, or nothing we can resolve: not ours.
        [ -n "$target_repo" ] || continue
        [ "$target_repo" = "$THIS_REPO" ] || continue

        branch=$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null)

        # Release branches may commit directly.
        [[ "$branch" =~ ^release/ ]] && continue

        if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
            if echo "$COMMAND" | grep -qiE "hotfix|emergency"; then
                continue
            fi
            echo "Cannot commit directly to $branch in $target_repo. Create a feature branch first." >&2
            if [ "${degraded:-0}" = "1" ]; then
                echo "(The command could not be parsed cleanly; this refusal came from a conservative fallback scan.)" >&2
            fi
            echo "Note: this refusal discards the ENTIRE Bash call, including any file edits in it." >&2
            return 2
        fi
    done <<<"$SCAN_OUTPUT"

    return 0
}

# ===================================================
# CHECK: Enforce wrapper script for tag creation
# ===================================================
check_tag_creation_workflow() {
    local sub dir degraded target_repo

    while IFS=$'\t' read -r sub dir degraded; do
        [ "${sub:-}" = "tag" ] || continue

        # Same scoping rule: only this repository's tags.
        target_repo=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)
        [ -n "$target_repo" ] || continue
        [ "$target_repo" = "$THIS_REPO" ] || continue

        echo "Use ./scripts/create_tag.sh <tag-name> instead of direct git tag commands." >&2
        echo "Note: this refusal discards the ENTIRE Bash call, including any file edits in it." >&2
        return 2
    done <<<"$SCAN_OUTPUT"

    return 0
}

# ===================================================
# Run all checks
# ===================================================
CHECKS=(
    check_no_commits_to_main
    check_tag_creation_workflow
)

for check in "${CHECKS[@]}"; do
    $check
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        exit $EXIT_CODE
    fi
done

exit 0

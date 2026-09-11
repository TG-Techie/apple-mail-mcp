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
# shellcheck source=scan_lib.sh
. "$HOOK_DIR/scan_lib.sh"
scan_lib_init "$HOOK_DIR"

if [ -z "$PYTHON" ]; then
    echo "pre_bash.sh: no working python3 found; cannot analyse the command. Refusing rather than guessing." >&2
    exit 2
fi

if [ "$SCAN_STATUS" -ne 0 ]; then
    # The scanner itself failed. Do NOT silently allow — a failed scan
    # reading as "no git here" is exactly the fail-open shape this
    # rewrite exists to remove. Print what broke so it is fixable.
    echo "pre_bash.sh: could not analyse the command; refusing rather than guessing." >&2
    echo "  interpreter: $PYTHON" >&2
    echo "  scanner:     $HOOK_DIR/git_command_scan.py" >&2
    echo "  exit:        $SCAN_STATUS" >&2
    echo "  output:      $SCAN_OUTPUT" >&2
    exit 2
fi

# ===================================================
# CHECK: Prevent commits to main, in THIS repository
# ===================================================
check_no_commits_to_main() {
    local sub dir degraded btarget remote refspec branch

    # The branch this repository will be on when a commit runs — not the
    # branch it is on now.
    #
    # A PreToolUse hook decides before ANY of the command runs, so live
    # git state is the state before the call. Measured 2026-09-09:
    # `git checkout -q main && ... && git commit --allow-empty -m x`,
    # issued from a feature branch, was allowed by this guard and by the
    # one it replaced, because both asked HEAD and HEAD still said
    # "feature branch". An empty commit landed on main.
    #
    # So the branch is tracked forward through the command instead:
    # start from HEAD, and update on every checkout/switch this call
    # makes in this repository, before deciding about any commit.
    branch=$(git -C "$THIS_REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)

    while IFS=$'\x1f' read -r sub dir degraded btarget remote refspec; do
        # Another repository, or nothing we can resolve: not ours.
        scan_lib_in_this_repo "$dir" || continue

        # A branch change earlier in the same call moves the target.
        if [ "${sub:-}" = "checkout" ] || [ "${sub:-}" = "switch" ]; then
            case "${btarget:-}" in
                "")
                    # Changes no branch at all: `git checkout -- path`,
                    # or a bare checkout. Leave the tracked branch alone
                    # rather than refusing, or every `git checkout --
                    # file && git commit` becomes a false positive.
                    ;;
                \$*)
                    # An unexpanded variable. Cannot tell where this
                    # lands, and it could be main. Fail closed.
                    branch="__unknown__"
                    ;;
                *)
                    branch="$btarget"
                    ;;
            esac
            continue
        fi

        [ "${sub:-}" = "commit" ] || continue

        # Release branches may commit directly.
        [[ "$branch" =~ ^release/ ]] && continue

        if [ "$branch" = "__unknown__" ]; then
            echo "This call changes branch and then commits, and the guard cannot tell which branch the commit lands on." >&2
            echo "Split it: change branch in one call, commit in the next." >&2
            echo "Note: this refusal discards the ENTIRE Bash call, including any file edits in it." >&2
            return 2
        fi

        if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
            if echo "$COMMAND" | grep -qiE "hotfix|emergency"; then
                continue
            fi
            echo "Cannot commit directly to $branch in $THIS_REPO. Create a feature branch first." >&2
            if [ -n "${btarget:-}" ] || echo "$SCAN_OUTPUT" | grep -qE $'^(checkout|switch)\x1f'; then
                echo "(This call switches to $branch before committing; the branch you are on now is not the one that matters.)" >&2
            fi
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
    local sub dir degraded btarget remote refspec

    while IFS=$'\x1f' read -r sub dir degraded btarget remote refspec; do
        [ "${sub:-}" = "tag" ] || continue

        # Same scoping rule: only this repository's tags.
        scan_lib_in_this_repo "$dir" || continue

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

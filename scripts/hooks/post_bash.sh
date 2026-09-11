#!/bin/bash
# PostToolUse hook for Bash commands: after a successful git push from
# THIS repository, watch the CI run for the commit that was pushed.
#
# It used to match `^git push` on the command text and look the run up
# with a bare `gh run list --commit $(git rev-parse HEAD)`. Both were
# measured wrong on 2026-09-11:
#
#   - The anchored match never saw the ordinary merge idiom,
#     `git checkout main && git merge --ff-only x && git push origin main`,
#     so the watch did not fire for any push made that way.
#   - Without a repository, `gh` here resolves the `upstream` remote
#     (there is no default set), so even a matched push looked for its
#     run in the wrong repository and reported "No CI run found".
#
# A monitor that never fires looks exactly like one with nothing to
# report. Now the push is found by git_command_scan.py, the commit is the
# refspec it named (HEAD when it named none), and the repository is the
# URL of the remote it pushed to.
#
# This is a monitor, not a guard: when it cannot tell what was pushed it
# says so and stands down, rather than blocking the call.

set -uo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')
EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exit_code // 0')

# Only a push that succeeded has a run to watch.
[ "$EXIT_CODE" = "0" ] || exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scan_lib.sh
. "$HOOK_DIR/scan_lib.sh"
scan_lib_init "$HOOK_DIR"

if [ "$SCAN_STATUS" -ne 0 ]; then
    echo "post_bash.sh: could not analyse the command (exit $SCAN_STATUS); not watching CI." >&2
    exit 0
fi

PUSH_DIR=""
PUSH_REMOTE=""
PUSH_REFSPEC=""
while IFS=$'\x1f' read -r sub dir degraded btarget remote refspec; do
    [ "${sub:-}" = "push" ] || continue
    scan_lib_in_this_repo "$dir" || continue
    PUSH_DIR="$dir"
    PUSH_REMOTE="${remote:-origin}"
    PUSH_REFSPEC="$refspec"
    break
done <<<"$SCAN_OUTPUT"

[ -n "$PUSH_DIR" ] || exit 0

if ! command -v gh &> /dev/null; then
    echo "gh CLI not found. Check CI manually." >&2
    exit 0
fi

# The commit that was pushed: the source side of the refspec, or HEAD
# when the push named no refspec (git pushes the current branch).
case "$PUSH_REFSPEC" in
    \$*)
        echo "Push refspec is an unexpanded variable ($PUSH_REFSPEC); cannot tell what was pushed. Check CI manually." >&2
        exit 0
        ;;
    :*)
        # `git push origin :branch` deletes; nothing to watch.
        exit 0
        ;;
esac
SRC="${PUSH_REFSPEC%%:*}"
SRC="${SRC#+}"
[ -n "$SRC" ] || SRC="HEAD"

PUSHED_SHA=$(git -C "$PUSH_DIR" rev-parse --verify --quiet "${SRC}^{commit}" 2>/dev/null)
if [ -z "$PUSHED_SHA" ]; then
    echo "Could not resolve '$SRC' to a commit in $PUSH_DIR; not watching CI." >&2
    exit 0
fi

REPO_URL=$(git -C "$PUSH_DIR" remote get-url "$PUSH_REMOTE" 2>/dev/null)
if [ -z "$REPO_URL" ]; then
    echo "Remote '$PUSH_REMOTE' has no URL in $PUSH_DIR; not watching CI." >&2
    exit 0
fi

echo "Pushed ${PUSHED_SHA:0:7} to $PUSH_REMOTE. Waiting for CI to start..." >&2
sleep 10

# Every run the push itself triggered. Not `--limit 1`: a commit can carry
# runs from other events too — measured 2026-09-11, the newest run on a
# pushed commit was a dependabot "dynamic" job, and the Tests run was
# seventh in the list.
RUN_IDS=$(gh run list -R "$REPO_URL" --commit "$PUSHED_SHA" --event push --json databaseId -q '.[].databaseId' 2>/dev/null)

if [ -z "$RUN_IDS" ]; then
    echo "No CI run found for ${PUSHED_SHA:0:7} in $REPO_URL (workflows may not fire on branch pushes; check after opening the PR)." >&2
    exit 0
fi

FAILED=0
for RUN_ID in $RUN_IDS; do
    RUN_NAME=$(gh run view -R "$REPO_URL" "$RUN_ID" --json name -q .name 2>/dev/null)
    echo "Watching CI run #$RUN_ID (${RUN_NAME:-?})..." >&2
    gh run watch -R "$REPO_URL" "$RUN_ID" --exit-status 2>&1 >&2
    if [ $? -ne 0 ]; then
        RUN_URL=$(gh run view -R "$REPO_URL" "$RUN_ID" --json url -q .url 2>/dev/null)
        echo "CI failed: ${RUN_NAME:-run $RUN_ID}. Details: $RUN_URL" >&2
        echo "Fetch logs: gh run view -R $REPO_URL $RUN_ID --log-failed" >&2
        FAILED=1
    fi
done

if [ $FAILED -ne 0 ]; then
    exit 2
fi
echo "CI passed." >&2

exit 0

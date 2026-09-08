# The branch guard blocks on formatting, not on intent

Observations first with the commands that produced them, derivations
labeled and last, gaps named. Same shape as the other notes here, per
`docs/DISCIPLINE.md`.

`scripts/hooks/pre_bash.sh` refuses commits to `main`. It has two
independent problems: it misses the most common way commits are
actually written, and it fires on repositories it has nothing to do
with. Both are silent.

## Observation 1 — what it does

The check, verbatim:

    if ! echo "$COMMAND" | grep -qE "^git commit"; then
        return 0
    fi
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

So a command is inspected only if `git commit` appears at the start of
a line, and the branch is read from the shell's working directory.

## Observation 2 — measured, on `main`

Feeding the hook a crafted payload and reading its exit code. `exit=2`
is a refusal, `exit=0` lets the command run:

    git commit -m "x"                       -> exit=2  refused
    cd /tmp && git commit -m "x"            -> exit=0  ALLOWED
    git add . && git commit -m "x"          -> exit=0  ALLOWED
    git -C /some/other/repo commit -m "x"   -> exit=0  ALLOWED
    git commit -m "hotfix: x"               -> exit=0  allowed by design

**`git add . && git commit -m "..."` is the ordinary way to commit and
the guard does not see it.** The bare form it does catch is the one
almost nobody types.

A command written across several lines *is* caught, because `grep -E
"^..."` anchors to the start of any line and the shell tool accepts
multi-line commands. So the same commit is refused or allowed depending
on whether it was formatted with `&&` or with newlines. That is a
guard keyed on formatting.

## Observation 3 — it fires on the wrong repository

`git rev-parse` runs in the shell's working directory, not in the
repository the commit targets. Observed 2026-09-07: with this
repository checked out on `main`, a commit into an unrelated repository
elsewhere on the machine was refused with "Cannot commit directly to
main. Create a feature branch first." Nothing about that commit
involved this repository or its `main`.

The converse holds and is the more serious direction: from a working
directory outside this repository, `git rev-parse` reports that
repository's branch or fails, so a commit to this repository's `main`
would not be refused.

## Observation 4 — malformed input is allowed

The hook parses its payload with `jq` and does not check the result. On
a payload `jq` rejects, it emits a parse error and returns `exit=0`.
Whatever the command was, it runs.

## Observation 5 — the gap was used, and it is the normal path

The question the rest of this note does not answer is whether anything
actually got through. `git reflog show main` answers it exactly,
because it records *how* `main` moved: `commit:` is a commit made
directly on `main`, `merge <branch>: Fast-forward` came from a branch.

Since the guard was introduced in `2a69048` on 2026-04-04, the reflog
for this clone shows **18 commits made directly on `main`** and 5
arrivals via a branch. Every one of the 18 is authored by the agent
identity, and the guard is registered in `.claude/settings.json` as a
`PreToolUse` hook matching `Bash`, so it was active for all of them.

Direct to `main`, in order: `0c1ebc5`, `4482003`, `8d71397`, `3ab94b3`,
`026ad73`, `32a43ab`, `c2b2936`, `7e9e46f`, `c51851a`, `e774be4`,
`4ee06e7`, `80839f4`, `7becce1`, `b8f57de`, `911a2ea`, `0176e88`, and
two more on 2026-09-07 that were rewritten out of history the same
evening and now exist only in the reflog.

**Two of them are code, not documentation:** `7e9e46f` and `0176e88`,
both connector fixes.

So the guard has refused a commit roughly as often as this repository
has used a feature branch, and the path it exists to prevent is the one
almost every change has taken. Committing to `main` was not an
occasional slip past a working guard; it was the norm, and the guard
was silent throughout.

## Derivations, mine

- **This is a guard that fails open**, in the sense the test notes in
  the fleet SOP use: it returns success when it has not checked
  anything, and nothing distinguishes "inspected and allowed" from "not
  inspected". A refusal is visible; an unchecked pass is not.
- **Its record is not evidence it works.** It has refused commits, which
  reads as the guard functioning. It refused the two commits that
  happened to be written across lines and allowed the single-line ones,
  and no one could see the difference from the outside.
- **The two halves have opposite fixes.** Matching the command more
  thoroughly addresses Observation 2; resolving the branch from the
  commit's target repository addresses Observation 3. Doing only the
  first would make it block unrelated repositories more often.
- **That the 18 direct commits went through `Bash` with the hook
  active is a derivation, not an observation.** It rests on the author identity
  being the agent one and the hook being registered for all `Bash`
  commands. A commit made in a terminal outside the agent harness would
  look identical here and the hook would never have run at all.

## Not fixed here, deliberately

Tightening it changes what is refused for every session working in this
repository, including sessions mid-task that have been relying on the
current behaviour. That is a behaviour change to a shared guard rather
than a defect fix in isolation, so it wants its own decision rather
than being folded into a note about it.

Shape of the fix, if it is taken up: match `git commit` anywhere in the
command rather than at a line start, resolve the branch with `git -C`
against the path the commit actually targets, and fail closed when `jq`
cannot parse the payload.

## Gaps, deliberately named

- Only the branch-protection check was probed. The same file carries a
  tag-creation check with the same `^`-anchored matching, and that one
  was not tested.
- The converse of Observation 3 — a commit to this repository's `main`
  from a working directory elsewhere — was reasoned from the code, not
  run.
- **The reflog is local to this clone and starts at the 2026-05-20
  clone, with a reset to `main-scrubbed` on 2026-08-23.** Anything
  before that cannot be classified this way, and commits made in
  another clone are invisible to it. The count of 18 is a floor.
- Nothing here establishes which of the 18 would have been *refused* by
  a working guard rather than legitimately allowed. `32a43ab` is a
  revert and several are single-file documentation edits; a stricter
  guard might reasonably have permitted some of them anyway.

Recorded 2026-09-07.

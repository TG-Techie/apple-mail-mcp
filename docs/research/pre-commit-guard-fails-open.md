# The branch guard blocks on formatting, not on intent

Observations first with the commands that produced them, derivations
labeled and last, gaps named. Same shape as the other notes here, per
`docs/DISCIPLINE.md`.

`scripts/hooks/pre_bash.sh` refuses commits to `main`. It has two
independent problems: it misses the most common way commits are
actually written, and it fires on repositories it has nothing to do
with. Both are silent.

**Scope, before the numbers below are read as an alarm.** This is one
repository's defect. The guard exists nowhere else in the fleet
(Observation 6), and the sibling projects have no branch guard at all —
which is the better failure, because a project with no guard does not
believe it has one. **The harm here was never the missing protection.
It was the false belief in it, and that belief lives in exactly one
repository.** A reader arriving at "18 commits went through the gap"
should not reach for a fleet-wide conclusion.

**If you are picking this up to fix it, the conclusion is that the
defect is in the idiom and not in any one check.** Three checks across
two files share one anchoring bug. Fixing the branch guard alone leaves
two behind and looks like a fix.

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

**Worth keeping as a technique.** A fast-forward merge and a direct
commit are indistinguishable in `git log` — same shape, one parent, no
merge commit — so the log cannot answer this question at all. The
reflog can, because it records the operation rather than the result.
Its limit is that it is local to one clone and expires.

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

## Observation 6 — the pattern, not the hook, is what repeats

Two questions here. Whether this hook is copied into sibling projects,
and whether the same matching appears elsewhere in it.

**It is not copied.** Searching `~/AgentAccessEnv` and
`~/AgentAccessFleet` for the guard's message and for
`git rev-parse --abbrev-ref HEAD` finds it only in this repository.
`apple-passwords-mcp`, `imessage-mcp` and `reminders-mcp` have no
`scripts/hooks/` and no `PreToolUse` branch guard of any kind. So this
is one project's defect, not a fleet-wide illusion of protection — a
sibling project with no guard at least does not believe it has one.

**The same anchoring repeats three times inside this project**, which
is where it does generalise:

    pre_bash.sh:13    grep -qE "^git commit"    branch protection
    pre_bash.sh:38    grep -qE "^git tag"       tag-workflow enforcement
    post_bash.sh:10   grep -qE "^git push"      CI watch after push

The tag check was probed the same way and behaves identically:

    git tag v9.9.9              -> exit=2  refused
    cd . && git tag v9.9.9      -> exit=0  ALLOWED
    echo x && git tag v9.9.9    -> exit=0  ALLOWED

The `git push` one is a `PostToolUse` monitor rather than a guard, so
its failure is quieter still: on a push it does not match, the CI watch
simply never runs and nothing says so. Read from the code, not probed.

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
- **The defect is in the idiom, not in the one check.** Three checks
  across two files were written the same way, so fixing only the
  branch guard would leave two behind. Whoever fixes this should fix
  the matching once and apply it to all three.
- **That the 18 direct commits went through `Bash` with the hook
  active is a derivation, not an observation.** It rests on the author identity
  being the agent one and the hook being registered for all `Bash`
  commands. A commit made in a terminal outside the agent harness would
  look identical here and the hook would never have run at all.

## A monitor that fails open leaves nothing to find

Not specific to this hook, and the reason the `git push` case is worse
than the two guards even though it permits nothing.

A **guard** that fails open lets something through, and the something
is an artifact. A commit exists, it is in the reflog, it can be counted
after the fact — which is exactly how Observation 5 was possible at
all.

A **monitor** that fails open produces nothing. `post_bash.sh` watches
CI after a push; when its match misses, no watch runs, no record says a
watch did not run, and there is no artifact to audit later. The absence
of a CI report is indistinguishable from a CI report nobody read.

So the two failures are not the same size. The guard's failure is
recoverable because it leaves evidence. The monitor's failure is
invisible in both directions, and the only way to find it is to read
the matching logic — which is how it was found here, rather than by
noticing anything wrong.

## Not fixed here, deliberately

Tightening it changes what is refused for every session working in this
repository, including sessions mid-task that have been relying on the
current behaviour. That is a behaviour change to a shared guard rather
than a defect fix in isolation, so it wants its own decision rather
than being folded into a note about it.

Shape of the fix, if it is taken up. **Fix the matching once and apply
it to all three checks** — the branch guard, the tag guard and the CI
watch — because the defect is in the idiom rather than in any one of
them. Then: match the verb anywhere in the command rather than at a
line start, resolve the branch with `git -C` against the path the
commit actually targets, and fail closed when `jq` cannot parse the
payload.

## Gaps, deliberately named

- The `git push` CI watch in `post_bash.sh` was read from the code and
  not probed. See "A monitor that fails open" above for why that one is
  the worst of the three.
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

## Observation 7 — the guard is not repo-scoped, and gates by the wrong repository

Observed 2026-09-09, unplanned, while committing to a different repository entirely.

The command was a commit into `~/AgentAccessFleet/sop`, a separate git repository with no
relationship to this one:

    cd ~/AgentAccessFleet/sop
    git add testing/<file>.md
    git commit -q -F - <<'MSG'
    ...
    MSG

Result:

    PreToolUse:Bash hook error: [./scripts/hooks/pre_bash.sh]:
    Cannot commit directly to main. Create a feature branch first.

That commit had nothing to do with this project. The mechanism is in
`check_no_commits_to_main`:

    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

`git rev-parse` runs in the hook's own working directory, which is this project, not the
directory the guarded command runs in. A `cd` inside the command does not move the hook. So the
guard reads **this** repository's branch and applies the verdict to a commit in **any**
repository.

The control observation is direct rather than inferred. The identical command shape succeeded
earlier the same day, landing commit `5d2489d` in the SOP repository, while this project was on
`fix/e2e-suite-stale-tool-names`. It began failing immediately after that branch merged and
this project returned to `main`. Nothing about the SOP repository changed between the two.

**The scope is the session, not the machine** — with the two halves of that claim resting on
different evidence, so they are separated here.

*Observed, in this session:* the guard runs for Bash calls made from here regardless of which
directory the command runs in, and gates them by this project's branch. That is the body of this
observation.

*Derived from the mechanism:* hooks are configured in a project's own `.claude/settings.json`,
so a session whose project is a different repository should not run this one at all. This is a
derivation from where the configuration lives, not something measured.

*Consistent with it, one positive result:* crisp-kelp reports committing to
`~/AgentAccessFleet/sop` twice on 2026-09-09 from its own session without hitting the guard.
One session, two commits, relayed rather than observed here. That is consistent with the
derivation and is not proof of it.

If the derivation holds, the effect is narrower than "the SOP is unwritable" and stranger: a
session working here cannot commit to any repository while this project's `HEAD` is `main`,
while a session elsewhere runs the identical command against the identical repository and
succeeds.

### A second mechanism, found while working around the first

The obvious repair — create a feature branch, then commit — fails when both are sent as one
Bash call:

    git checkout -q -b docs/guard-is-not-repo-scoped
    ... edit ...
    git commit -q -F - <<'MSG'

This is a `PreToolUse` hook. It evaluates the whole command string before any of it runs, so at
evaluation time `HEAD` is still `main`, the guard returns 2, and **the entire call is aborted**
— the `checkout` never happens either, and neither does the file edit that sat between them.
The branch has to be created in its own call, and the commit sent in the next one.

Worth naming because the failure is silent in a specific way: the refusal message names the
commit, so the natural reading is that only the commit was rejected. Everything else in the
call is discarded with it, including edits, and nothing says so.

### Why this belongs beside the fail-open finding rather than replacing it

The guard now has both failure directions recorded, from the same twenty lines:

- **Fails open** on `git add . && git commit -m ...`, because `^git commit` is anchored and
  `git add` occupies the start of the line. That is Observation 3, and it is the ordinary idiom.
- **Fails closed** across repository boundaries, blocking commits it has no business having an
  opinion about, because the branch it reads is not the branch being committed to.

A guard that can be walked past by the most common spelling of the thing it guards, and that
also refuses unrelated work in other repositories, is not calibrated in either direction. Both
follow from the same design: matching on the text of the command instead of on what the command
will do, and reading state from the hook's environment instead of the command's.

### Still not fixed, and still deliberately

**Whose file this is, established 2026-09-09 rather than assumed.** The hook is this
repository's own file and nobody else's. `scripts/hooks/pre_bash.sh` is referenced from
`.claude/settings.json` here and from no configuration outside this repo; a search of
`~/AgentAccessEnv` and `~/AgentAccessFleet` finds no other copy of it (the other directories
named `hooks` are `node_modules`, virtualenvs and control-pane snapshots). Its entire history is
three commits, all here: `55659b5`, `8d57a72`, `2a69048`.

That matters because an earlier reading of this section treated the guard as somebody else's
work to leave alone. It is not. The fleet rule about not editing inside another agent's work
does not apply, and neither does the doctrine about Claude's own configuration files — that
covers the global settings JSON, any `.claude/settings.json` or `settings.local.json`, and any
`.mcp.json`. `pre_bash.sh` is none of those. Editing it is ordinary work in this repository.

**The constraint that does apply is blast radius, not ownership.** This hook runs for every
session whose project is this repository, and it is the permission surface those sessions work
against. A guard that starts matching far more commands is a live change to what an agent here
can do without a prompt, in sessions nobody is watching at the time. Fixing the matching alone
would be the worst version of it: the guard would fire correctly on many more commands while
still reading the wrong repository's branch, turning something wrong quietly into something
wrong loudly, in other people's repositories.

So it stays unfixed pending Jonah's call — raised with him rather than decided here, and not
batched with anything else, because the failure mode of getting it wrong is an agent blocked or
unblocked somewhere neither of us is looking. plush-kelp has an open, unanswered question with
him on the same defect one level up: config-guard Bash rules that match command text rather than
command effect. The measured evidence here — `git add . && git commit -m ...` walking past
`grep -qE "^git commit"` — is the concrete instance that question lacked, so the two go to him
together.

**Correction, 2026-09-09.** An earlier version of this section said all three checks share both
the `^`-anchored matching and the ambient-cwd assumption. Only the first half is true, and the
sentence was written from memory rather than from the file. Read back from
`scripts/hooks/pre_bash.sh` and `scripts/hooks/post_bash.sh`:

| check | file | matches | reads git state |
|---|---|---|---|
| `check_no_commits_to_main` | `pre_bash.sh:13` | `^git commit` | yes — `git rev-parse` in the hook's cwd |
| `check_tag_creation_workflow` | `pre_bash.sh:38` | `^git tag` | no |
| CI watch after push | `post_bash.sh:10` | `^git push` | no |

`pre_bash.sh` defines exactly two checks, not three; the third is in the other file. **All three
share the anchored-match defect. Only the branch guard has the ambient-cwd defect**, because it
is the only one that reads repository state at all.

So a fix addresses the matching in all three, and the cwd question in one. That is a smaller and
better-defined change than the sentence it replaces implied, which is the reason to get it right
rather than leave a tidy overstatement standing.

What landed the blocked SOP commit was satisfying the guard rather than bypassing it: this
project moved to a feature branch, and the commit in the other repository then went through.
Nothing about that commit changed, only this project's `HEAD`.

## Observation 8 — fixed, and what the fix cost to get right

Fixed 2026-09-09. Both directions, together, as one change: `scripts/hooks/git_command_scan.py`
plus a rewritten `scripts/hooks/pre_bash.sh`.

### What changed

The hook no longer greps the command text. It asks a parser two questions — **which git
subcommands does this command line run, and in which directory does each one run** — and makes
its decision from the answers. The branch guard now applies only when the target repository is
this one, and the tag guard likewise.

### Acceptance, old against new, measured

Every row is the two hooks run against the same JSON input, exit code only.

| command | old | new |
|---|---|---|
| `git add . && git commit -m "x"` | 0 | 2 |
| `git add -A; git commit` | 0 | 2 |
| `GIT_EDITOR=true git commit --amend` | 0 | 2 |
| `git commit -m "x"` | 2 | 2 |
| `git tag v1.0.0` | 2 | 2 |
| `ls -la && echo hi` | 0 | 0 |
| `echo "git commit -m x"` | 0 | 0 |
| multi-line: `cd <sop repo>` then `git commit` at line start | **2** | **0** |

The first three rows are the fail-open defect closing. The last row is the fail-closed defect
closing, and it is the exact command shape that blocked a commit into the SOP repository earlier
in the day: `grep` is line-oriented, so `^git commit` matched a line inside a multi-line command
whose working directory was a different repository entirely.

Rows 4 to 7 matter as much: unchanged behaviour where behaviour should not change, including no
new false positive on a string that merely mentions the command.

### Two things found only by doing it

**The interpreter on the hook's PATH is broken.** `python3` there resolves to
`~/.tg/bin/python3`, which links against a Homebrew Python 3.14 framework that no longer exists
and dies with a dyld error, exit 134. The first version of this hook called plain `python3`,
could not analyse anything, and — because it correctly refuses rather than guessing when the
scan fails — refused every Bash call in the session until it was reverted. The hook now resolves
an interpreter it has verified will run, preferring `/usr/bin/python3`, and the scanner is kept
compatible with 3.9 for that reason.

**Commit messages are heredocs, and they quote commands.** Several commits in this repository
have bodies containing the literal words `git commit`, written while documenting this very
defect. A parser that treated every line as a command would read a message *describing* a
command as *running* one. The scanner strips heredoc bodies before parsing.

### The lockout, recorded because it is the general lesson

Installing the untested hook made every Bash call fail. The recovery was not clever: the file
tools are not gated by a Bash hook, so `pre_bash.sh` was restored with Write, and the work then
continued with the new hook staged at a different filename and tested by feeding it JSON
directly — old and new side by side — until the table above was green.

**Never install a guard you have not run against inputs whose answers you already know.** A
guard is the one kind of code that can prevent you from fixing it. The staged-file-plus-known-
answers method costs a few minutes and removes that whole class of problem.

### Still open

`post_bash.sh:10` still matches `^git push` and carries the same fail-open defect. It is a
monitor rather than a guard — failing open loses a CI watch rather than admitting a bad commit —
so it is left for a separate change rather than bundled into a guard rewrite that had already
locked the session once. *(Closed in Observation 10.)*

## Observation 9 — the guard read HEAD, and HEAD is not where the commit lands

Found 2026-09-09, minutes after Observation 8 shipped, by running the new guard against an
input whose answer was known. It is the same class as everything else here and it survived the
rewrite, so it is worth its own entry.

### The escape

    git checkout -q main && git merge --ff-only <branch> -q && git push origin main
    git commit --allow-empty -m "this must be refused"

Issued from a feature branch, this was **allowed**, and an empty commit landed on `main`. It was
reverted with `git reset --soft HEAD~1` before it reached the remote.

### Why, and why the rewrite did not fix it

A `PreToolUse` hook decides before **any** of the command runs. So `git rev-parse --abbrev-ref
HEAD` inside the hook reports the branch as it was *before* the call — and the call's own
`git checkout main` has not happened yet. The guard asked "what branch am I on", got "a feature
branch", and allowed a commit that would land on main.

The old hook had this too; it is not a regression introduced by Observation 8. Both versions
asked the same wrong question. The rewrite replaced text-matching with effect-matching and left
the state question untouched, which is a good illustration that fixing one axis of a defect does
not audit the others.

### The fix

The branch is now tracked **forward through the command** rather than read once. The scanner
reports, for each `checkout`/`switch`, the branch that invocation moves HEAD to; the hook starts
from the current branch and applies each change in order before deciding about any commit that
follows.

Three cases, deliberately distinguished, because collapsing them produces either a hole or a
false positive:

| in the command | tracked branch becomes | why |
|---|---|---|
| `git checkout main`, `git switch main` | `main` | determinable |
| `git checkout -- path`, bare `git checkout` | unchanged | changes no branch; refusing here would break `git checkout -- f && git commit` |
| `git checkout $BRANCH` | unknown → refuse | could be main; fail closed |

### Acceptance, measured against the installed hook

| command (from a feature branch) | exit |
|---|---|
| `git checkout -q main && git commit --allow-empty -m x` | 2 |
| `git switch main && git commit -m x` | 2 |
| `git checkout $BRANCH && git commit -m x` | 2 |
| `git checkout -b feature/new && git commit -m x` | 0 |
| `git checkout -- src/file.py && git commit -m x` | 0 |
| `git add . && git commit -m x` | 0 |
| `cd <sop repo> && git commit -m x` | 0 |
| `ls -la` | 0 |

### The lesson, which is not about git

**A guard that reads live state is reading the state before the call, not the state the guarded
action will see.** Anything a command does to itself — changing directory, changing branch,
creating the file it then checks — is invisible to a pre-execution check unless the check models
it. This generalises to any pre-flight validation: the world it inspects is the world before,
and the action runs in the world after.

It was found only by running the guard against a case whose answer was known independently.
Nothing in the unit tests would have caught it, because the unit tests test the parser, and the
parser was right — the hook was asking it the wrong question.

## Observation 10 — the monitor had never watched anything

`post_bash.sh` is the PostToolUse hook that watches CI after a push. Fixed 2026-09-11. It had
the anchored text match from Observation 7, and a second defect underneath that the match had
been hiding.

### Measured

Every push this session was made as `git checkout -q main && git merge --ff-only -q <branch>
&& git push -q origin main`. `grep -qE "^git push"` does not match that line, so the hook
exited at line 10 on every one of them. No watch ever fired, and nothing said so — a monitor
that never fires is indistinguishable from one with nothing to report.

Had the match fired, the lookup would still have failed. The hook ran `gh run list --commit
$(git rev-parse HEAD)` with no repository. This checkout has an `upstream` remote and no `gh`
default set, and in that state `gh` resolves the **upstream** repository:

    $ gh repo view --json nameWithOwner --jq .nameWithOwner
    s-morgan-jeffries/apple-mail-fast-mcp
    $ gh run list --commit <sha on main>            # nothing
    $ gh run list -R <origin url> --commit <sha>    # {"conclusion":"success", ...}

So a matched push would have looked for its run in the upstream author's repository, found
nothing, and printed "No CI run found … check after opening the PR". Same day, the same
misdirection had `gh issue list` showing upstream's tracker as if it were ours.

One more, found once the first two were fixed: `--limit 1` takes the newest run on the commit,
whichever event produced it. On the commit measured, that was a dependabot "dynamic" job; the
Tests run was seventh in the list.

### The fix

The push is found by `git_command_scan.py`, which now reports a push's remote and refspec
alongside the subcommand. The commit watched is the refspec's source side (`HEAD` when the push
named none), resolved in the directory the push ran in. The repository is the URL of the remote
that was pushed to, passed to `gh` with `-R`. Every run the push event triggered on that commit
is watched, and the hook exits 2 if any failed.

The interpreter resolution and the scan moved into `scan_lib.sh`, sourced by both hooks, so the
guard and the monitor cannot drift apart on them again.

### A field-separator defect found on the way

The scanner's shell output was tab-separated. Tab is IFS whitespace, and bash's `read` collapses
runs of IFS whitespace — so a row with an empty middle column (`push`, with no branch target)
had every later column shift one place left. Measured: the monitor read `origin` as the branch
target and `main` as the remote, and reported "Remote 'main' has no URL". The separator is now
ASCII 0x1f, which `read` does not collapse. The guard had been reading the same rows; it never
noticed because it only consults the branch-target column on `checkout`/`switch` rows, where the
column is non-empty.

### Acceptance, measured against the installed hooks

Guard, on a feature branch (0 allow, 2 refuse): `git commit` 0; `git add . && git commit` 0;
`git checkout main && git commit --allow-empty` 2; `git checkout -- path && git commit` 0;
`git checkout $B && git commit` 2; `git -C /tmp commit` 0; `git tag v1` 2; `ls` 0.

Monitor: failed push → stands down; non-push → stands down; push in another repository → stands
down; `git push origin :old` (a delete) → stands down; `git push origin $B` → says it cannot
tell what was pushed; the merge idiom above, against an already-pushed commit → finds the Tests
run in the origin repository and reports its conclusion.


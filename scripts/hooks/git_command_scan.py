#!/usr/bin/env python3
"""Work out which git subcommands a shell command runs, and where.

The hooks in this directory used to ask ``grep -qE "^git commit"``. That
matches the *text* of a command rather than its *effect*, and two
measured defects followed:

1. **Failed open.** ``git add . && git commit -m "x"`` does not begin
   with ``git commit``, so the ordinary compound idiom walked past the
   branch guard entirely. Measured, not inferred from the regex.
2. **Failed closed across repositories.** The branch was read with
   ``git rev-parse`` in the hook's own working directory — which is this
   project, never the directory the command runs in — so a commit into an
   unrelated repository was refused on the basis of this project's
   branch.

Both dissolve once the question is asked properly: *which git
subcommands does this command line run, and in what directory does each
one run?* This module answers exactly that and nothing else. It makes no
policy decisions; the hooks do.

It is deliberately not a shell parser. It handles the constructs that
appear in agent and human command lines — separators, ``cd``, leading
environment assignments, ``git -C`` — and when it cannot read a command
it says so and guesses toward detection rather than away from it. A
guard that reports "no git here" because it could not parse the line is
the fail-open defect wearing a different coat.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass

__all__ = ["GitInvocation", "scan_git_commands"]

# Characters that end one command and begin another. Everything after a
# run of these starts a fresh argv, which is what the old anchored regex
# could not see. shlex groups a run of punctuation into one token, so
# `&&\n` arrives as a single token; a token made only of these
# characters is a separator whatever its length.
_SEPARATOR_CHARS = frozenset("&|;\n")

# shlex's default punctuation set, plus newline — so a newline is a
# separator token rather than whitespace, and the whole command can be
# tokenized at once. Line-by-line tokenization broke a double-quoted
# string that continued onto the next line (a multi-line commit
# message): its first line was an unbalanced quote, fell to the
# degraded fallback, and was attributed to the base directory rather
# than the one a preceding `cd` had moved to. Measured 2026-09-11 as a
# commit into another repository refused as a commit to this one's main.
_PUNCTUATION = "();<>|&\n"

# git's own global options, before the subcommand. Those in this set
# consume the following token as their value, so the value is never
# mistaken for the subcommand (`git -c user.name=x commit` would
# otherwise look like a `user.name=x` subcommand).
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


@dataclass(frozen=True)
class GitInvocation:
    """One git command found in a command line.

    Attributes:
        subcommand: The git subcommand, e.g. ``"commit"``.
        directory: The directory this invocation runs in, already
            resolved: the base directory, or wherever a preceding ``cd``
            moved to, or the argument of this invocation's own ``-C``.
        degraded: True when the command line could not be tokenized and
            this result came from the fallback scan. A caller must not
            treat a degraded result as a reliable reading of the
            command — only as "something that looks like this ran".
        branch_target: For ``checkout`` and ``switch``, the branch this
            invocation moves HEAD to. ``None`` for every other
            subcommand, and for a checkout that touches paths rather
            than branches.

            This exists because a PreToolUse hook decides before any of
            the command runs, so live git state is the state *before*
            the call. ``git checkout main && git commit`` issued from a
            feature branch reads as "on a feature branch" and is allowed;
            measured 2026-09-09, and an empty commit landed on main. A
            guard has to ask what branch the command will be on when it
            commits, not what branch it is on now.
        remote: For ``push``, the repository argument (or ``--repo``),
            when one was given. ``None`` otherwise.
        refspec: For ``push``, the first refspec argument, when one was
            given. ``None`` otherwise — which for a push means "whatever
            the current branch's upstream is".

            These exist so the post-push CI monitor can find the run for
            the commit that was actually pushed, in the repository it was
            pushed to. It used to read HEAD and call ``gh`` with no
            repository; with an ``upstream`` remote present, ``gh``
            resolves that, and the run was looked up in the wrong place.
    """

    subcommand: str
    directory: str
    degraded: bool = False
    branch_target: str | None = None
    remote: str | None = None
    refspec: str | None = None


def _resolve(base: str, target: str) -> str:
    """Resolve a ``cd`` or ``-C`` argument against the current directory."""
    expanded = os.path.expanduser(target)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(base, expanded))


def _is_git(word: str) -> bool:
    """True for ``git`` and ``/usr/bin/git``, false for ``gitk``."""
    return word == "git" or word.endswith("/git")


def _is_separator(token: str) -> bool:
    return bool(token) and set(token) <= _SEPARATOR_CHARS


def _tokenize(text: str) -> list[str]:
    """Tokenize shell text; raises ValueError on an unbalanced quote."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if _is_separator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _strip_env_assignments(words: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` prefixes.

    ``GIT_EDITOR=true git commit`` runs git; the assignments are not the
    command.
    """
    i = 0
    while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
        name = words[i].split("=", 1)[0]
        if name and (name[0].isalpha() or name[0] == "_") and name.replace("_", "").isalnum():
            i += 1
            continue
        break
    return words[i:]


_BRANCH_CHANGING = {"checkout", "switch"}

# Flags on checkout/switch that take the new branch name as their value.
_NEW_BRANCH_FLAGS = {"-b", "-B", "-c", "-C"}


def _branch_target(subcommand: str, rest: list[str]) -> str | None:
    """The branch a checkout/switch moves HEAD to, if determinable.

    ``rest`` is everything after the subcommand. Returns None when the
    invocation changes no branch — ``git checkout -- path``, or a
    checkout with no ref at all.
    """
    if subcommand not in _BRANCH_CHANGING:
        return None

    i = 0
    while i < len(rest):
        word = rest[i]
        if word == "--":
            # Everything after this is paths, not refs.
            return None
        if word in _NEW_BRANCH_FLAGS:
            if i + 1 < len(rest):
                return rest[i + 1]
            return None
        if word.startswith("-"):
            i += 1
            continue
        return word
    return None


# push options that take their value as the NEXT word (the `=` form is a
# single word and needs no special handling).
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _push_targets(subcommand: str, rest: list[str]) -> tuple[str | None, str | None]:
    """The (remote, refspec) of a push, each None when not given.

    ``git push [<options>] [<repository> [<refspec>...]]``. The first
    positional is the repository, the second the refspec; ``--repo=<r>``
    also names the repository.
    """
    if subcommand != "push":
        return None, None

    remote: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        word = rest[i]
        if word.startswith("--repo="):
            remote = word[len("--repo="):]
        elif word in _PUSH_OPTS_WITH_VALUE:
            if word == "--repo" and i + 1 < len(rest):
                remote = rest[i + 1]
            i += 2
            continue
        elif word.startswith("-"):
            pass
        else:
            positionals.append(word)
        i += 1

    if positionals:
        remote = positionals[0]
    refspec = positionals[1] if len(positionals) > 1 else None
    return remote, refspec


def _subcommand_and_dir(
    words: list[str], cwd: str
) -> tuple[str, str, str | None, str | None, str | None] | None:
    """Extract the subcommand, effective directory, branch target, and
    push remote/refspec."""
    directory = cwd
    i = 1  # words[0] is git itself
    while i < len(words):
        word = words[i]
        if not word.startswith("-"):
            rest = words[i + 1 :]
            remote, refspec = _push_targets(word, rest)
            return word, directory, _branch_target(word, rest), remote, refspec
        if word == "-C" and i + 1 < len(words):
            directory = _resolve(directory, words[i + 1])
            i += 2
            continue
        if word in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if word.startswith("--git-dir=") or word.startswith("--work-tree="):
            i += 1
            continue
        i += 1
    return None


def _fallback(command: str, base_dir: str) -> list[GitInvocation]:
    """Last resort when the line cannot be tokenized.

    Errs toward detection. Reports every git subcommand that appears as
    a word anywhere in the raw text, attributed to the base directory,
    and marks the result degraded so the caller knows it is a guess.
    """
    found: list[GitInvocation] = []
    words = command.replace("\n", " ").split()
    for index, word in enumerate(words):
        if _is_git(word) and index + 1 < len(words):
            for candidate in words[index + 1 :]:
                if not candidate.startswith("-"):
                    found.append(GitInvocation(candidate, base_dir, degraded=True))
                    break
    return found


_HEREDOC_RE = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")


def _strip_heredoc_bodies(command: str) -> list[str]:
    """Return the command's lines with heredoc bodies removed.

    A heredoc body is data, not commands. This matters here because the
    commit messages these hooks guard are themselves written as
    heredocs, and those messages routinely quote command lines —
    including ``git commit``. Without this, a message *describing* a
    command would be read as *running* it.
    """
    lines = command.split("\n")
    kept: list[str] = []
    terminator: str | None = None

    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue

        kept.append(line)

        match = _HEREDOC_RE.search(line)
        if match:
            terminator = match.group(1) or match.group(2) or match.group(3)

    return kept


def _scan_segments(
    segments: list[list[str]], cwd: str, invocations: list[GitInvocation]
) -> str:
    """Read each argv in order, tracking ``cd``; returns the final cwd."""
    for words in segments:
        stripped = _strip_env_assignments(words)
        if not stripped:
            continue

        if stripped[0] == "cd" and len(stripped) > 1:
            cwd = _resolve(cwd, stripped[1])
            continue

        if not _is_git(stripped[0]):
            continue

        result = _subcommand_and_dir(stripped, cwd)
        if result is not None:
            subcommand, directory, branch_target, remote, refspec = result
            invocations.append(
                GitInvocation(
                    subcommand,
                    directory,
                    branch_target=branch_target,
                    remote=remote,
                    refspec=refspec,
                )
            )
    return cwd


def scan_git_commands(command: str, base_dir: str) -> list[GitInvocation]:
    """Find every git invocation in ``command``.

    Args:
        command: The shell command line, exactly as it will be run.
        base_dir: The directory the command starts in.

    Returns:
        One :class:`GitInvocation` per git command found, in order.
        Empty when the line runs no git commands.
    """
    invocations: list[GitInvocation] = []
    text = "\n".join(_strip_heredoc_bodies(command))

    try:
        tokens = _tokenize(text)
    except ValueError:
        # Unbalanced quote or similar. Read the command line by line so
        # the lines that do parse are still read properly, in order and
        # with `cd` tracked; only the broken line is guessed at. Guess
        # toward detection: see _fallback, and the module docstring on
        # why not returning [].
        cwd = base_dir
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                line_tokens = _tokenize(line)
            except ValueError:
                invocations.extend(_fallback(line, cwd))
                continue
            cwd = _scan_segments(_split_segments(line_tokens), cwd, invocations)
        return invocations

    _scan_segments(_split_segments(tokens), base_dir, invocations)
    return invocations


_FIELD_SEP = "\x1f"


def _main(argv: list[str]) -> int:
    """CLI for the shell hooks: command on stdin, one line per git call.

    Usage::

        git_command_scan.py [--format=usv] <base_dir>   # command on stdin

    Prints one line per git invocation, six fields separated by ASCII
    unit separator (0x1f)::

        <subcommand> <directory> <degraded> <branch_target> <remote> <refspec>

    where ``degraded`` is 1 or 0, ``branch_target`` is empty unless the
    invocation moves HEAD, and ``remote``/``refspec`` are empty unless
    the invocation is a push that named them.

    Not tab: tab is IFS whitespace, and bash's ``read`` collapses runs
    of IFS whitespace, so an empty middle field shifted every field
    after it one column left. Measured 2026-09-11: a push row read
    ``origin`` as the branch target and ``main`` as the remote.

    Empty stdout means the command runs no git commands. That is a real
    answer and is deliberately distinct from a non-zero exit, which means
    the scan itself failed. The hooks must treat them differently: a
    failed scan that reads as "no git here" is the fail-open defect this
    module was written to remove.
    """
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(
            "usage: git_command_scan.py [--format=usv] <base_dir>  "
            "(command on stdin)",
            file=sys.stderr,
        )
        return 64

    base_dir = args[0]
    command = sys.stdin.read()

    for inv in scan_git_commands(command, base_dir):
        print(
            _FIELD_SEP.join(
                [
                    inv.subcommand,
                    inv.directory,
                    "1" if inv.degraded else "0",
                    inv.branch_target or "",
                    inv.remote or "",
                    inv.refspec or "",
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

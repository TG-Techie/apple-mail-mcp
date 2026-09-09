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

# Tokens that end one command and begin another. Everything after one of
# these starts a fresh argv, which is what the old anchored regex could
# not see.
_SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}

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
    """

    subcommand: str
    directory: str
    degraded: bool = False


def _resolve(base: str, target: str) -> str:
    """Resolve a ``cd`` or ``-C`` argument against the current directory."""
    expanded = os.path.expanduser(target)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(base, expanded))


def _is_git(word: str) -> bool:
    """True for ``git`` and ``/usr/bin/git``, false for ``gitk``."""
    return word == "git" or word.endswith("/git")


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
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


def _subcommand_and_dir(words: list[str], cwd: str) -> tuple[str, str] | None:
    """Extract the subcommand and effective directory from one git argv."""
    directory = cwd
    i = 1  # words[0] is git itself
    while i < len(words):
        word = words[i]
        if not word.startswith("-"):
            return word, directory
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
    cwd = base_dir

    for line in _strip_heredoc_bodies(command):
        if not line.strip():
            continue

        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quote or similar. Guess toward detection: see
            # _fallback, and the module docstring on why not returning [].
            invocations.extend(_fallback(line, cwd))
            continue

        for words in _split_segments(tokens):
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
                subcommand, directory = result
                invocations.append(GitInvocation(subcommand, directory))

    return invocations


def _main(argv: list[str]) -> int:
    """CLI for the shell hooks: command on stdin, one line per git call.

    Usage::

        git_command_scan.py [--format=tsv] <base_dir>   # command on stdin

    Prints one ``<subcommand>\\t<directory>\\t<degraded>`` line per git
    invocation, where ``degraded`` is 1 or 0.

    Empty stdout means the command runs no git commands. That is a real
    answer and is deliberately distinct from a non-zero exit, which means
    the scan itself failed. The hooks must treat them differently: a
    failed scan that reads as "no git here" is the fail-open defect this
    module was written to remove.
    """
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(
            "usage: git_command_scan.py [--format=tsv] <base_dir>  "
            "(command on stdin)",
            file=sys.stderr,
        )
        return 64

    base_dir = args[0]
    command = sys.stdin.read()

    for inv in scan_git_commands(command, base_dir):
        print(f"{inv.subcommand}\t{inv.directory}\t{1 if inv.degraded else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

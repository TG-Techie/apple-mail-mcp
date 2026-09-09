"""Tests for the hook's git-command parser.

The guard these back used ``grep -qE "^git commit"``. Two measured
defects followed from matching command *text* rather than command
*effect*:

1. Fails open. ``git add . && git commit -m "x"`` does not start with
   ``git commit``, so the ordinary compound idiom walked straight past
   the guard. Observed, not reasoned from the regex.
2. Fails closed across repositories. The guard resolved the branch with
   ``git rev-parse`` in the hook's own working directory, which is this
   project, so it refused a commit into an unrelated repository based on
   this project's branch.

Both are fixed by answering two questions properly: which git
subcommands does this command line actually run, and in which directory
does each one run. That is what ``scan_git_commands`` does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts" / "hooks"))

from git_command_scan import scan_git_commands  # noqa: E402

BASE = "/repo"


def subcommands(command: str, base: str = BASE) -> list[str]:
    return [inv.subcommand for inv in scan_git_commands(command, base)]


def dirs_for(command: str, sub: str, base: str = BASE) -> list[str]:
    return [
        inv.directory
        for inv in scan_git_commands(command, base)
        if inv.subcommand == sub
    ]


class TestFindsTheSubcommand:
    def test_bare_commit(self) -> None:
        assert subcommands('git commit -m "x"') == ["commit"]

    def test_commit_after_add_is_found(self) -> None:
        # THE defect. The old guard missed exactly this.
        assert subcommands('git add . && git commit -m "x"') == ["add", "commit"]

    def test_semicolon_separator(self) -> None:
        assert subcommands("git add .; git commit -m x") == ["add", "commit"]

    def test_or_separator(self) -> None:
        assert subcommands("git add . || git commit -m x") == ["add", "commit"]

    def test_newline_separator(self) -> None:
        assert subcommands("git add .\ngit commit -m x") == ["add", "commit"]

    def test_pipe_separator(self) -> None:
        assert subcommands("git log | head") == ["log"]

    def test_leading_whitespace(self) -> None:
        assert subcommands('   git commit -m "x"') == ["commit"]

    def test_env_assignment_prefix(self) -> None:
        assert subcommands('GIT_EDITOR=true git commit --amend') == ["commit"]

    def test_multiple_env_assignments(self) -> None:
        assert subcommands("A=1 B=2 git tag v1") == ["tag"]

    def test_absolute_git_path(self) -> None:
        assert subcommands("/usr/bin/git commit -m x") == ["commit"]

    def test_global_flags_before_subcommand(self) -> None:
        assert subcommands("git --no-pager log") == ["log"]

    def test_dash_c_config_flag_value_is_not_the_subcommand(self) -> None:
        assert subcommands("git -c user.name=x commit -m y") == ["commit"]

    def test_non_git_commands_yield_nothing(self) -> None:
        assert subcommands("ls -la && echo hello") == []

    def test_word_containing_git_is_not_git(self) -> None:
        assert subcommands("gitk --all") == []
        assert subcommands("echo git commit") == []

    def test_quoted_string_mentioning_git_is_not_a_command(self) -> None:
        assert subcommands('echo "git commit -m x"') == []


class TestResolvesTheDirectory:
    def test_defaults_to_the_base_directory(self) -> None:
        assert dirs_for('git commit -m "x"', "commit") == [BASE]

    def test_cd_changes_the_directory(self) -> None:
        # THE second defect. The old guard read the hook's own cwd and
        # gated a commit in a completely different repository.
        assert dirs_for("cd /other && git commit -m x", "commit") == ["/other"]

    def test_cd_relative_resolves_against_base(self) -> None:
        assert dirs_for("cd sub && git commit -m x", "commit") == ["/repo/sub"]

    def test_cd_applies_only_after_it_runs(self) -> None:
        out = scan_git_commands("git add . && cd /other && git commit -m x", BASE)
        by_sub = {inv.subcommand: inv.directory for inv in out}
        assert by_sub["add"] == BASE
        assert by_sub["commit"] == "/other"

    def test_git_dash_C_sets_the_directory_for_that_invocation_only(self) -> None:
        out = scan_git_commands("git -C /other commit -m x && git status", BASE)
        by_sub = {inv.subcommand: inv.directory for inv in out}
        assert by_sub["commit"] == "/other"
        assert by_sub["status"] == BASE

    def test_git_dash_C_still_finds_the_subcommand(self) -> None:
        assert subcommands("git -C /other commit -m x") == ["commit"]

    def test_cd_with_tilde_expands(self) -> None:
        got = dirs_for("cd ~ && git commit -m x", "commit")
        assert got == [str(Path.home())]


class TestFailsClosedOnInputItCannotParse:
    def test_unbalanced_quote_still_reports_a_commit(self) -> None:
        # shlex raises on this. A guard that returns "no git here"
        # because it could not read the command is a guard that fails
        # open, which is the defect this whole module exists to fix.
        out = scan_git_commands('git commit -m "unterminated', BASE)
        assert "commit" in [inv.subcommand for inv in out]

    def test_unparseable_input_marks_itself_as_degraded(self) -> None:
        out = scan_git_commands('git commit -m "unterminated', BASE)
        assert any(inv.degraded for inv in out), (
            "a fallback result must say it is a fallback, so a caller "
            "cannot mistake a guess for a parse"
        )

    def test_clean_input_is_not_marked_degraded(self) -> None:
        out = scan_git_commands('git commit -m "x"', BASE)
        assert not any(inv.degraded for inv in out)


class TestTheRegressionsThatMotivatedThis:
    """Both defects, stated as the acceptance test rather than as prose."""

    def test_the_compound_idiom_is_caught(self) -> None:
        assert "commit" in subcommands('git add . && git commit -m "wip"')

    def test_a_commit_in_another_repo_is_attributed_to_that_repo(self) -> None:
        dirs = dirs_for(
            "cd /Users/someone/other-repo && git commit -q -F -", "commit"
        )
        assert dirs == ["/Users/someone/other-repo"]
        assert dirs != [BASE]


@pytest.mark.parametrize(
    "command",
    [
        'git add . && git commit -m "x"',
        "git add -A; git commit",
        "cd /tmp && git add f && git commit -m y",
        "GIT_EDITOR=true git commit --amend --no-edit",
        "git -C /elsewhere commit -m z",
    ],
)
def test_every_known_commit_spelling_is_detected(command: str) -> None:
    assert "commit" in subcommands(command), f"missed a commit in: {command}"


class TestHeredocBodiesAreDataNotCommands:
    """The commit messages these hooks guard are written as heredocs, and
    those messages quote command lines. A message *describing* a command
    must not be read as *running* it.

    This is not hypothetical: several commits in this repository have
    bodies containing the literal words "git commit", written while
    documenting the very defect these hooks have.
    """

    def test_heredoc_body_mentioning_git_commit_is_not_a_command(self) -> None:
        command = (
            "git commit -q -F - <<'MSG'\n"
            "docs: explain the guard\n"
            "\n"
            "The old form missed `git add . && git commit -m x`.\n"
            "git commit is what it failed to catch.\n"
            "MSG"
        )
        # Exactly one commit: the real one. Not the two in the message.
        assert subcommands(command) == ["commit"]

    def test_heredoc_body_does_not_hide_a_later_real_command(self) -> None:
        command = (
            "git commit -q -F - <<'MSG'\n"
            "a message\n"
            "MSG\n"
            "git push origin main"
        )
        assert subcommands(command) == ["commit", "push"]

    def test_unquoted_heredoc_delimiter_also_works(self) -> None:
        command = "git commit -F - <<MSG\ngit tag v9\nMSG"
        assert subcommands(command) == ["commit"]

    def test_double_quoted_heredoc_delimiter_also_works(self) -> None:
        command = 'git commit -F - <<"MSG"\ngit tag v9\nMSG'
        assert subcommands(command) == ["commit"]

    def test_dash_suppressed_heredoc_delimiter(self) -> None:
        command = "git commit -F - <<-MSG\n\tgit tag v9\n\tMSG"
        assert subcommands(command) == ["commit"]

    def test_a_real_session_shape_is_read_correctly(self) -> None:
        # Copied from what this session actually ran, which is the input
        # whose answer is known independently of this parser.
        command = (
            "cd /Users/agent-access/AgentAccessFleet/sop\n"
            "git add work/recording/entry.md\n"
            "git commit -q -F - <<'MSG'\n"
            "recording: something\n"
            "MSG"
        )
        out = scan_git_commands(command, BASE)
        by_sub = {inv.subcommand: inv.directory for inv in out}
        assert set(by_sub) == {"add", "commit"}
        assert by_sub["commit"] == "/Users/agent-access/AgentAccessFleet/sop", (
            "a commit into the SOP repo must be attributed to the SOP repo, "
            "not to this project — that is the second defect"
        )


class TestBranchChangesInTheSameCall:
    """A PreToolUse hook decides before any of the command runs, so live
    git state is the state BEFORE the call, not at the moment each piece
    of it executes.

    Measured 2026-09-09, against the rewritten hook and equally against
    the one it replaced: ``git checkout main && git commit -m x``, issued
    from a feature branch, was allowed. The guard read the feature branch
    because that is what HEAD was when it was asked. An empty commit
    landed on main.

    So a guard cannot ask "what branch am I on"; it has to ask "what
    branch will this command be on when it commits". ``branch_target``
    carries the answer for the invocations that change it.
    """

    def test_plain_checkout_reports_its_target(self) -> None:
        out = scan_git_commands("git checkout main && git commit -m x", BASE)
        checkout = [i for i in out if i.subcommand == "checkout"][0]
        assert checkout.branch_target == "main"

    def test_checkout_dash_b_reports_the_new_branch(self) -> None:
        out = scan_git_commands("git checkout -b feature/x && git commit -m y", BASE)
        checkout = [i for i in out if i.subcommand == "checkout"][0]
        assert checkout.branch_target == "feature/x"

    def test_switch_reports_its_target(self) -> None:
        out = scan_git_commands("git switch main", BASE)
        assert out[0].branch_target == "main"

    def test_switch_dash_c_reports_the_new_branch(self) -> None:
        out = scan_git_commands("git switch -c feature/y", BASE)
        assert out[0].branch_target == "feature/y"

    def test_checkout_of_a_path_changes_no_branch(self) -> None:
        out = scan_git_commands("git checkout -- src/file.py", BASE)
        assert out[0].branch_target is None

    def test_checkout_with_quiet_flag_still_finds_the_branch(self) -> None:
        out = scan_git_commands("git checkout -q main", BASE)
        assert out[0].branch_target == "main"

    def test_checkout_dash_b_with_quiet_flag(self) -> None:
        out = scan_git_commands("git checkout -q -b feature/z", BASE)
        assert out[0].branch_target == "feature/z"

    def test_non_branch_subcommands_have_no_target(self) -> None:
        out = scan_git_commands("git commit -m x", BASE)
        assert out[0].branch_target is None

    def test_the_exact_sequence_that_slipped_through(self) -> None:
        # Reproduces the measured escape: checkout to main, then commit,
        # in one call, from a feature branch.
        out = scan_git_commands(
            "git checkout -q main && git merge --ff-only x -q && "
            "git commit --allow-empty -m 'oops'",
            BASE,
        )
        subs = [i.subcommand for i in out]
        assert subs == ["checkout", "merge", "commit"]
        assert out[0].branch_target == "main", (
            "the guard must be able to see that this call lands on main "
            "before the commit runs"
        )

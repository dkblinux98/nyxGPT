"""`--help` must not assert things the code contradicts (#4122 review round 1).

Two flags shipped help text that was false about the code in the very same
diff: `cloud deploy --ssh-timeout` still said "(default: 300)" after #4122
replaced that single number with a per-OS wait, and `ops install-extra`
offered a `cloud, rag` pair in which `rag` is not an extra at all (argparse's
own `choices` rejects it) while `verify`, which is one, went unmentioned.

Both are now built from the constants instead of restating them, and these
tests pin that: they read the *claims* back out of the generated help and
compare them to `SSH_TIMEOUT_BY_OS` and `INSTALLABLE_EXTRAS`. They
deliberately do not grep for today's sentence -- a wording pin would pass
happily the next time a target OS or an extra is added and the help is left
behind, which is the failure being guarded against.

`--help` is the surface an operator consults before running anything, so a
false claim here costs a real deploy: the Mac path's whole defect was needing
`--ssh-timeout 1800` because nothing said the default could not work.
"""

from __future__ import annotations

import re

import pytest

from nyxgpt import cloud_deploy, ops
from nyxgpt.cli import cli

pytestmark = pytest.mark.unit


def _help_for(capsys: pytest.CaptureFixture[str], argv: list[str]) -> str:
    """Return `argv`'s generated help, whitespace-normalized.

    argparse wraps to the terminal width, so a claim can land across a line
    break; normalizing lets one regex match it wherever it wrapped.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli([*argv, "--help"])
    assert excinfo.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def _option_help(text: str, flag: str) -> str:
    """The help text argparse printed for `flag`, up to the next option.

    Uses the *last* occurrence: the first is the usage line's `[--flag META]`,
    which carries no help at all.
    """
    rest = text[text.rindex(flag) + len(flag) :]
    following_option = re.search(r"\s--[a-z]", rest)
    return rest[: following_option.start()] if following_option else rest


def test_the_ssh_timeout_help_names_every_per_os_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The numbers the flag's help quotes must be exactly the ones it waits.

    Set equality both ways: a help that still says only "(default: 300)" is
    missing the Mac's 2700 and fails, and a help left quoting a number the
    table no longer holds fails too.
    """
    ssh_timeout = _option_help(_help_for(capsys, ["cloud", "deploy"]), "--ssh-timeout")

    quoted = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", ssh_timeout)}
    assert quoted == set(cloud_deploy.SSH_TIMEOUT_BY_OS.values())

    # And each number is attributed to the `--os` value it belongs to, so the
    # operator can tell which one applies to the target they are deploying.
    for family in cloud_deploy.SSH_TIMEOUT_BY_OS:
        assert family in ssh_timeout


def test_the_install_extra_help_offers_exactly_the_installable_extras(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Advertising an extra the command cannot install is the defect class
    `install-extra` exists to fix -- the retired `pip install nyxgpt[cloud]`
    remedy was unfollowable in exactly the same way."""
    ops_help = _help_for(capsys, ["ops"])

    # The name also appears inside argparse's `{install,status,...}` choice
    # lists, where it is followed by a comma or a brace; the entry that
    # carries help text is the one followed by whitespace.
    heading = re.search(r"install-extra\s", ops_help)
    assert heading is not None, f"`ops --help` lists no install-extra entry: {ops_help!r}"
    entry = ops_help[heading.end() :][:300]
    listed = re.search(r"\(([^)]*)\)", entry)
    assert listed is not None, f"install-extra's help offers no extras at all: {entry!r}"

    offered = {name.strip() for name in listed.group(1).split(",")}
    assert offered == set(ops.INSTALLABLE_EXTRAS)

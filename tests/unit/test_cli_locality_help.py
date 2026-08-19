"""The `ops` help surface must describe the locality behavior the code has (#3948).

`ops -h` is the only place most users learn what the product can do, and it
drifted from the code twice over in opposite directions: it advertised
`--terraform`/`--kubernetes` as "requires --local" (a requirement being removed
here), and it described `--cloud` as "not yet implemented" full stop, which
reads as "nyxGPT cannot deploy to a cloud target" -- false, since
`nyxgpt cloud infra apply` + `nyxgpt cloud deploy` are the shipped cloud path.

These tests deliberately do NOT grep for the new wording, which would pin
today's sentence and catch nothing when the behavior next changes. They read
the *claims* out of the generated help and check each one against what
`_resolve_locality` actually does, so re-adding the requirement in code
without saying so in the help (or the reverse) fails here.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from nyxgpt import ops
from nyxgpt.cli import cli

pytestmark = pytest.mark.unit

# "requires --local", "required with --terraform", "require --cloud" ... any
# claim in the help that some flag must be passed.
_REQUIREMENT_CLAIM = re.compile(r"requires?d?\s+(?:with\s+)?(--[a-z-]+)")

_LOCALITY_FLAGS = {"--local", "--cloud"}


def _help_for(capsys: pytest.CaptureFixture[str], argv: list[str]) -> str:
    """Return `argv`'s generated help, whitespace-normalized.

    argparse wraps help text to the terminal width, so a claim like "requires
    --local" can land across a line break; normalizing lets one regex match it
    wherever it wrapped.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli([*argv, "--help"])
    assert excinfo.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def _locality_flags_the_code_requires() -> set[str]:
    """Which locality flag(s) `_resolve_locality` actually forces the user to pass.

    A locality flag is required exactly when a run that passes none of them is
    rejected -- nothing else in the parser can make one mandatory, since they
    live in an optional mutually-exclusive group. `--cloud` can never be the
    required one: it is rejected even when passed.
    """
    if ops._resolve_locality(SimpleNamespace(local=False, cloud=False)) is None:
        return {"--local"}
    return set()


@pytest.mark.parametrize("argv", [["ops", "install"], ["up"], ["ops", "observability"]])
def test_help_claims_no_locality_requirement_the_code_does_not_enforce(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    claimed = {
        f for f in _REQUIREMENT_CLAIM.findall(_help_for(capsys, argv)) if f in _LOCALITY_FLAGS
    }
    assert claimed == _locality_flags_the_code_requires()


def test_install_help_points_at_the_commands_that_do_deploy_to_a_cloud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--cloud`'s help must scope the gap to the flag and name the real path.

    Asserting the shared constant appears -- rather than a copy of its words --
    is what stops the help and `_resolve_locality`'s rejection from drifting
    apart again: there is one string, used in both places.
    """
    help_text = _help_for(capsys, ["ops", "install"])
    assert " ".join(ops.CLOUD_DEPLOY_POINTER.split()) in help_text


def test_install_help_does_not_claim_local_is_the_only_locality(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The inverse falsehood: Terraform is not local-only -- `nyxgpt cloud infra` uses it."""
    assert "only locality" not in _help_for(capsys, ["ops", "install"])


def test_dev_help_leads_with_what_it_governs(capsys: pytest.CaptureFixture[str]) -> None:
    """`--dev`'s name gives no hint that it selects the *source* of the images.

    The owner could not tell from `ops -h` whether the working-tree image
    builds (#3834/#3835) existed at all. The flag's help must say what it
    governs before the three-deployment-mode detail, so it is findable by
    someone scanning the option list.
    """
    # Past the usage line (which lists every flag) into the option list, where
    # each flag is followed by its help.
    options = _help_for(capsys, ["ops", "install"]).split("options:", 1)[1]
    lead = options.split("--dev", 1)[1][:160]
    assert "SOURCE OF THE CODE" in lead
    assert "checkout" in lead and "published artifacts" in lead

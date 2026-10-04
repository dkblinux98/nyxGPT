"""A required suite must answer about the code, not about the machine (#3986).

Both halves of the submit gate that blocked #3986's head were green for the
agent that wrote them and red on the runner that gates them, and in neither
case had the code under test changed:

* **`acceptance-standard`.** `tests/test_acceptance_failure_handler.sh` case 5
  extracts the handlers' `if: failure()` alert step and runs it. It passed
  `NYXGPT_CONFIG_FILE` to every other extracted step and to that one it passed
  nothing, so `load_config` read `$HOME/.nyxGPT/config.ini`. A developer
  machine has one, and a `developer_auto_implement.yml` run exports the var --
  green. The gate's own runner has neither, so `load_config` `_die`d, and
  because sourcing `gh_project.sh` turns `set -e` on, the step exited 1 having
  written nothing. Red on `v3.0.0` from 2026-10-01, on every PR touching those
  files, for a reason no diff contained.

* **`missing-sources`.** `tests/test_retro_missing_sources.sh` restamps its
  inputs to now -- its own header says the suite is "about absent inputs
  only" -- and then restored the two it deletes by copying the **checked-in**
  dumps back, un-restamped. The build exits 3 on a stale source, so the suite
  was green for about three days after each data refresh and red after. Red on
  `v3.0.0` from 2026-10-03.

Both are the same defect in different clothes: a suite whose verdict comes
from the environment it happens to run in. That is worse than a plain red --
it is a gate that can neither be trusted when it passes nor diagnosed when it
fails, and the cost lands on whatever unrelated PR is in flight.

These guards pin the two properties that make those suites answer about the
code: the extracted-step runs name their whole environment, and nothing the
restamp cleaned is restored from the repository afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLER_SUITE = REPO_ROOT / "tests" / "test_acceptance_failure_handler.sh"
RETRO_SUITE = REPO_ROOT / "tests" / "test_retro_missing_sources.sh"


def test_the_extracted_alert_step_is_run_in_a_named_environment():
    """`env -i` plus an explicit config path: nothing ambient reaches it.

    Without this the suite's answer depends on whether the machine running it
    happens to have a nyxGPT config -- which is how it came to mean one thing
    locally and the opposite on the gate's runner.
    """
    text = HANDLER_SUITE.read_text(encoding="utf-8")
    assert "env -i \\" in text, (
        "tests/test_acceptance_failure_handler.sh no longer runs the extracted "
        "alert step under `env -i`. Inheriting the ambient environment is what "
        "made this suite pass locally and fail on the runner that gates it."
    )
    # The config path is named for both legs, and the alert step's own run
    # block is what has to carry it -- `_run_handler` already does.
    assert text.count('NYXGPT_CONFIG_FILE="$cfg"') == 1
    assert 'NYXGPT_CONFIG_FILE="$TMP/repo/config.ini"' in text


def test_the_loud_path_is_exercised_with_no_config_at_all():
    """The config step failing is the case the alert step exists for.

    `Write ephemeral config` is a step like any other; when it is the one that
    failed, the alert step inherits no `NYXGPT_CONFIG_FILE` and there is no
    `~/.nyxGPT/config.ini` on a runner. That is precisely when the owner must
    still be told their acceptance report was not filed, so one leg of case 5
    runs with a config path that does not exist and requires the comment and
    the DM anyway.
    """
    text = HANDLER_SUITE.read_text(encoding="utf-8")
    assert "for config in present absent; do" in text
    assert "no-such-config.ini" in text, (
        "the config-absent leg of case 5 is gone -- the loud path is only "
        "proven in the state where it was never at risk"
    )
    # And it has to assert where the writers aimed: REPO_OWNER/REPO_NAME can
    # then only come from GITHUB_REPOSITORY, and a comment posted to `/`
    # reaches nobody.
    assert "REPO|stub-owner/stub-repo" in text


def test_the_retro_suite_never_restores_the_checked_in_dumps():
    """Restoring from the repo re-imports the staleness the restamp removed.

    The suite is about an absent input. Whether the checked-in `spend.json` is
    two days old or ten is a different test's question
    (`test_retro_dashboard_stamps.sh`), and letting it decide this one makes
    the verdict a function of the wall clock.
    """
    text = RETRO_SUITE.read_text(encoding="utf-8")
    assert (
        'cp "$WORK/landed/spend.json" "$WORK/landed/churn.json" "$WORK/data/"' in text
    ), "the landed-dump restore no longer comes from the restamped stash"
    restores_from_repo = [
        line
        for line in text.splitlines()
        if line.strip().startswith("cp ") and '"$RETRO/data/' in line
    ]
    assert not restores_from_repo, (
        "tests/test_retro_missing_sources.sh copies a checked-in dump into its "
        "work dir after the restamp:\n  "
        + "\n  ".join(restores_from_repo)
        + "\nThat is the clock dependency that made `missing-sources` red on "
        "v3.0.0 three days after every data refresh. Restore from "
        "$WORK/landed/, which the restamp has already touched."
    )

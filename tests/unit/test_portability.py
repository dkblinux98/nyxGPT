"""Tests for the repo-less portability matrix (P6-16, #3516).

Two kinds of assertion live here, and the distinction matters:

* Assertions about the *checker* -- given a row with a `git clone` in it, or
  a raw `kubectl`, or evidence that doesn't exist, does `check_target`
  actually fail? These use synthetic rows, so they keep testing the checker
  even after every real row is green.
* Assertions about the *real matrix* -- every shipped row satisfies the
  mechanical invariants, cites evidence that exists, and covers exactly the
  five targets CLAUDE.md's Repo-less Portability requirement names.

Deliberately NOT asserted: that every row is acceptance-ready. Two rows have
open gaps today (Compose and Kubernetes both build their core images from a
checkout), and a test that demanded otherwise would either fail on merge or
force the matrix to lie. `test_gaps_are_reported_not_hidden` pins the honest
behaviour instead: while a gap is recorded, `acceptance_ready` is false and
`--strict` exits non-zero.
"""

import argparse
import json
from pathlib import Path

import pytest

from nyxgpt import portability

pytestmark = pytest.mark.unit


def _target(**overrides) -> portability.Target:
    """A minimal, valid matrix row, with `overrides` applied."""
    defaults = {
        "key": "example",
        "name": "Example target",
        "artifact": "PyPI wheel (nyxgpt)",
        "install": ("pip install nyxgpt",),
        "operate": ("nyxgpt up",),
        "teardown": "nyxgpt down",
        "status": "ci-verified",
        "evidence": ("pyproject.toml",),
        "notes": "",
        "gaps": (),
    }
    defaults.update(overrides)
    return portability.Target(**defaults)  # type: ignore[arg-type]


def _check(target: portability.Target, name: str, root: Path | None = None) -> dict:
    report = portability.check_target(target, root)
    return next(check for check in report["checks"] if check["check"] == name)


# --- the checker: repo-less -------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git clone https://github.com/dkblinux98/nyxGPT",
        "git clone git@github.com:dkblinux98/nyxGPT.git",
        "pip install https://github.com/dkblinux98/nyxGPT.git",
        "curl -L https://github.com/dkblinux98/nyxGPT/archive/refs/tags/v3.0.0.tar.gz | tar xz",
        "git pull",
    ],
)
def test_repo_less_check_rejects_fetching_source(command):
    check = _check(_target(install=(command,)), "repo_less")

    assert not check["passed"]
    assert command in check["detail"]


def test_repo_less_check_passes_for_published_artifacts():
    target = _target(install=("brew tap dkblinux98/nyxgpt", "brew install nyxgpt-api nyxgpt-web"))

    assert _check(target, "repo_less")["passed"]


def test_repo_less_check_looks_at_operate_and_teardown_too():
    # A row could plausibly install from PyPI and then tell the operator to
    # clone for the manifests -- which is exactly the Kubernetes gap.
    target = _target(operate=("nyxgpt up", "git clone https://github.com/dkblinux98/nyxGPT"))

    assert not _check(target, "repo_less")["passed"]


# --- the checker: wrapped ---------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "docker compose up -d",
        "docker-compose up -d",
        "kubectl apply -k k8s/",
        "terraform apply",
        "helm install nyxgpt ./chart",
    ],
)
def test_wrapped_check_rejects_raw_orchestrators(command):
    check = _check(_target(operate=(command,)), "wrapped")

    assert not check["passed"]
    assert command in check["detail"]


@pytest.mark.parametrize(
    "command",
    [
        "sudo docker compose up -d",
        "sudo -E kubectl apply -k k8s/",
        "DOCKER_HOST=ssh://host docker compose up -d",
        "env TF_IN_AUTOMATION=1 terraform apply",
    ],
)
def test_wrapped_check_sees_through_sudo_and_env_prefixes(command):
    """`sudo docker …` is still a raw `docker` the operator shouldn't type."""
    check = _check(_target(operate=(command,)), "wrapped")

    assert not check["passed"]
    assert command in check["detail"]


def test_wrapped_check_still_accepts_a_prefixed_wrapper_command():
    assert _check(_target(operate=("sudo nyxgpt ops install",)), "wrapped")["passed"]


def test_first_word_of_an_empty_command_is_empty():
    assert portability._first_word("   ") == ""


def test_wrapped_check_rejects_unwrapped_teardown():
    check = _check(_target(teardown="launchctl unload nyxgpt-api"), "wrapped")

    assert not check["passed"]
    assert "not a `nyxgpt` command" in check["detail"]


def test_wrapped_check_allows_package_managers_for_install_only():
    # `pip install nyxgpt` is how the artifact arrives; pip is not an
    # orchestrator. The same tool in an operate step is not accepted.
    assert _check(_target(install=("pipx install nyxgpt",)), "wrapped")["passed"]
    assert not _check(_target(operate=("pip install nyxgpt",)), "wrapped")["passed"]


# --- the checker: evidence --------------------------------------------


def test_evidence_check_fails_on_a_path_that_does_not_exist(tmp_path):
    check = _check(_target(evidence=("docs/nope.md",)), "evidence", root=tmp_path)

    assert not check["passed"]
    assert "docs/nope.md" in check["detail"]


def test_evidence_check_is_skipped_without_a_checkout(monkeypatch):
    """An installed wheel has no repo beside it -- the normal, desired state."""
    monkeypatch.setattr(portability, "checkout_root", lambda: None)
    check = _check(_target(evidence=("docs/nope.md",)), "evidence")

    assert check["skipped"]
    assert check["passed"]


def test_checkout_root_is_none_when_the_tree_is_not_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        portability, "__file__", str(tmp_path / "pkg" / "nyxgpt" / "portability.py")
    )

    assert portability.checkout_root() is None


def test_checkout_root_finds_this_repo():
    root = portability.checkout_root()

    assert root is not None
    assert (root / "pyproject.toml").is_file()


# --- the real matrix --------------------------------------------------


def test_matrix_covers_exactly_the_required_targets():
    """CLAUDE.md's Repo-less Portability requirement names these five; Windows is out of scope."""
    assert [t.key for t in portability.TARGETS] == [
        "macos-native",
        "linux-native",
        "docker-compose",
        "kubernetes",
        "aws-ec2",
    ]
    assert not portability.check_matrix()["summary"]["windows_in_scope"]
    assert not any("windows" in target.name.lower() for target in portability.TARGETS)


@pytest.mark.parametrize("target", portability.TARGETS, ids=lambda t: t.key)
def test_every_shipped_row_satisfies_the_mechanical_invariants(target):
    report = portability.check_target(target)

    assert report["invariants_passed"], report["checks"]


@pytest.mark.parametrize("target", portability.TARGETS, ids=lambda t: t.key)
def test_every_shipped_row_cites_evidence_that_exists(target):
    root = portability.checkout_root()
    assert root is not None, "these tests run from a checkout"

    assert portability._missing_evidence(target, root) == []


@pytest.mark.parametrize("target", portability.TARGETS, ids=lambda t: t.key)
def test_every_row_has_a_recognized_status_and_matching_gaps(target):
    assert target.status in ("ci-verified", "acceptance", "gap")
    # "gap" and "has gaps recorded" must agree, or the table's status column
    # and its gap list would tell an operator two different things.
    assert bool(target.gaps) == (target.status == "gap")


def test_gaps_are_reported_not_hidden():
    report = portability.check_matrix()
    gapped = [t for t in report["targets"] if t["gaps"]]

    assert gapped, "if every gap has closed, delete this test and assert acceptance_ready instead"
    for target in gapped:
        assert not target["acceptance_ready"]
        # A gap has to say what is missing concretely enough to act on.
        for gap in target["gaps"]:
            assert len(gap) > 40
    assert not report["acceptance_ready"]
    assert report["summary"]["open_gaps"] == sum(len(t["gaps"]) for t in report["targets"])


def test_summary_counts_match_the_rows():
    report = portability.check_matrix()

    assert report["summary"]["total"] == len(portability.TARGETS)
    assert report["summary"]["acceptance_ready"] == sum(
        1 for t in report["targets"] if t["acceptance_ready"]
    )
    assert report["summary"]["invariants_failed"] == 0


# --- the acceptance sequence ------------------------------------------


def test_acceptance_sequence_is_a_clean_machine_run_end_to_end():
    steps = [step["step"] for step in portability.ACCEPTANCE_SEQUENCE]

    # Install first, teardown last -- an acceptance that never tears down
    # leaves billed AWS resources behind (P6-16 AC: "teardown verified").
    assert steps[0] == "install"
    assert steps[-1] == "teardown"
    assert {"deploy", "verify", "self-heal"} <= set(steps)


def test_acceptance_sequence_commands_are_wrapped_and_repo_less():
    commands = tuple(step["command"] for step in portability.ACCEPTANCE_SEQUENCE)

    assert portability._source_fetch_findings(commands) == []
    for command in commands:
        tool = portability._first_word(command)
        assert tool == "nyxgpt" or tool in portability._INSTALL_TOOLS, command


def test_acceptance_sequence_verifies_the_deployment_it_just_made():
    """`cloud smoke` on its own would deploy a *second* stack and prove nothing."""
    smoke = next(s for s in portability.ACCEPTANCE_SEQUENCE if s["step"] == "verify")

    assert "--skip-deploy" in smoke["command"]


def test_every_step_states_what_to_expect():
    for step in portability.ACCEPTANCE_SEQUENCE:
        assert step["expect"].strip()


# --- the CLI entry point ----------------------------------------------


def test_portability_command_reports_and_succeeds(capsys):
    code = portability.portability(argparse.Namespace(strict=False, json=False))
    out = capsys.readouterr().out

    assert code == 0
    for target in portability.TARGETS:
        assert target.name in out
    assert "docs/portability-matrix.md" in out
    assert "nyxgpt cloud destroy --yes" in out


def test_portability_command_strict_fails_while_a_gap_is_open(capsys):
    code = portability.portability(argparse.Namespace(strict=True, json=False))
    capsys.readouterr()

    assert code == 1


def test_portability_command_json_is_machine_readable(capsys):
    code = portability.portability(argparse.Namespace(strict=False, json=True))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["summary"]["total"] == len(portability.TARGETS)
    assert payload["commands"]["strict"] == "nyxgpt ops portability --strict"
    assert len(payload["acceptance_sequence"]) == len(portability.ACCEPTANCE_SEQUENCE)


def test_portability_command_fails_on_an_invariant_failure(capsys, monkeypatch):
    monkeypatch.setattr(
        portability, "TARGETS", (_target(operate=("kubectl apply -k k8s/",)),), raising=True
    )

    code = portability.portability(argparse.Namespace(strict=False, json=False))
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL" in out

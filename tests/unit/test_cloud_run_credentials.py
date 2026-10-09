"""Every actor in a run gets the run's credential choice -- Terraform included (#4181).

`cloud_identity` has been the one resolver since #4186, but it only ever
answered for callers holding a parsed namespace. `terraform` holds none: it is
started by `cloud_infra._run_terraform`, five frames below any `args`, and it
resolves credentials from its own provider block plus the ambient environment.
A stored tfvars file an *earlier* run rendered is part of that provider block.

So in the owner's 2026-10-09 acceptance round, `--profile nyxgpt` reached every
boto3 call in the command and none of the Terraform ones: the release-schedule
cleanup read `mac-release.tfvars` as rendered on 2026-10-04 -- `aws_region`,
no `aws_profile` line at all -- fell through to the default credential chain,
and `terraform destroy` failed AccessDenied on `nyxgpt-tf-mac-release` while
the same command's API calls authenticated correctly.

Two channels close it, and both are tested here because each covers a case the
other cannot: the environment covers a root whose provider has no
`aws_profile` to read, and the re-stamp covers the case where a *stale* one
would outrank the environment (`profile = var.aws_profile != "" ? var.aws_profile
: null`).
"""

from __future__ import annotations

import argparse

import pytest

from nyxgpt import cloud_identity, cloud_infra, cloud_mac


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the modules read or write at a temp dir."""
    cloud_identity.reset_prompt_cache()
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_infra, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    monkeypatch.setattr(cloud_infra, "TERRAFORM_DIR", cloud_dir / "terraform")
    monkeypatch.setattr(cloud_mac, "MAC_TFVARS_FILE", cloud_dir / "mac.tfvars")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFVARS_FILE", cloud_dir / "mac-release.tfvars")
    monkeypatch.setattr(cloud_mac, "MAC_RELEASE_TFSTATE_FILE", cloud_dir / "mac-release.tfstate")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    yield cloud_dir
    cloud_identity.reset_prompt_cache()


# --- The resolver answers with no args at all ------------------------------


def test_the_flags_this_run_was_given_are_readable_with_no_namespace_to_hand():
    """What makes the Terraform channel possible at all.

    `_run_terraform` cannot be handed `args`; without a bound namespace it
    resolved from `infra.json` down and never saw `--profile`.
    """
    cloud_identity.bind_run_args(argparse.Namespace(profile="nyxgpt", region="us-east-1"))

    resolved = cloud_identity.account_default()

    assert resolved.profile == "nyxgpt"
    assert resolved.region == "us-east-1"
    assert resolved.source == cloud_identity.SOURCE_FLAG


def test_passing_a_namespace_binds_it_so_the_api_path_needs_no_second_call():
    """The admin API synthesizes a namespace rather than parsing argv.

    Binding inside `account_default` means every handler reaches the same
    resolution without each one having to remember an extra call -- which is
    the shape of omission that left one of two classifier copies unfixed for
    seven weeks (#4179).
    """
    cloud_identity.account_default(argparse.Namespace(profile="nyxgpt", region="eu-west-1"))

    assert cloud_identity.account_default().profile == "nyxgpt"


def test_the_bound_flags_are_forgotten_between_runs():
    cloud_identity.bind_run_args(argparse.Namespace(profile="nyxgpt", region="us-east-1"))

    cloud_identity.reset_prompt_cache()

    assert cloud_identity.run_args() is None
    assert cloud_identity.account_default().profile == ""


# --- Channel 1: Terraform's environment ------------------------------------


def test_the_credential_environment_names_the_runs_profile_and_region():
    cloud_identity.bind_run_args(argparse.Namespace(profile="nyxgpt", region="us-east-1"))

    env = cloud_identity.credential_env()

    assert env["AWS_PROFILE"] == "nyxgpt"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"


def test_terraform_is_started_with_the_runs_profile(monkeypatch, _isolated_cloud_home):
    seen: dict[str, str] = {}

    def _fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cloud_infra, "ensure_terraform_binary", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(cloud_infra.subprocess, "run", _fake_run)
    cloud_identity.bind_run_args(argparse.Namespace(profile="nyxgpt", region="us-east-1"))

    cloud_infra.run_terraform(["destroy", "-auto-approve"], capture=True)

    assert seen["AWS_PROFILE"] == "nyxgpt"
    assert seen["AWS_REGION"] == "us-east-1"


def test_a_run_that_resolved_no_profile_clears_an_inherited_one(monkeypatch, _isolated_cloud_home):
    """ "No named profile" is an answer, and must outrank the environment.

    The operator typing `-` at the prompt, or nothing naming one, resolves to
    boto3's default chain. Leaving an inherited `AWS_PROFILE` in place would
    let the environment decide the account after the resolver had decided it.
    """
    seen: dict[str, str] = {}

    def _fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("AWS_PROFILE", "inherited-from-the-shell")
    monkeypatch.setattr(cloud_infra, "ensure_terraform_binary", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(cloud_infra.subprocess, "run", _fake_run)
    # Explicitly cleared, the way the prompt's `-` does.
    cloud_identity.bind_run_args(argparse.Namespace(profile="", region="us-east-1"))
    monkeypatch.setattr(
        cloud_identity,
        "account_default",
        lambda *a, **k: cloud_identity.AccountChoice(
            profile="", region="us-east-1", account_id="", source=cloud_identity.SOURCE_PROMPT
        ),
    )

    cloud_infra.run_terraform(["plan"], capture=True)

    assert "AWS_PROFILE" not in seen
    # And nothing else in the environment was dropped on the way.
    assert seen["AWS_REGION"] == "us-east-1"


def test_a_caller_with_its_own_choice_still_outranks_the_default(monkeypatch, _isolated_cloud_home):
    """One documented override, for `cloud_state`'s remote backend.

    Its bucket and lock table live wherever `cloud state bootstrap` put them,
    which is not necessarily where this run is acting -- so the specific choice
    wins over the general one.
    """
    seen: dict[str, str] = {}

    def _fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cloud_infra, "ensure_terraform_binary", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(cloud_infra.subprocess, "run", _fake_run)
    cloud_identity.bind_run_args(argparse.Namespace(profile="nyxgpt", region="us-east-1"))

    cloud_infra.run_terraform(["state", "pull"], capture=True, extra_env={"AWS_PROFILE": "backend"})

    assert seen["AWS_PROFILE"] == "backend"


# --- Channel 2: a stored tfvars is re-stamped before it is reused ----------


def test_a_stored_tfvars_with_no_profile_line_gets_this_runs(_isolated_cloud_home):
    """The owner's case, verbatim.

    `mac-release.tfvars` as rendered on 2026-10-04: a region and no
    `aws_profile`. The provider's `var.aws_profile != "" ? ... : null` then
    falls through to the default chain, which is account 551292530955.
    """
    path = cloud_mac.MAC_RELEASE_TFVARS_FILE
    path.write_text('aws_region = "us-east-1"\nhost_id = "h-06c438d25077be888"\n', encoding="utf-8")

    cloud_mac._restamp_tfvars(path, region="us-east-1", profile="nyxgpt")

    values = cloud_mac._read_tfvars(path)
    assert values["aws_profile"] == "nyxgpt"
    # And everything the destroy actually operates on is untouched: those values
    # are what created the resources, and re-deriving them is what would fail on
    # an availability zone no record captured.
    assert values["host_id"] == "h-06c438d25077be888"


def test_a_stored_tfvars_naming_another_account_is_overwritten(_isolated_cloud_home):
    """A stale `aws_profile` outranks the environment, so the env alone is not enough."""
    path = cloud_mac.MAC_RELEASE_TFVARS_FILE
    path.write_text(
        'aws_region = "us-east-1"\naws_profile = "an-older-runs-account"\nhost_id = "h-0abc"\n',
        encoding="utf-8",
    )

    cloud_mac._restamp_tfvars(path, region="us-east-1", profile="nyxgpt")

    assert cloud_mac._read_tfvars(path)["aws_profile"] == "nyxgpt"


def test_restamping_to_no_profile_removes_the_line(_isolated_cloud_home):
    """`""` is how the provider is told to use the ambient chain.

    `_write_tfvars` drops an empty value, and the line's absence is what makes
    the provider's conditional choose `null`. A run that resolved no named
    profile must not inherit an earlier run's.
    """
    path = cloud_mac.MAC_RELEASE_TFVARS_FILE
    path.write_text('aws_region = "us-east-1"\naws_profile = "nyxgpt"\n', encoding="utf-8")

    cloud_mac._restamp_tfvars(path, region="us-east-1", profile="")

    assert "aws_profile" not in path.read_text(encoding="utf-8")


def test_restamping_a_file_that_does_not_exist_is_a_no_op(_isolated_cloud_home):
    """The caller renders a fresh one in that case, with the current choice."""
    cloud_mac._restamp_tfvars(cloud_mac.MAC_RELEASE_TFVARS_FILE, region="us-east-1", profile="x")

    assert not cloud_mac.MAC_RELEASE_TFVARS_FILE.exists()


def test_numbers_and_booleans_survive_a_round_trip(_isolated_cloud_home):
    """`root_volume_size = 200` must not come back as the string "200".

    A quoted number is a type error Terraform reports at plan time, on a
    teardown -- the worst moment to discover one.
    """
    path = cloud_mac.MAC_TFVARS_FILE
    cloud_mac._write_tfvars(
        path, {"aws_region": "us-east-1", "root_volume_size": 200, "enabled": True}
    )

    cloud_mac._restamp_tfvars(path, region="us-east-1", profile="nyxgpt")

    values = cloud_mac._read_tfvars(path)
    assert values["root_volume_size"] == 200
    assert values["enabled"] is True


def test_the_release_stack_teardown_restamps_before_terraform_reads_it(
    monkeypatch, _isolated_cloud_home
):
    """The end-to-end shape of finding 2, at the seam below the CLI."""
    cloud_mac.MAC_RELEASE_TFSTATE_FILE.write_text("{}", encoding="utf-8")
    cloud_mac.MAC_RELEASE_TFVARS_FILE.write_text(
        'aws_region = "us-east-1"\nhost_id = "h-0abc"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cloud_infra, "sync_terraform_config", lambda: None)
    monkeypatch.setattr(cloud_mac, "_init", lambda *a, **k: None)
    monkeypatch.setattr(cloud_infra, "run_terraform", lambda *a, **k: None)

    assert cloud_mac.destroy_release_stack(region="us-east-1", profile="nyxgpt") is True

    assert cloud_mac._read_tfvars(cloud_mac.MAC_RELEASE_TFVARS_FILE)["aws_profile"] == "nyxgpt"

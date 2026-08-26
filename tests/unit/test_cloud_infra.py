"""Unit tests for `nyxgpt cloud infra` (P6-8, #3509).

Terraform itself is never executed here -- `_run_terraform` is replaced with a
recorder so the tests assert on the command line the wrapper builds, the files
it generates, and the state handoff it performs. The Terraform configuration's
own behaviour is covered by the plan-level suite in
`terraform/aws/tests/plan.tftest.hcl` (run by
.github/workflows/terraform-aws-validate.yml and `nyxgpt cloud infra test`).
"""

import argparse
import json
import re
import shutil
import subprocess

import pytest

from nyxgpt import cloud, cloud_infra
from nyxgpt.cloud import CloudCommandError

REPO_TERRAFORM_AWS = "terraform/aws"


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the module writes to at a temp dir, including the shared state file."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    monkeypatch.setattr(cloud_infra, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_infra, "TERRAFORM_DIR", cloud_dir / "terraform")
    monkeypatch.setattr(cloud_infra, "TFVARS_FILE", cloud_dir / "terraform.tfvars")
    monkeypatch.setattr(cloud_infra, "TFSTATE_FILE", cloud_dir / "terraform.tfstate")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    monkeypatch.setattr(cloud_infra, "PLAN_FILE", cloud_dir / "tfplan")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    # config.ini's [cloud] section is a *fallback* source for region/profile;
    # a developer's real one must not leak into these assertions.
    monkeypatch.setattr(
        cloud_infra, "_configured_cloud_reference", lambda: {"profile": "", "region": ""}
    )
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return cloud_dir


@pytest.fixture
def terraform_calls(monkeypatch):
    """Record `_run_terraform` invocations instead of shelling out to Terraform."""
    calls: list[list[str]] = []

    def fake_run(arguments, *, capture=False, extra_env=None):
        calls.append(list(arguments))
        return subprocess.CompletedProcess(["terraform", *arguments], 0, stdout="", stderr="")

    monkeypatch.setattr(cloud_infra, "_run_terraform", fake_run)
    return calls


def _args(**overrides) -> argparse.Namespace:
    base = {
        "region": None,
        "profile": None,
        "owner_ip": "198.51.100.7",
        "ssh_public_key": None,
        "ssh_key_name": "existing-pair",
        "instance_type": None,
        "root_volume_size": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- Packaged configuration ---------------------------------------------


def test_packaged_terraform_dir_resolves_to_the_aws_config():
    """The wrapper must find the AWS config through the package, not a repo-relative path."""
    packaged = cloud_infra.packaged_terraform_dir()
    assert packaged.is_dir(), f"{packaged} is missing -- is the resources/terraform symlink gone?"
    assert (packaged / "main.tf").is_file()
    assert (packaged / "modules" / "security" / "main.tf").is_file()


def test_sync_terraform_config_materializes_the_config_and_is_idempotent():
    first = cloud_infra.sync_terraform_config()
    assert (first / "main.tf").is_file()
    assert (first / "modules" / "compute" / "main.tf").is_file()

    # A second sync must not fail on the already-populated directory, and must
    # refresh a locally-modified source file back to the packaged content.
    (first / "main.tf").write_text("# tampered\n", encoding="utf-8")
    second = cloud_infra.sync_terraform_config()
    assert second == first
    assert "tampered" not in (first / "main.tf").read_text(encoding="utf-8")


def test_sync_terraform_config_reports_a_broken_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_infra, "packaged_terraform_dir", lambda: tmp_path / "nope")
    with pytest.raises(CloudCommandError, match="installation is incomplete"):
        cloud_infra.sync_terraform_config()


# --- Settings resolution -------------------------------------------------


def test_resolve_settings_detects_the_owner_ip_when_not_given(monkeypatch):
    monkeypatch.setattr(cloud_infra, "detect_current_public_ip", lambda: "203.0.113.5")
    settings = cloud_infra.resolve_settings(_args(owner_ip=None))
    assert settings.owner_ip_cidr == "203.0.113.5/32"


def test_resolve_settings_refuses_a_world_open_owner_ip():
    """The wrapper refuses 0.0.0.0/0 before Terraform ever sees it."""
    with pytest.raises(CloudCommandError, match="0.0.0.0/0"):
        cloud_infra.resolve_settings(_args(owner_ip="0.0.0.0/0"))


def test_resolve_settings_requires_an_ssh_key():
    with pytest.raises(CloudCommandError, match="No SSH key configured"):
        cloud_infra.resolve_settings(_args(ssh_key_name=None, ssh_public_key=None))


def test_resolve_settings_refuses_both_ssh_key_inputs(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAAC3Nza test@host\n", encoding="utf-8")
    with pytest.raises(CloudCommandError, match="not both"):
        cloud_infra.resolve_settings(_args(ssh_public_key=str(pub)))


def test_resolve_settings_reads_public_key_material_from_a_file(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAAC3Nza test@host\n", encoding="utf-8")
    settings = cloud_infra.resolve_settings(_args(ssh_key_name=None, ssh_public_key=str(pub)))
    assert settings.ssh_public_key == "ssh-ed25519 AAAAC3Nza test@host"
    assert settings.ssh_key_name == ""


def test_resolve_settings_refuses_a_private_key(tmp_path):
    key = tmp_path / "id_ed25519"
    # Assembled rather than written out: the literal header is what
    # pre-commit's detect-private-key hook scans for, and it cannot tell this
    # fixture from a real key leaking into the repo. Splitting it keeps the
    # hook meaningful for actual keys while still handing `_read_ssh_public_key`
    # the exact string it refuses on.
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    key.write_text(f"{header}\nsecret\n", encoding="utf-8")
    with pytest.raises(CloudCommandError, match="PRIVATE key"):
        cloud_infra.resolve_settings(_args(ssh_key_name=None, ssh_public_key=str(key)))


def test_resolve_settings_remembers_previous_answers():
    saved = cloud_infra.resolve_settings(
        _args(region="eu-west-2", instance_type="m6i.xlarge", root_volume_size=250)
    )
    cloud_infra.save_settings(saved)

    # A later run passing only the owner IP inherits everything else.
    later = cloud_infra.resolve_settings(_args(owner_ip="198.51.100.8", ssh_key_name=None))
    assert later.aws_region == "eu-west-2"
    assert later.instance_type == "m6i.xlarge"
    assert later.root_volume_size == 250
    assert later.ssh_key_name == "existing-pair"
    assert later.owner_ip_cidr == "198.51.100.8/32"


# --- Instance sizing (#3992) ---------------------------------------------
#
# `m5.large` (2 vCPU / 8 GiB) was the original default and could not run the
# stack: the box sat at 7.2 of 7.6 GiB minutes after boot with no swap and
# froze interactively under ordinary use. `m5.xlarge` (4 vCPU / 16 GiB) is the
# shipped size, and these pin both halves of that change -- that the default
# really is the bigger one, and that raising it cannot resize a substrate
# someone already provisioned.


def test_the_default_instance_type_is_the_size_the_stack_actually_fits_on():
    """No flag and nothing remembered must land on the decision record's size."""
    assert cloud_infra.DEFAULT_INSTANCE_TYPE == "m5.xlarge"
    assert cloud_infra.resolve_settings(_args()).instance_type == "m5.xlarge"


def test_a_remembered_instance_type_is_never_overridden_by_a_raised_default():
    """The resize hazard: an existing deployment keeps the size it is running.

    Every plan/apply persists the resolved settings, so a substrate
    provisioned before #3992 has `m5.large` in its `infra.json`. If the raised
    default won here, the operator's next `nyxgpt cloud deploy` would stop,
    resize and restart a live instance they believed an upgrade had left
    alone.
    """
    cloud_infra.CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    cloud_infra.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "aws_region": "us-east-1",
                "owner_ip_cidr": "198.51.100.7/32",
                "ssh_key_name": "existing-pair",
                "instance_type": "m5.large",
                "root_volume_size": 100,
            }
        ),
        encoding="utf-8",
    )

    settings = cloud_infra.resolve_settings(_args(ssh_key_name=None))

    assert settings.instance_type == "m5.large"
    # …and Terraform is handed that size, not the new default.
    assert 'instance_type = "m5.large"' in cloud_infra.render_tfvars(settings)


def test_teardown_settings_also_keep_the_remembered_instance_type():
    """`destroy` reads `saved_settings()`, which has its own fallback to patch."""
    cloud_infra.CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    cloud_infra.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "aws_region": "us-east-1",
                "owner_ip_cidr": "198.51.100.7/32",
                "ssh_key_name": "existing-pair",
                "instance_type": "m5.large",
            }
        ),
        encoding="utf-8",
    )

    assert cloud_infra.saved_settings().instance_type == "m5.large"


def test_an_explicit_flag_still_beats_the_remembered_size():
    """Resizing stays available -- it just has to be asked for."""
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args(instance_type="m5.large")))

    later = cloud_infra.resolve_settings(_args(instance_type="m5.xlarge", ssh_key_name=None))

    assert later.instance_type == "m5.xlarge"


def test_the_terraform_default_matches_the_python_default():
    """Two defaults for one value drift; this fails the moment they disagree.

    `render_tfvars` only omits a variable when its value is empty, so the
    Terraform default is what a hand-run or a partially-pinned tfvars falls
    back to -- it has to name the same size the wrapper does.
    """
    variables = (cloud_infra.packaged_terraform_dir() / "variables.tf").read_text(encoding="utf-8")
    block = re.search(r'variable "instance_type" \{(.*?)\n\}', variables, re.DOTALL)
    assert block, "the instance_type variable is gone from the packaged configuration"
    declared = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
    assert declared and declared.group(1) == cloud_infra.DEFAULT_INSTANCE_TYPE


def test_the_cli_help_names_the_shipped_default(capsys):
    """The help text is where an operator reads the default; it must not lie."""
    from nyxgpt.cli import cli

    with pytest.raises(SystemExit):
        cli(["cloud", "infra", "apply", "--help"])

    help_text = capsys.readouterr().out
    assert cloud_infra.DEFAULT_INSTANCE_TYPE in help_text
    assert "m5.large" not in help_text


def test_resolve_settings_falls_back_to_configured_region(monkeypatch):
    monkeypatch.setattr(
        cloud_infra,
        "_configured_cloud_reference",
        lambda: {"profile": "nyxgpt", "region": "ap-southeast-2"},
    )
    settings = cloud_infra.resolve_settings(_args())
    assert settings.aws_region == "ap-southeast-2"
    assert settings.aws_profile == "nyxgpt"


def test_settings_file_is_owner_only(_isolated_cloud_home):
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args()))
    assert cloud_infra.SETTINGS_FILE.stat().st_mode & 0o777 == 0o600


# --- tfvars rendering ----------------------------------------------------


def test_render_tfvars_quotes_strings_and_omits_empty_values():
    settings = cloud_infra.InfraSettings(
        aws_region="us-east-1",
        owner_ip_cidr="198.51.100.7/32",
        ssh_key_name="existing-pair",
    )
    rendered = cloud_infra.render_tfvars(settings)
    assert 'aws_region = "us-east-1"' in rendered
    assert 'owner_ip_cidr = "198.51.100.7/32"' in rendered
    assert "root_volume_size = 100" in rendered
    # Unset values must be absent rather than emitted as "" -- an empty
    # ssh_public_key alongside ssh_key_name would trip the config's
    # "exactly one of" validation.
    assert "ssh_public_key" not in rendered
    assert "aws_profile" not in rendered


def test_write_tfvars_is_owner_only():
    cloud_infra.write_tfvars(cloud_infra.resolve_settings(_args()))
    assert cloud_infra.TFVARS_FILE.stat().st_mode & 0o777 == 0o600


def test_hcl_value_escapes_embedded_quotes():
    assert cloud_infra._hcl_value('say "hi"') == '"say \\"hi\\""'
    assert cloud_infra._hcl_value(True) == "true"
    assert cloud_infra._hcl_value(["a", "b"]) == '["a", "b"]'


# --- plan / apply / destroy ----------------------------------------------


def test_plan_creates_nothing_and_saves_a_plan_file(terraform_calls):
    result = cloud_infra.plan_infra(_args())

    commands = [call[0] for call in terraform_calls]
    assert commands == ["init", "plan"]
    assert "apply" not in commands
    plan_call = terraform_calls[1]
    assert f"-out={cloud_infra.PLAN_FILE}" in plan_call
    assert f"-var-file={cloud_infra.TFVARS_FILE}" in plan_call
    assert result["action"] == "plan"


def test_init_pins_state_outside_the_synced_config(terraform_calls):
    cloud_infra.plan_infra(_args())
    init_call = terraform_calls[0]
    assert f"-backend-config=path={cloud_infra.TFSTATE_FILE}" in init_call
    # State must not live inside the directory sync_terraform_config overwrites.
    assert cloud_infra.TERRAFORM_DIR not in cloud_infra.TFSTATE_FILE.parents


def test_init_only_upgrades_providers_when_the_config_changed(terraform_calls):
    """`-upgrade` re-resolves the provider registry; that belongs on real changes only."""
    cloud_infra.plan_infra(_args())
    assert "-upgrade" in terraform_calls[0], "first init must resolve providers"

    terraform_calls.clear()
    cloud_infra.plan_infra(_args())
    assert "-upgrade" not in terraform_calls[0], "unchanged config must not re-resolve"
    assert f"-backend-config=path={cloud_infra.TFSTATE_FILE}" in terraform_calls[0]

    # An nyxGPT upgrade syncs a different configuration (here, one more `.tf`
    # source than before) -- provider resolution has to run again.
    (cloud_infra.TERRAFORM_DIR / "zz_upgraded.tf").write_text("# new source\n", encoding="utf-8")
    terraform_calls.clear()
    cloud_infra.plan_infra(_args())
    assert "-upgrade" in terraform_calls[0]


def test_init_upgrades_again_when_the_plugin_cache_is_removed(terraform_calls):
    cloud_infra.plan_infra(_args())
    shutil.rmtree(cloud_infra.TERRAFORM_DIR / ".terraform")

    terraform_calls.clear()
    cloud_infra.plan_infra(_args())
    assert "-upgrade" in terraform_calls[0]


def test_init_does_not_record_the_fingerprint_when_terraform_fails(monkeypatch):
    """A failed init must not leave a stamp that suppresses the next `-upgrade`."""
    cloud_infra.sync_terraform_config()

    def boom(arguments, *, capture=False, extra_env=None):
        raise CloudCommandError("`terraform init` failed")

    monkeypatch.setattr(cloud_infra, "_run_terraform", boom)
    with pytest.raises(CloudCommandError):
        cloud_infra.terraform_init()

    assert not (cloud_infra.TERRAFORM_DIR / ".terraform" / "nyxgpt-config.sha256").exists()


def test_apply_writes_the_state_contract_allow_ip_reads(terraform_calls, monkeypatch):
    outputs = {
        "region": "us-east-1",
        "vpc_id": "vpc-123",
        "security_group_id": "sg-456",
        "instance_id": "i-789",
        "instance_type": "m5.xlarge",
        "public_ip": "198.51.100.200",
        "private_ip": "10.42.1.10",
        "ssh_key_name": "existing-pair",
        # An output the state file deliberately doesn't carry.
        "security_posture": {"ssh_only": True},
    }
    monkeypatch.setattr(cloud_infra, "terraform_outputs", lambda: outputs)

    cloud_infra.apply_infra(_args())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert written["security_group_id"] == "sg-456"
    assert written["region"] == "us-east-1"
    assert written["instance_id"] == "i-789"
    assert "security_posture" not in written
    assert cloud_infra.CLOUD_STATE_FILE.stat().st_mode & 0o777 == 0o600

    # The whole point of the handoff: allow-ip now resolves with no flags.
    resolved = cloud._resolve_security_group_id(argparse.Namespace())
    assert resolved == "sg-456"


def test_apply_preserves_unrelated_keys_in_the_shared_state_file(terraform_calls, monkeypatch):
    cloud_infra.CLOUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cloud_infra.CLOUD_STATE_FILE.write_text(
        json.dumps({"some_other_command": "value"}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_infra, "terraform_outputs", lambda: {"security_group_id": "sg-1"})

    cloud_infra.apply_infra(_args())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert written["some_other_command"] == "value"
    assert written["security_group_id"] == "sg-1"


# --- state refresh on re-provision (#3993) --------------------------------
#
# The defect: `write_cloud_state` merged, writing only the keys the new
# outputs carried, so a key the apply did not produce kept the *previous*
# substrate's value. Observed live -- state.json naming the new instance's id
# beside a destroyed substrate's security group, which sent
# `nyxgpt cloud allow-ip`'s auto-discovery at a group that no longer existed,
# while the operator was locked out and depending on it.


def test_apply_drops_a_prior_substrates_ids_it_did_not_reproduce(terraform_calls, monkeypatch):
    cloud_infra.CLOUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cloud_infra.CLOUD_STATE_FILE.write_text(
        json.dumps(
            {
                "instance_id": "i-old",
                # The destroyed substrate's group, which the merge kept.
                "security_group_id": "sg-0fad-destroyed",
                "public_ip": "198.51.100.1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cloud_infra,
        "terraform_outputs",
        lambda: {"instance_id": "i-new", "region": "us-east-1"},
    )

    cloud_infra.apply_infra(_args())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert written["instance_id"] == "i-new"
    assert "security_group_id" not in written, "a destroyed substrate's group must not survive"
    assert "public_ip" not in written


def test_apply_drops_ids_whose_output_came_back_null(terraform_calls, monkeypatch):
    """A null output is "this substrate has no such id", not "keep the old one"."""
    cloud_infra.CLOUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cloud_infra.CLOUD_STATE_FILE.write_text(
        json.dumps({"security_group_id": "sg-stale"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cloud_infra,
        "terraform_outputs",
        lambda: {"instance_id": "i-new", "security_group_id": None},
    )

    cloud_infra.apply_infra(_args())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert "security_group_id" not in written


def test_apply_leaves_state_alone_when_outputs_are_unreadable(terraform_calls, monkeypatch, capsys):
    """ "Cannot determine" is its own outcome (#3993).

    `terraform_outputs` returns `{}` when the *read* failed as well as when
    there is nothing to read. Blanking every recorded id because a read failed
    would be a worse lie than the stale one: the operator would be told
    nothing is provisioned by a function that never asked AWS. Left as-is, and
    said out loud.
    """
    cloud_infra.CLOUD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cloud_infra.CLOUD_STATE_FILE.write_text(
        json.dumps({"instance_id": "i-old", "security_group_id": "sg-old"}), encoding="utf-8"
    )
    monkeypatch.setattr(cloud_infra, "terraform_outputs", dict)

    result = cloud_infra.apply_infra(_args())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert written == {"instance_id": "i-old", "security_group_id": "sg-old"}
    assert result["state_refreshed"] is False
    err = capsys.readouterr().err
    assert "NOT been refreshed" in err
    assert "earlier substrate" in err


def test_apply_reports_a_refreshed_state_on_the_normal_path(terraform_calls, monkeypatch):
    monkeypatch.setattr(cloud_infra, "terraform_outputs", lambda: {"instance_id": "i-new"})

    assert cloud_infra.apply_infra(_args())["state_refreshed"] is True


def test_destroy_requires_existing_state(terraform_calls):
    with pytest.raises(CloudCommandError, match="nothing to destroy"):
        cloud_infra.destroy_infra(_args())


def test_destroy_uses_saved_settings_without_touching_the_network(terraform_calls, monkeypatch):
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args()))
    cloud_infra.TFSTATE_FILE.write_text("{}", encoding="utf-8")

    def explode() -> str:
        raise AssertionError("destroy must not re-detect the owner's public IP")

    monkeypatch.setattr(cloud_infra, "detect_current_public_ip", explode)

    cloud_infra.destroy_infra(argparse.Namespace())

    assert [call[0] for call in terraform_calls] == ["init", "destroy"]


def test_destroy_clears_only_this_modules_state_keys(terraform_calls):
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args()))
    cloud_infra.TFSTATE_FILE.write_text("{}", encoding="utf-8")
    cloud_infra.CLOUD_STATE_FILE.write_text(
        json.dumps({"security_group_id": "sg-1", "some_other_command": "value"}), encoding="utf-8"
    )

    cloud_infra.destroy_infra(argparse.Namespace())

    written = json.loads(cloud_infra.CLOUD_STATE_FILE.read_text(encoding="utf-8"))
    assert written == {"some_other_command": "value"}


def test_destroy_removes_the_state_file_when_nothing_else_is_left(terraform_calls):
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args()))
    cloud_infra.TFSTATE_FILE.write_text("{}", encoding="utf-8")
    cloud_infra.CLOUD_STATE_FILE.write_text(json.dumps({"instance_id": "i-1"}), encoding="utf-8")

    cloud_infra.destroy_infra(argparse.Namespace())

    assert not cloud_infra.CLOUD_STATE_FILE.exists()


def test_test_infra_runs_the_plan_suite_offline(monkeypatch):
    seen: list[tuple[list[str], dict | None]] = []

    def fake_run(arguments, *, capture=False, extra_env=None):
        seen.append((list(arguments), extra_env))
        return subprocess.CompletedProcess(["terraform", *arguments], 0, stdout="", stderr="")

    monkeypatch.setattr(cloud_infra, "_run_terraform", fake_run)

    assert cloud_infra.test_infra()["passed"] is True

    assert seen[0][0] == ["init", "-input=false", "-backend=false"]
    assert seen[1][0] == ["test"]
    assert seen[1][1]["AWS_ACCESS_KEY_ID"] == "testing"


# --- status --------------------------------------------------------------


def test_status_reports_nothing_provisioned_before_the_first_apply():
    status = cloud_infra.infra_status()
    assert status["provisioned"] is False
    assert status["instance_id"] == ""
    assert status["access_model"]["open_ports"] == []
    assert status["access_model"]["world_open_ingress"] is False


def test_status_reports_the_recorded_substrate(terraform_calls, monkeypatch):
    monkeypatch.setattr(
        cloud_infra,
        "terraform_outputs",
        lambda: {
            "region": "us-east-1",
            "instance_id": "i-789",
            "security_group_id": "sg-456",
            "public_ip": "198.51.100.200",
        },
    )
    cloud_infra.apply_infra(_args())

    status = cloud_infra.infra_status()
    assert status["provisioned"] is True
    assert status["instance_id"] == "i-789"
    assert status["security_group_id"] == "sg-456"
    assert status["public_ip"] == "198.51.100.200"
    assert status["owner_ip_cidr"] == "198.51.100.7/32"
    assert status["access_model"]["open_ports"] == [22]
    assert status["access_model"]["ssh_only"] is True


# --- status: which source can see the substrate from here (#3804) --------


def test_status_is_unknown_on_a_machine_that_is_neither_an_instance_nor_an_operator():
    """Never "not provisioned" without a source that could know (#3804).

    "Not provisioned" is an assertion about AWS. A machine that is not an
    instance and has never run Terraform for the substrate has checked
    nothing, so it must say so -- the rc12 defect in its general form.
    """
    status = cloud_infra.infra_status()

    assert status["known"] is False
    assert status["source"] == cloud_infra.SOURCE_UNKNOWN
    assert status["provisioned"] is False
    assert status["on_ec2"] is False


def test_status_reads_the_instance_itself_when_running_on_ec2(monkeypatch):
    """The owner's rc12 observation: served from the instance, state is elsewhere."""
    monkeypatch.setattr(
        cloud_infra.cloud_imds,
        "instance_facts",
        lambda **_kwargs: {
            "instance_id": "i-0abc123",
            "region": "us-east-1",
            "instance_type": "m5.xlarge",
            "public_ip": "203.0.113.10",
            "private_ip": "10.0.1.20",
            "vpc_id": "vpc-0def456",
            "subnet_id": "subnet-0aaa111",
            "security_group_id": "sg-0bbb222",
            "ssh_key_name": "nyxgpt-owner",
        },
    )

    status = cloud_infra.infra_status()

    assert status["source"] == cloud_infra.SOURCE_IMDS
    assert status["on_ec2"] is True
    assert status["known"] is True
    assert status["provisioned"] is True
    assert status["instance_id"] == "i-0abc123"
    assert status["vpc_id"] == "vpc-0def456"
    assert status["security_group_id"] == "sg-0bbb222"
    assert status["ssh_key_name"] == "nyxgpt-owner"
    assert status["access_model"]["open_ports"] == [22]
    # The one substrate fact the instance cannot see: which CIDR its security
    # group admits is a rule, not metadata. Left empty rather than guessed.
    assert status["owner_ip_cidr"] == ""


def test_instance_metadata_wins_over_a_stale_local_state_file(terraform_calls, monkeypatch):
    """Both sources present: the machine you are on beats a file describing another."""
    monkeypatch.setattr(
        cloud_infra,
        "terraform_outputs",
        lambda: {"region": "eu-west-2", "instance_id": "i-from-state"},
    )
    cloud_infra.apply_infra(_args())
    monkeypatch.setattr(
        cloud_infra.cloud_imds,
        "instance_facts",
        lambda **_kwargs: {"instance_id": "i-from-imds", "region": "us-east-1"},
    )

    status = cloud_infra.infra_status()

    assert status["instance_id"] == "i-from-imds"
    assert status["region"] == "us-east-1"


def test_status_reports_the_terraform_state_source_on_the_operator_workstation(
    terraform_calls, monkeypatch
):
    monkeypatch.setattr(
        cloud_infra, "terraform_outputs", lambda: {"region": "us-east-1", "instance_id": "i-789"}
    )
    cloud_infra.apply_infra(_args())

    status = cloud_infra.infra_status()

    assert status["source"] == cloud_infra.SOURCE_TERRAFORM_STATE
    assert status["on_ec2"] is False
    assert status["known"] is True
    assert status["provisioned"] is True


def test_not_provisioned_is_an_answer_once_this_machine_has_run_terraform(terraform_calls):
    """Settings saved here and no instance recorded: a real "nothing is up"."""
    cloud_infra.save_settings(cloud_infra.resolve_settings(_args()))

    status = cloud_infra.infra_status()

    assert status["known"] is True
    assert status["source"] == cloud_infra.SOURCE_TERRAFORM_STATE
    assert status["provisioned"] is False
    assert status["access_model"]["open_ports"] == []


# --- terraform binary + output decoding ----------------------------------


def test_ensure_terraform_binary_errors_without_brew(monkeypatch):
    monkeypatch.setattr(cloud_infra.shutil, "which", lambda name: None)
    with pytest.raises(CloudCommandError, match="Homebrew is unavailable"):
        cloud_infra.ensure_terraform_binary()


def _fake_terraform(tmp_path, script: str) -> str:
    """Write an executable stand-in for the terraform binary and return its path."""
    binary = tmp_path / "terraform"
    binary.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


def test_run_terraform_surfaces_the_diagnostic_on_failure(monkeypatch, tmp_path):
    """A failed run must carry Terraform's own error text -- the dashboard shows only that."""
    monkeypatch.setattr(
        cloud_infra,
        "ensure_terraform_binary",
        lambda: _fake_terraform(tmp_path, 'echo "Error: no valid credential sources" >&2\nexit 1'),
    )

    with pytest.raises(CloudCommandError, match="no valid credential sources"):
        cloud_infra._run_terraform(["apply"])


def test_run_terraform_leaves_stdout_streaming_for_the_cli(monkeypatch, tmp_path, capfd):
    """A long apply must not go silent: stdout stays attached unless it's being parsed."""
    monkeypatch.setattr(
        cloud_infra,
        "ensure_terraform_binary",
        lambda: _fake_terraform(tmp_path, 'echo "Creating aws_instance.this..."'),
    )

    cloud_infra._run_terraform(["apply"])

    assert "Creating aws_instance.this" in capfd.readouterr().out


def test_terraform_outputs_decodes_the_json_envelope(monkeypatch):
    payload = json.dumps({"instance_id": {"value": "i-1", "type": "string"}})

    def fake_run(arguments, *, capture=False, extra_env=None):
        return subprocess.CompletedProcess(["terraform"], 0, stdout=payload, stderr="")

    monkeypatch.setattr(cloud_infra, "_run_terraform", fake_run)
    assert cloud_infra.terraform_outputs() == {"instance_id": "i-1"}


def test_terraform_outputs_is_empty_before_the_first_apply(monkeypatch):
    def fake_run(arguments, *, capture=False, extra_env=None):
        raise CloudCommandError("no state")

    monkeypatch.setattr(cloud_infra, "_run_terraform", fake_run)
    assert cloud_infra.terraform_outputs() == {}


# --- CLI entry point -----------------------------------------------------


def test_infra_command_destroy_refuses_without_yes(capsys):
    args = _args()
    args.infra_cmd = "destroy"
    args.yes = False

    assert cloud_infra.infra_command(args) == 1
    assert "--yes" in capsys.readouterr().err


def test_infra_command_reports_errors_without_a_traceback(capsys, monkeypatch):
    monkeypatch.setattr(
        cloud_infra,
        "plan_infra",
        lambda args: (_ for _ in ()).throw(CloudCommandError("boom")),
    )
    args = _args()
    args.infra_cmd = "plan"

    assert cloud_infra.infra_command(args) == 1
    assert "boom" in capsys.readouterr().err


def test_infra_command_status_prints_json(capsys):
    args = argparse.Namespace(infra_cmd="status")
    assert cloud_infra.infra_command(args) == 0
    assert json.loads(capsys.readouterr().out)["provisioned"] is False

import argparse
import json
from types import SimpleNamespace

import httpx
import pytest

from nyxgpt import cloud, cloud_infra


class _FakeEC2Client:
    """Minimal stand-in for a boto3 EC2 client, recording calls made against it."""

    def __init__(self, existing_cidrs: list[str]) -> None:
        self.existing_cidrs = existing_cidrs
        self.calls: list[tuple[str, dict]] = []

    def describe_security_groups(self, **kwargs):
        self.calls.append(("describe_security_groups", kwargs))
        return {
            "SecurityGroups": [
                {
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": cidr} for cidr in self.existing_cidrs],
                        },
                        # A non-SSH rule that must never be touched.
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [{"CidrIp": "203.0.113.9/32"}],
                        },
                    ]
                }
            ]
        }

    def revoke_security_group_ingress(self, **kwargs):
        self.calls.append(("revoke_security_group_ingress", kwargs))

    def authorize_security_group_ingress(self, **kwargs):
        self.calls.append(("authorize_security_group_ingress", kwargs))


@pytest.fixture(autouse=True)
def _isolated_cloud_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "CLOUD_STATE_FILE", tmp_path / "cloud" / "state.json")
    # #3993 widened profile/region resolution beyond the state file, so every
    # other source it now consults has to be neutralised here too -- otherwise
    # the suite answers from the developer's own infra.json, config.ini and
    # AWS_PROFILE, and passes or fails according to their AWS setup.
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", tmp_path / "cloud" / "infra.json")
    monkeypatch.setattr(cloud, "_configured_cloud_reference", lambda: {"profile": "", "region": ""})
    monkeypatch.delenv("AWS_PROFILE", raising=False)


# --- normalize_cidr -----------------------------------------------------


def test_normalize_cidr_bare_ip_scopes_to_slash_32():
    assert cloud.normalize_cidr("203.0.113.5") == "203.0.113.5/32"


def test_normalize_cidr_keeps_explicit_cidr():
    assert cloud.normalize_cidr("203.0.113.0/24") == "203.0.113.0/24"


def test_normalize_cidr_refuses_open_cidr():
    with pytest.raises(cloud.CloudCommandError, match="0.0.0.0/0"):
        cloud.normalize_cidr("0.0.0.0/0")


def test_normalize_cidr_rejects_invalid_input():
    with pytest.raises(cloud.CloudCommandError):
        cloud.normalize_cidr("not-an-ip")


# --- detect_current_public_ip -------------------------------------------


def test_detect_current_public_ip_returns_trimmed_ip(monkeypatch):
    def fake_get(url, timeout):
        assert url == cloud.IP_ECHO_URL
        return httpx.Response(200, text="203.0.113.5\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(cloud.httpx, "get", fake_get)
    assert cloud.detect_current_public_ip() == "203.0.113.5"


def test_detect_current_public_ip_rejects_non_ip_response(monkeypatch):
    def fake_get(url, timeout):
        return httpx.Response(200, text="not-an-ip", request=httpx.Request("GET", url))

    monkeypatch.setattr(cloud.httpx, "get", fake_get)
    with pytest.raises(cloud.CloudCommandError):
        cloud.detect_current_public_ip()


def test_detect_current_public_ip_wraps_http_errors(monkeypatch):
    def fake_get(url, timeout):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(cloud.httpx, "get", fake_get)
    with pytest.raises(cloud.CloudCommandError):
        cloud.detect_current_public_ip()


# --- security group / region resolution ----------------------------------


def test_resolve_security_group_id_prefers_explicit_arg():
    args = argparse.Namespace(security_group_id="sg-explicit")
    assert cloud._resolve_security_group_id(args) == "sg-explicit"


def test_resolve_security_group_id_falls_back_to_state_file(tmp_path):
    cloud.CLOUD_STATE_FILE.parent.mkdir(parents=True)
    cloud.CLOUD_STATE_FILE.write_text(json.dumps({"security_group_id": "sg-from-state"}))
    args = argparse.Namespace(security_group_id=None)
    assert cloud._resolve_security_group_id(args) == "sg-from-state"


def test_resolve_security_group_id_raises_when_unresolvable():
    args = argparse.Namespace(security_group_id=None)
    with pytest.raises(cloud.CloudCommandError):
        cloud._resolve_security_group_id(args)


def test_resolve_region_prefers_explicit_arg():
    args = argparse.Namespace(region="us-east-1")
    assert cloud._resolve_region(args) == "us-east-1"


def test_resolve_region_falls_back_to_state_file(tmp_path):
    cloud.CLOUD_STATE_FILE.parent.mkdir(parents=True)
    cloud.CLOUD_STATE_FILE.write_text(json.dumps({"region": "us-west-2"}))
    args = argparse.Namespace(region=None)
    assert cloud._resolve_region(args) == "us-west-2"


def test_resolve_region_returns_none_when_unresolvable():
    args = argparse.Namespace(region=None)
    assert cloud._resolve_region(args) is None


# --- refresh_ssh_ingress_rule ---------------------------------------------


def test_refresh_ssh_ingress_rule_authorizes_when_no_existing_rule():
    client = _FakeEC2Client(existing_cidrs=[])

    old_cidrs, changed = cloud.refresh_ssh_ingress_rule(client, "sg-123", "203.0.113.5/32")

    assert old_cidrs == []
    assert changed is True
    call_names = [name for name, _ in client.calls]
    assert "revoke_security_group_ingress" not in call_names
    assert "authorize_security_group_ingress" in call_names
    authorize_kwargs = dict(client.calls)["authorize_security_group_ingress"]
    assert authorize_kwargs["GroupId"] == "sg-123"
    ip_ranges = authorize_kwargs["IpPermissions"][0]["IpRanges"]
    assert ip_ranges == [{"CidrIp": "203.0.113.5/32", "Description": cloud.SSH_RULE_DESCRIPTION}]


def test_refresh_ssh_ingress_rule_is_idempotent_when_unchanged():
    client = _FakeEC2Client(existing_cidrs=["203.0.113.5/32"])

    old_cidrs, changed = cloud.refresh_ssh_ingress_rule(client, "sg-123", "203.0.113.5/32")

    assert old_cidrs == ["203.0.113.5/32"]
    assert changed is False
    call_names = [name for name, _ in client.calls]
    assert call_names == ["describe_security_groups"]


def test_refresh_ssh_ingress_rule_replaces_stale_cidr():
    client = _FakeEC2Client(existing_cidrs=["198.51.100.1/32"])

    old_cidrs, changed = cloud.refresh_ssh_ingress_rule(client, "sg-123", "203.0.113.5/32")

    assert old_cidrs == ["198.51.100.1/32"]
    assert changed is True
    call_names = [name for name, _ in client.calls]
    assert "revoke_security_group_ingress" in call_names
    assert "authorize_security_group_ingress" in call_names
    revoke_kwargs = dict(client.calls)["revoke_security_group_ingress"]
    revoked_cidrs = [r["CidrIp"] for r in revoke_kwargs["IpPermissions"][0]["IpRanges"]]
    assert revoked_cidrs == ["198.51.100.1/32"]


def test_refresh_ssh_ingress_rule_authorizes_before_revoking_stale_cidr():
    """The new CIDR must be authorized before the stale one is revoked, so the
    security group is never left with zero valid SSH sources between calls."""
    client = _FakeEC2Client(existing_cidrs=["198.51.100.1/32"])

    cloud.refresh_ssh_ingress_rule(client, "sg-123", "203.0.113.5/32")

    call_names = [name for name, _ in client.calls]
    assert call_names.index("authorize_security_group_ingress") < call_names.index(
        "revoke_security_group_ingress"
    )


def test_refresh_ssh_ingress_rule_leaves_stale_cidr_when_authorize_fails():
    """If authorize fails, the stale rule must not be revoked -- the group must
    keep at least one valid SSH source rather than being left with none."""

    class _FailingAuthorizeClient(_FakeEC2Client):
        def authorize_security_group_ingress(self, **kwargs):
            super().authorize_security_group_ingress(**kwargs)
            raise RuntimeError("boom")

    client = _FailingAuthorizeClient(existing_cidrs=["198.51.100.1/32"])

    with pytest.raises(cloud.CloudCommandError):
        cloud.refresh_ssh_ingress_rule(client, "sg-123", "203.0.113.5/32")

    call_names = [name for name, _ in client.calls]
    assert "revoke_security_group_ingress" not in call_names


def test_refresh_ssh_ingress_rule_missing_group_raises():
    class _EmptyClient:
        def describe_security_groups(self, **kwargs):
            return {"SecurityGroups": []}

    with pytest.raises(cloud.CloudCommandError):
        cloud.refresh_ssh_ingress_rule(_EmptyClient(), "sg-missing", "203.0.113.5/32")


# --- _get_ec2_client -------------------------------------------------------


def test_get_ec2_client_raises_helpful_error_when_boto3_missing(monkeypatch):
    monkeypatch.setattr(cloud, "try_import", lambda name: None)
    with pytest.raises(cloud.CloudCommandError, match="nyxgpt\\[cloud\\]"):
        cloud._get_ec2_client(None)


def test_get_ec2_client_passes_region(monkeypatch):
    calls = []

    fake_boto3 = SimpleNamespace(client=lambda service, **kwargs: calls.append((service, kwargs)))
    monkeypatch.setattr(cloud, "try_import", lambda name: fake_boto3 if name == "boto3" else None)

    cloud._get_ec2_client("us-east-1")

    assert calls == [("ec2", {"region_name": "us-east-1"})]


# --- allow_ip (CLI orchestration) -----------------------------------------


def _allow_ip_args(**overrides):
    defaults = {"ip": None, "security_group_id": "sg-123", "region": None, "profile": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_allow_ip_updates_rule_and_prints_old_new(monkeypatch, capsys):
    client = _FakeEC2Client(existing_cidrs=["198.51.100.1/32"])
    monkeypatch.setattr(cloud, "_get_ec2_client", lambda region, profile="": client)
    monkeypatch.setattr(cloud, "detect_current_public_ip", lambda: "203.0.113.5")

    exit_code = cloud.allow_ip(_allow_ip_args())

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "198.51.100.1/32" in out
    assert "203.0.113.5/32" in out


def test_allow_ip_is_idempotent(monkeypatch, capsys):
    client = _FakeEC2Client(existing_cidrs=["203.0.113.5/32"])
    monkeypatch.setattr(cloud, "_get_ec2_client", lambda region, profile="": client)
    monkeypatch.setattr(cloud, "detect_current_public_ip", lambda: "203.0.113.5")

    exit_code = cloud.allow_ip(_allow_ip_args())

    assert exit_code == 0
    assert "no change" in capsys.readouterr().out.lower()
    call_names = [name for name, _ in client.calls]
    assert "authorize_security_group_ingress" not in call_names
    assert "revoke_security_group_ingress" not in call_names


def test_allow_ip_uses_explicit_ip_override_without_detecting(monkeypatch):
    client = _FakeEC2Client(existing_cidrs=[])
    monkeypatch.setattr(cloud, "_get_ec2_client", lambda region, profile="": client)

    def _should_not_be_called():
        raise AssertionError("detect_current_public_ip should not be called when --ip is given")

    monkeypatch.setattr(cloud, "detect_current_public_ip", _should_not_be_called)

    exit_code = cloud.allow_ip(_allow_ip_args(ip="203.0.113.42"))

    assert exit_code == 0
    authorize_kwargs = dict(client.calls)["authorize_security_group_ingress"]
    ip_ranges = authorize_kwargs["IpPermissions"][0]["IpRanges"]
    assert ip_ranges[0]["CidrIp"] == "203.0.113.42/32"


def test_allow_ip_refuses_open_cidr(monkeypatch, capsys):
    exit_code = cloud.allow_ip(_allow_ip_args(ip="0.0.0.0/0"))

    assert exit_code == 1
    assert "0.0.0.0/0" in capsys.readouterr().err


def test_allow_ip_reports_missing_security_group(capsys):
    exit_code = cloud.allow_ip(_allow_ip_args(security_group_id=None))

    assert exit_code == 1
    assert "security group" in capsys.readouterr().err.lower()


# --- credential resolution (#3993) ----------------------------------------
#
# The defect these cover: `_get_ec2_client` built a bare `boto3.client`, so a
# workstation whose *default* AWS profile names a different account got
# `InvalidGroup.NotFound` for a security group that existed -- reported to an
# operator who, being locked out of SSH, could not check. `allow-ip` is the
# recovery tool for exactly that state, so its failure reading as "your
# infrastructure is gone" is the worst possible failure mode it could have.


def _fake_boto3(record: list, sts_account: str = "111122223333") -> SimpleNamespace:
    """A boto3 stand-in recording how each client was built (bare vs. named Session)."""

    def _client(service, **kwargs):
        record.append(("bare", service, kwargs))
        return SimpleNamespace(get_caller_identity=lambda: {"Account": sts_account})

    def _session(profile_name):
        def _session_client(service, **kwargs):
            record.append((profile_name, service, kwargs))
            return SimpleNamespace(get_caller_identity=lambda: {"Account": sts_account})

        return SimpleNamespace(client=_session_client)

    return SimpleNamespace(client=_client, Session=_session)


class _NotFoundEC2Client:
    """An EC2 client answering every describe with botocore's InvalidGroup.NotFound."""

    def describe_security_groups(self, **kwargs):
        error = Exception(
            "An error occurred (InvalidGroup.NotFound) when calling the "
            "DescribeSecurityGroups operation: The security group "
            "'sg-0fed18aaeb342c218' does not exist"
        )
        error.response = {"Error": {"Code": "InvalidGroup.NotFound"}}
        raise error


def test_resolve_profile_prefers_the_explicit_flag(monkeypatch):
    monkeypatch.setattr(cloud, "_saved_infra_settings", lambda: {"aws_profile": "from-infra"})
    monkeypatch.setattr(
        cloud, "_configured_cloud_reference", lambda: {"profile": "from-config", "region": ""}
    )
    monkeypatch.setenv("AWS_PROFILE", "from-env")

    assert cloud._resolve_profile(argparse.Namespace(profile="from-flag")) == "from-flag"


def test_resolve_profile_falls_back_to_the_last_applied_substrate(monkeypatch):
    monkeypatch.setattr(cloud, "_saved_infra_settings", lambda: {"aws_profile": "from-infra"})
    monkeypatch.setattr(
        cloud, "_configured_cloud_reference", lambda: {"profile": "from-config", "region": ""}
    )

    assert cloud._resolve_profile(argparse.Namespace(profile=None)) == "from-infra"


def test_resolve_profile_falls_back_to_config_ini(monkeypatch):
    """The owner's exact configuration: `[cloud] profile` set and nothing else."""
    monkeypatch.setattr(
        cloud, "_configured_cloud_reference", lambda: {"profile": "nyxgpt", "region": ""}
    )

    assert cloud._resolve_profile(argparse.Namespace(profile=None)) == "nyxgpt"


def test_resolve_profile_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "from-env")

    assert cloud._resolve_profile(argparse.Namespace(profile=None)) == "from-env"


def test_resolve_profile_is_empty_when_nothing_configures_one():
    assert cloud._resolve_profile(argparse.Namespace(profile=None)) == ""


def test_get_ec2_client_uses_a_session_for_the_resolved_profile(monkeypatch):
    """The fix itself: a resolved profile must reach boto3 rather than be dropped."""
    record: list = []
    monkeypatch.setattr(
        cloud, "try_import", lambda name: _fake_boto3(record) if name == "boto3" else None
    )

    cloud._get_ec2_client("us-east-1", "nyxgpt")

    assert record == [("nyxgpt", "ec2", {"region_name": "us-east-1"})]


def test_get_ec2_client_stays_on_the_default_chain_without_a_profile(monkeypatch):
    record: list = []
    monkeypatch.setattr(
        cloud, "try_import", lambda name: _fake_boto3(record) if name == "boto3" else None
    )

    cloud._get_ec2_client("us-east-1")

    assert record == [("bare", "ec2", {"region_name": "us-east-1"})]


def test_allow_ip_authenticates_with_the_configured_profile(monkeypatch):
    """End to end and flag-free: `[cloud] profile` alone must reach the EC2 client.

    The owner's reported scenario. Before the fix nothing resolved the profile
    at all and the client was built bare, so the describe ran in whichever
    account the workstation's default profile names.
    """
    monkeypatch.setattr(
        cloud, "_configured_cloud_reference", lambda: {"profile": "nyxgpt", "region": ""}
    )
    monkeypatch.setattr(cloud, "detect_current_public_ip", lambda: "203.0.113.5")

    built: list = []

    def _capture(region, profile=""):
        built.append((region, profile))
        return _FakeEC2Client(existing_cidrs=["198.51.100.1/32"])

    monkeypatch.setattr(cloud, "_get_ec2_client", _capture)

    exit_code = cloud.allow_ip(_allow_ip_args())

    assert exit_code == 0
    assert built == [(None, "nyxgpt")]


def test_not_found_names_the_account_and_profile_it_queried(monkeypatch, capsys):
    """A NotFound must never read as destroyed infrastructure (#3993).

    The group id was right and the group existed; the query went to the wrong
    account. Nothing in the old message could tell those two apart, and the
    operator seeing it had no SSH access with which to check.
    """
    monkeypatch.setattr(cloud, "detect_current_public_ip", lambda: "203.0.113.5")
    monkeypatch.setattr(cloud, "_get_ec2_client", lambda region, profile="": _NotFoundEC2Client())
    monkeypatch.setattr(
        cloud,
        "describe_credential_context",
        lambda region, profile: "AWS account 999888777666, profile 'nyxgpt'",
    )

    exit_code = cloud.allow_ip(_allow_ip_args(security_group_id="sg-0fed18aaeb342c218"))

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "AWS account 999888777666, profile 'nyxgpt'" in err
    assert "credential-resolution problem" in err
    assert "not a destroyed substrate" in err


def test_a_non_not_found_error_is_not_annotated(monkeypatch, capsys):
    """Only a NotFound earns the account annotation.

    A throttle or a network failure says nothing about which account was
    queried, and paying an extra STS round trip on every failure would slow
    down the one command an operator runs while locked out.
    """

    class _ThrottledClient:
        def describe_security_groups(self, **kwargs):
            error = Exception("Rate exceeded")
            error.response = {"Error": {"Code": "RequestLimitExceeded"}}
            raise error

    called: list = []
    monkeypatch.setattr(cloud, "detect_current_public_ip", lambda: "203.0.113.5")
    monkeypatch.setattr(cloud, "_get_ec2_client", lambda region, profile="": _ThrottledClient())
    monkeypatch.setattr(
        cloud,
        "describe_credential_context",
        lambda region, profile: (called.append(1), "unused")[1],
    )

    exit_code = cloud.allow_ip(_allow_ip_args())

    assert exit_code == 1
    assert called == [], "STS must not be called for an unrelated failure"
    assert "credential-resolution problem" not in capsys.readouterr().err


def test_describe_credential_context_reports_the_account(monkeypatch):
    record: list = []
    monkeypatch.setattr(
        cloud,
        "try_import",
        lambda name: _fake_boto3(record, sts_account="123456789012") if name == "boto3" else None,
    )

    assert cloud.describe_credential_context("us-east-1", "nyxgpt") == (
        "AWS account 123456789012, profile 'nyxgpt'"
    )
    assert record == [("nyxgpt", "sts", {"region_name": "us-east-1"})]


def test_describe_credential_context_survives_an_sts_failure(monkeypatch):
    """Best-effort by design: annotating an error must never replace it."""

    def _boom(profile_name):
        raise RuntimeError("expired token")

    monkeypatch.setattr(
        cloud,
        "try_import",
        lambda name: SimpleNamespace(client=None, Session=_boom) if name == "boto3" else None,
    )

    result = cloud.describe_credential_context("us-east-1", "nyxgpt")

    assert "profile 'nyxgpt'" in result
    assert "could not be determined" in result


def test_resolve_region_falls_back_to_config_ini(monkeypatch):
    monkeypatch.setattr(
        cloud, "_configured_cloud_reference", lambda: {"profile": "", "region": "eu-west-1"}
    )

    assert cloud._resolve_region(argparse.Namespace(region=None)) == "eu-west-1"

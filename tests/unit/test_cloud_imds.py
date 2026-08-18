"""Unit tests for `nyxgpt.cloud_imds` (#3804).

The module answers one question -- "am I running on an EC2 instance, and if
so which one?" -- and the Infrastructure page's AWS section is only correct
because of it: a dashboard served *from* the provisioned instance has no
Terraform state, so before this it reported "not provisioned" while running
on the machine Terraform had just created.

Nothing here touches the network: `urllib.request.urlopen` is replaced with a
fake IMDS that enforces the parts of the protocol that actually matter --
IMDSv2's token handshake, and the token header on every read.
"""

from __future__ import annotations

import urllib.request

import pytest

from nyxgpt import cloud_imds

# Captured before tests/conftest.py's autouse guard replaces it with a
# "not on EC2" stub: exercising the real reader is the point of this file.
_REAL_READ_METADATA = cloud_imds.read_metadata

pytestmark = pytest.mark.unit

TOKEN = "AQAAAsomeopaquetoken=="

# A complete instance, as the substrate's Terraform provisions it.
_METADATA = {
    "meta-data/instance-id": "i-0abc123def456",
    "meta-data/placement/region": "us-east-1",
    "meta-data/placement/availability-zone": "us-east-1a",
    "meta-data/instance-type": "m5.large",
    "meta-data/public-ipv4": "203.0.113.10",
    "meta-data/local-ipv4": "10.0.1.20",
    "meta-data/mac": "0a:1b:2c:3d:4e:5f",
    "meta-data/network/interfaces/macs/0a:1b:2c:3d:4e:5f/vpc-id": "vpc-0def456",
    "meta-data/network/interfaces/macs/0a:1b:2c:3d:4e:5f/subnet-id": "subnet-0aaa111",
    "meta-data/network/interfaces/macs/0a:1b:2c:3d:4e:5f/security-group-ids": "sg-0bbb222",
    "meta-data/public-keys/": "0=nyxgpt-owner",
}


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_imds(metadata: dict[str, str], *, token: str | None = TOKEN):
    """Return a urlopen replacement serving `metadata`, plus the request log."""
    seen: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        seen.append(request)
        url = request.full_url
        if url == cloud_imds.TOKEN_URL:
            if token is None:
                raise OSError("no route to host")
            return _Response(token)
        # Every read must present the token -- IMDSv2 refuses otherwise, and a
        # fake that did not enforce it would let a token-less regression pass.
        assert request.get_header("X-aws-ec2-metadata-token") == token
        path = url[len(cloud_imds.IMDS_BASE) + 1 :]
        if path not in metadata:
            raise OSError(f"404 {path}")
        return _Response(metadata[path])

    return urlopen, seen


def test_read_metadata_returns_none_when_imds_is_unreachable(monkeypatch):
    """A workstation, a container or a hop-limited pod is "not on EC2", not an error."""
    urlopen, _ = _fake_imds({}, token=None)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert _REAL_READ_METADATA() is None


def test_read_metadata_returns_none_when_something_that_is_not_imds_answers(monkeypatch):
    """A token but no instance id: report not-EC2 rather than half an instance."""
    urlopen, _ = _fake_imds({})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert _REAL_READ_METADATA() is None


def test_read_metadata_describes_the_running_instance(monkeypatch):
    urlopen, seen = _fake_imds(_METADATA)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    facts = _REAL_READ_METADATA()

    assert facts == {
        "instance_id": "i-0abc123def456",
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "instance_type": "m5.large",
        "public_ip": "203.0.113.10",
        "private_ip": "10.0.1.20",
        "vpc_id": "vpc-0def456",
        "subnet_id": "subnet-0aaa111",
        "security_group_id": "sg-0bbb222",
        "ssh_key_name": "nyxgpt-owner",
    }
    # The handshake is a PUT carrying the TTL header, per IMDSv2.
    token_request = seen[0]
    assert token_request.get_method() == "PUT"
    assert token_request.full_url == cloud_imds.TOKEN_URL
    assert token_request.get_header("X-aws-ec2-metadata-token-ttl-seconds") == str(
        cloud_imds.TOKEN_TTL_SECONDS
    )


def test_absent_metadata_nodes_read_as_empty_rather_than_failing(monkeypatch):
    """An instance with no public IP has no `public-ipv4` node at all."""
    metadata = {k: v for k, v in _METADATA.items() if k != "meta-data/public-ipv4"}
    urlopen, _ = _fake_imds(metadata)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    facts = _REAL_READ_METADATA()

    assert facts is not None
    assert facts["public_ip"] == ""
    assert facts["instance_id"] == "i-0abc123def456"


def test_several_security_groups_are_all_reported(monkeypatch):
    metadata = dict(_METADATA)
    metadata["meta-data/network/interfaces/macs/0a:1b:2c:3d:4e:5f/security-group-ids"] = (
        "sg-0bbb222\nsg-0ccc333"
    )
    urlopen, _ = _fake_imds(metadata)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    facts = _REAL_READ_METADATA()

    assert facts is not None
    assert facts["security_group_id"] == "sg-0bbb222, sg-0ccc333"


def test_network_facts_are_empty_without_a_mac(monkeypatch):
    metadata = {k: v for k, v in _METADATA.items() if not k.startswith("meta-data/network")}
    del metadata["meta-data/mac"]
    urlopen, _ = _fake_imds(metadata)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    facts = _REAL_READ_METADATA()

    assert facts is not None
    assert facts["vpc_id"] == ""
    assert facts["security_group_id"] == ""


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ("0=nyxgpt-owner", "nyxgpt-owner"),
        ("0=nyxgpt-owner\n1=second-key", "nyxgpt-owner"),
        # No key pair attached, or an index line in an unexpected shape.
        ("", ""),
        ("0", ""),
    ],
)
def test_the_key_pair_name_comes_from_the_public_key_index(monkeypatch, listing, expected):
    metadata = dict(_METADATA)
    metadata["meta-data/public-keys/"] = listing
    urlopen, _ = _fake_imds(metadata)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    facts = _REAL_READ_METADATA()

    assert facts is not None
    assert facts["ssh_key_name"] == expected


def test_instance_facts_caches_both_answers_so_polling_stays_cheap(monkeypatch):
    """The status endpoints poll; an uncached miss costs a full timeout."""
    calls = {"n": 0}

    def counting(timeout: float | None = None) -> dict[str, str] | None:
        calls["n"] += 1
        return {"instance_id": "i-0abc123def456"}

    monkeypatch.setattr(cloud_imds, "read_metadata", counting)
    cloud_imds.reset_cache()

    assert cloud_imds.instance_facts() == {"instance_id": "i-0abc123def456"}
    assert cloud_imds.instance_facts() == {"instance_id": "i-0abc123def456"}
    assert calls["n"] == 1

    # A negative answer is cached exactly like a positive one -- that is the
    # case that would otherwise pay the link-local timeout on every request.
    def none(timeout: float | None = None) -> dict[str, str] | None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(cloud_imds, "read_metadata", none)
    cloud_imds.reset_cache()

    assert cloud_imds.instance_facts() is None
    assert cloud_imds.instance_facts() is None
    assert calls["n"] == 2


def test_force_and_reset_bypass_the_cache(monkeypatch):
    answers = [{"instance_id": "first"}, {"instance_id": "second"}, {"instance_id": "third"}]

    def popping(timeout: float | None = None) -> dict[str, str] | None:
        return answers.pop(0)

    monkeypatch.setattr(cloud_imds, "read_metadata", popping)
    cloud_imds.reset_cache()

    assert cloud_imds.instance_facts() == {"instance_id": "first"}
    assert cloud_imds.instance_facts(force=True) == {"instance_id": "second"}
    cloud_imds.reset_cache()
    assert cloud_imds.instance_facts() == {"instance_id": "third"}


def test_on_ec2_is_the_yes_no_form_of_the_same_read(monkeypatch):
    monkeypatch.setattr(cloud_imds, "read_metadata", lambda timeout=None: {"instance_id": "i-1"})
    cloud_imds.reset_cache()
    assert cloud_imds.on_ec2() is True

    monkeypatch.setattr(cloud_imds, "read_metadata", lambda timeout=None: None)
    cloud_imds.reset_cache()
    assert cloud_imds.on_ec2() is False

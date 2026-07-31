"""Unit tests for issue #3346: container data relocated under ~/.nyxGPT/volumes/.

Guards docker-compose.yml and terraform/main.tf against reintroducing opaque
named Docker volumes, and checks that the three directories genuinely shared
across deployment modes (ollama, cassandra, nyxgpt-data) resolve to the same
host path in both files -- the whole point of the change is that switching
deployment modes doesn't lose chats or re-download models.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _main_tf() -> str:
    return (REPO_ROOT / "terraform" / "main.tf").read_text()


def test_docker_compose_declares_no_named_volumes() -> None:
    compose = _compose()
    assert not compose.get("volumes"), (
        "docker-compose.yml should have no top-level named volumes -- container data "
        "must be a host bind mount under ~/.nyxGPT/volumes/ (see docs/docker-compose.md#volumes)"
    )


@pytest.mark.parametrize(
    ("service", "container_path", "component"),
    [
        ("ollama", "/root/.ollama", "ollama"),
        ("cassandra", "/var/lib/cassandra", "cassandra"),
        ("api", "/root/.nyxGPT", "nyxgpt-data"),
        ("web", "/var/log/nyxgpt-web", "nyxgpt-web-logs"),
        ("prometheus", "/prometheus", "prometheus"),
        ("grafana", "/var/lib/grafana", "grafana"),
        ("loki", "/loki", "loki"),
        ("glitchtip-postgres", "/var/lib/postgresql/data", "glitchtip-postgres"),
    ],
)
def test_compose_service_bind_mounts_host_volume_dir(
    service: str, container_path: str, component: str
) -> None:
    compose = _compose()
    volumes = compose["services"][service]["volumes"]
    expected = f"${{HOME}}/.nyxGPT/volumes/{component}:{container_path}"
    assert any(
        v == expected for v in volumes
    ), f"{service} should bind-mount {expected!r}, got {volumes!r}"


def test_glitchtip_uploads_shared_by_glitchtip_and_worker() -> None:
    compose = _compose()
    expected = "${HOME}/.nyxGPT/volumes/glitchtip-uploads:/code/uploads"
    for service in ("glitchtip", "glitchtip-worker"):
        assert expected in compose["services"][service]["volumes"]


def test_terraform_declares_no_docker_volume_resources() -> None:
    main_tf = _main_tf()
    assert not re.search(r'resource\s+"docker_volume"', main_tf), (
        "terraform/main.tf should have no docker_volume resources -- container data "
        "must be a host_path bind mount under ~/.nyxGPT/volumes/ (see docs/terraform.md)"
    )


@pytest.mark.parametrize(
    ("resource", "container_path", "component"),
    [
        ("ollama", "/root/.ollama", "ollama"),
        ("cassandra", "/var/lib/cassandra", "cassandra"),
        ("api", "/root/.nyxGPT", "nyxgpt-data"),
    ],
)
def test_terraform_container_bind_mounts_host_volume_dir(
    resource: str, container_path: str, component: str
) -> None:
    main_tf = _main_tf()
    block_match = re.search(
        rf'resource\s+"docker_container"\s+"{resource}"\s*\{{(.*?)\n\}}', main_tf, re.DOTALL
    )
    assert block_match, f"expected a docker_container.{resource} resource block"
    block = block_match.group(1)
    assert re.search(
        rf'host_path\s*=\s*pathexpand\("~/\.nyxGPT/volumes/{component}"\)\s*\n\s*container_path\s*=\s*"{re.escape(container_path)}"',
        block,
    ), f"docker_container.{resource} should bind-mount ~/.nyxGPT/volumes/{component} at {container_path}"


@pytest.mark.parametrize("component", ["ollama", "cassandra", "nyxgpt-data"])
def test_shared_components_use_the_same_host_dir_in_compose_and_terraform(component: str) -> None:
    # The whole point of #3346: these three directories are reused as-is by
    # every mode that mounts them, so the literal path string must match.
    compose_path = f"${{HOME}}/.nyxGPT/volumes/{component}"
    terraform_path = f'pathexpand("~/.nyxGPT/volumes/{component}")'
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text()
    main_tf = _main_tf()
    assert compose_path in compose_text
    assert terraform_path in main_tf

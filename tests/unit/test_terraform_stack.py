"""Unit tests for the local-first Terraform IaC config (terraform/).

Guards the invariants called out in docs/terraform.md and the issue #2690
owner reversal: docker-provider only, no cloud provider resources, local
state backend, and the core stack matching docker-compose.yml's services.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DIR = REPO_ROOT / "terraform"

# Resource-type prefixes used by common cloud providers. None of these should
# ever appear in this local-only stack.
CLOUD_RESOURCE_PREFIXES = ("aws_", "google_", "azurerm_", "digitalocean_", "linode_")


def _read(name: str) -> str:
    return (TERRAFORM_DIR / name).read_text()


def test_only_the_docker_provider_is_required() -> None:
    versions = _read("versions.tf")
    assert 'source  = "kreuzwerker/docker"' in versions
    for provider in ("aws", "google", "azurerm"):
        assert f'source  = "hashicorp/{provider}"' not in versions


def test_state_backend_is_local() -> None:
    versions = _read("versions.tf")
    assert 'backend "local"' in versions


def test_no_cloud_provider_resources_are_declared() -> None:
    main = _read("main.tf")
    resource_types = re.findall(r'resource\s+"([a-z0-9_]+)"', main)
    assert resource_types, "expected at least one resource block in main.tf"

    for resource_type in resource_types:
        assert not resource_type.startswith(
            CLOUD_RESOURCE_PREFIXES
        ), f"unexpected cloud provider resource {resource_type!r} in main.tf"


def test_core_stack_matches_docker_compose_services() -> None:
    main = _read("main.tf")
    container_names = set(re.findall(r'resource\s+"docker_container"\s+"([a-z_]+)"', main))
    assert container_names == {"ollama", "cassandra", "api", "web"}


def test_auth_api_key_variable_is_marked_sensitive() -> None:
    variables = _read("variables.tf")
    auth_block = re.search(r'variable\s+"auth_api_key"\s*\{([^}]*)\}', variables, re.DOTALL)
    assert auth_block, "expected an auth_api_key variable block"
    assert "sensitive   = true" in auth_block.group(1)


def test_docs_reference_the_scope_reversal_rationale() -> None:
    docs = (REPO_ROOT / "docs" / "terraform.md").read_text()
    assert "No cloud provider modules" in docs
    assert "#2690" in docs


def test_admin_deploy_page_references_terraform_docs() -> None:
    page = (REPO_ROOT / "web" / "src" / "app" / "admin" / "deploy" / "page.tsx").read_text()
    assert "docs/terraform.md" in page

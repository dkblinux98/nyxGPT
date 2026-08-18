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


def _resource_block(hcl: str, resource_type: str, resource_name: str) -> str:
    """Extract a top-level `resource "<type>" "<name>" { ... }` block's body.

    HCL nests braces (`volumes { ... }`, `networks_advanced { ... }`), so a
    naive `[^}]*` regex (fine for the flat `variable` block above) would stop
    at the first nested block's closing brace instead of the resource's own.
    This walks braces to find the matching close instead.
    """
    marker = f'resource "{resource_type}" "{resource_name}"'
    start = hcl.index(marker)
    return _block_after(hcl, hcl.index("{", start))


def _block_after(hcl: str, open_brace: int) -> str:
    """Return the body between `hcl[open_brace]` (a `{`) and its balanced `}`.

    Brace-counts rather than regex-matching to `}` -- HCL interpolations like
    `${var.repo_path}` contain their own `{`/`}` pair, which a naive
    `[^}]*` block regex stops at prematurely.
    """
    depth = 0
    for i, ch in enumerate(hcl[open_brace:], start=open_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return hcl[open_brace + 1 : i]
    raise AssertionError(f"unbalanced braces starting at offset {open_brace}")


def _without_comments(hcl: str) -> str:
    """Strip `#` comment lines -- a comment explaining why something is NOT
    done must not read as it being done (several of these files carry a
    comment naming `var.repo_path` precisely to say it is not used there)."""
    return "\n".join(line for line in hcl.splitlines() if not line.lstrip().startswith("#"))


def _sub_blocks(hcl: str, keyword: str) -> list[str]:
    """Return the body of every top-level `<keyword> { ... }` block in `hcl`."""
    blocks = []
    for m in re.finditer(rf"(?<![\w.])({re.escape(keyword)})\s*\{{", hcl):
        blocks.append(_block_after(hcl, m.end() - 1))
    return blocks


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


def test_api_container_sets_compose_file_env_for_observability_visibility() -> None:
    """#3588: without NYXGPT_COMPOSE_FILE, self_heal.py's `_resolve_compose_file()`
    can't find a docker-compose.yml inside `nyxgpt-tf-api`, so every `docker
    compose ps` call silently fails and the observability tier (Grafana/Loki/
    Jaeger/GlitchTip) never appears on the Self-Heal or Infrastructure Status
    pages in Terraform mode -- even though the containers are running.
    """
    api_block = _resource_block(_read("main.tf"), "docker_container", "api")
    assert "NYXGPT_COMPOSE_FILE=/etc/nyxgpt/docker-compose.yml" in api_block


def test_api_container_bind_mounts_docker_compose_file() -> None:
    """The compose file NYXGPT_COMPOSE_FILE points at must actually be mounted
    into the container, mirroring the Compose `api` service's own
    `./docker-compose.yml:/etc/nyxgpt/docker-compose.yml:ro` mount.
    """
    api_block = _resource_block(_read("main.tf"), "docker_container", "api")
    volume_blocks = _sub_blocks(api_block, "volumes")
    matching = [
        v
        for v in volume_blocks
        if "docker-compose.yml" in v and "/etc/nyxgpt/docker-compose.yml" in v
    ]
    assert matching, f"expected a docker-compose.yml volume mount in: {api_block!r}"
    assert "read_only      = true" in matching[0]


def test_api_container_still_mounts_docker_socket() -> None:
    """Regression guard: the compose-file mount added for #3588 must not have
    displaced the pre-existing docker.sock mount self-heal needs to actually
    drive `docker`/`docker compose` commands from inside the container.
    """
    api_block = _resource_block(_read("main.tf"), "docker_container", "api")
    volume_blocks = _sub_blocks(api_block, "volumes")
    assert any(
        "/var/run/docker.sock" in v and v.count("/var/run/docker.sock") == 2 for v in volume_blocks
    )


# --- dev mode vs the artifact path (#3835) ---


def test_images_are_variables_so_published_ones_can_be_deployed() -> None:
    """The artifact path runs `ghcr.io/dkblinux98/nyxgpt-*`, whose repository
    is not `nyxgpt-api` -- so the image is a full ref passed in, not a tag
    appended to a hard-coded name."""
    main = _read("main.tf")
    assert "name = var.api_image" in main
    assert "name = var.web_image" in main


def test_the_build_block_is_conditional_on_dev_mode() -> None:
    """A `build {}` block makes the deploy need a checkout. It must exist only
    when `build_from_source` says the operator asked for one (`--dev`)."""
    main = _read("main.tf")
    for resource in ("api", "web"):
        block = _resource_block(main, "docker_image", resource)
        marker = block.find('dynamic "build"')
        assert marker != -1, f"expected a dynamic build block in docker_image.{resource}"
        dynamic_body = _block_after(block, block.index("{", marker))
        assert "var.build_from_source ? [1] : []" in dynamic_body
        # No unconditional build: `build {` may only appear inside the dynamic
        # block's content, never at the resource's own level.
        assert not re.search(r"^\s{2}build\s*\{", block, re.MULTILINE)


def test_repo_path_is_optional_so_a_machine_with_no_checkout_can_apply() -> None:
    variables = _read("variables.tf")
    repo_path = re.search(r'variable\s+"repo_path"\s*\{([^}]*)\}', variables, re.DOTALL)
    assert repo_path, "expected a repo_path variable block"
    assert 'default     = ""' in repo_path.group(1)


def test_no_container_mount_resolves_through_the_checkout() -> None:
    """Every host path the core stack mounts must be ops-managed (~/.nyxGPT)
    or a system path -- a `${var.repo_path}/...` mount would make the artifact
    path require the repository it is defined to work without (#3835)."""
    main = _read("main.tf")
    for resource in ("api", "web", "ollama", "cassandra"):
        block = _resource_block(main, "docker_container", resource)
        for volume in _sub_blocks(block, "volumes"):
            assert "var.repo_path" not in _without_comments(
                volume
            ), f"{resource} mounts through the checkout: {volume}"


def test_the_local_stack_config_is_packaged_for_repo_less_installs() -> None:
    """`nyxgpt ops install --terraform --local` materializes these files from
    package data (`_sync_local_terraform_config`), so every one of them has to
    be in `nyxgpt.resources` and be the same file as the tracked source."""
    packaged = REPO_ROOT / "src" / "nyxgpt" / "resources" / "terraform" / "local"
    for name in (
        "main.tf",
        "variables.tf",
        "outputs.tf",
        "versions.tf",
        "terraform.tfvars.example",
    ):
        assert (packaged / name).is_file(), f"{name} is not packaged"
        assert (packaged / name).read_text() == (TERRAFORM_DIR / name).read_text()


def test_the_tfvars_example_carries_no_checkout_path() -> None:
    """The bootstrapped tfvars is what an operator keeps; nothing in it may
    tie the deployment to a repository (#3835)."""
    assert "repo_path" not in _without_comments(_read("terraform.tfvars.example"))

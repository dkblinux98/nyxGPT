"""Regression guard for issue #3345: every third-party Docker image reference
in docker-compose.yml, terraform/main.tf, and src/nyxgpt/ops.py must be pinned
to a specific version -- no `:latest` and no bare/untagged image name. See
docs/docker-compose.md#image-pinning for the full policy.

First-party images built from this repo (`nyxgpt-api`, `nyxgpt-web`) are out
of scope -- their tag is a build-time variable (e.g. `${NYXGPT_API_IMAGE_TAG}`
/ `var.api_image_tag`), not a pulled third-party version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

PINNED_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "terraform" / "main.tf",
    REPO_ROOT / "src" / "nyxgpt" / "ops.py",
)

# name -> pinned tag, shared across all three definition points.
EXPECTED_PINS = {
    "ollama/ollama": "0.32.4",
    "cassandra": "5.0.8",
    "prom/prometheus": "v3.13.1",
    "grafana/grafana": "13.1.1",
    "grafana/loki": "3.6.13",
    "grafana/promtail": "3.6.11",
    "otel/opentelemetry-collector-contrib": "0.157.0",
    "jaegertracing/all-in-one": "1.76.0",
    "glitchtip/glitchtip": "6.2.0",
    "postgres": "16.14",
    "redis": "7.4.9-alpine",
}

# docker-compose.yml `image:` values and terraform `docker_image` names for
# the two first-party images built from this repo -- excluded because their
# tag is a build-time variable, not a pulled third-party version.
FIRST_PARTY_IMAGE_PREFIXES = ("nyxgpt-api:", "nyxgpt-web:")


def _read(path: Path) -> str:
    return path.read_text()


def test_no_latest_tag_in_pinned_files() -> None:
    for path in PINNED_FILES:
        text = _read(path)
        assert (
            ":latest" not in text
        ), f"{path.relative_to(REPO_ROOT)} still references a `:latest` image tag"


def test_docker_compose_third_party_images_are_pinned() -> None:
    compose = _read(REPO_ROOT / "docker-compose.yml")
    images = re.findall(r"^\s*image:\s*(\S+)\s*$", compose, re.MULTILINE)
    assert images, "expected at least one `image:` entry in docker-compose.yml"

    third_party = [img for img in images if not img.startswith(FIRST_PARTY_IMAGE_PREFIXES)]
    assert third_party, "expected at least one third-party image in docker-compose.yml"

    for image in third_party:
        assert ":" in image, f"image {image!r} in docker-compose.yml has no tag"
        name, _, tag = image.rpartition(":")
        assert tag != "latest", f"image {name!r} in docker-compose.yml is pinned to `latest`"
        assert name in EXPECTED_PINS, f"unexpected third-party image {name!r} in docker-compose.yml"
        assert (
            tag == EXPECTED_PINS[name]
        ), f"{name!r} pinned to {tag!r} in docker-compose.yml, expected {EXPECTED_PINS[name]!r}"


def test_terraform_third_party_images_are_pinned() -> None:
    main_tf = _read(REPO_ROOT / "terraform" / "main.tf")
    images = re.findall(r'resource\s+"docker_image"\s+"\w+"\s*\{\s*name\s*=\s*"([^"]+)"', main_tf)
    assert images, "expected at least one docker_image resource in terraform/main.tf"

    third_party = [img for img in images if not img.startswith(FIRST_PARTY_IMAGE_PREFIXES)]
    assert third_party, "expected at least one third-party docker_image in terraform/main.tf"

    for image in third_party:
        assert ":" in image, f"image {image!r} in terraform/main.tf has no tag"
        name, _, tag = image.rpartition(":")
        assert tag != "latest", f"image {name!r} in terraform/main.tf is pinned to `latest`"
        assert name in EXPECTED_PINS, f"unexpected third-party image {name!r} in terraform/main.tf"
        assert (
            tag == EXPECTED_PINS[name]
        ), f"{name!r} pinned to {tag!r} in terraform/main.tf, expected {EXPECTED_PINS[name]!r}"


def test_ops_cassandra_image_is_pinned() -> None:
    ops = _read(REPO_ROOT / "src" / "nyxgpt" / "ops.py")
    match = re.search(r'CASSANDRA_IMAGE\s*=\s*"([^"]+)"', ops)
    assert match, "expected a CASSANDRA_IMAGE constant in src/nyxgpt/ops.py"
    name, _, tag = match.group(1).rpartition(":")
    assert tag != "latest"
    assert tag == EXPECTED_PINS["cassandra"]


def test_cassandra_pin_matches_across_all_three_files() -> None:
    compose = _read(REPO_ROOT / "docker-compose.yml")
    main_tf = _read(REPO_ROOT / "terraform" / "main.tf")
    ops = _read(REPO_ROOT / "src" / "nyxgpt" / "ops.py")

    compose_tag = re.search(r"image:\s*cassandra:(\S+)", compose).group(1)
    terraform_tag = re.search(r'name\s*=\s*"cassandra:([^"]+)"', main_tf).group(1)
    ops_tag = re.search(r'CASSANDRA_IMAGE\s*=\s*"cassandra:([^"]+)"', ops).group(1)

    assert compose_tag == terraform_tag == ops_tag == EXPECTED_PINS["cassandra"]


def test_ollama_pin_matches_across_compose_and_terraform() -> None:
    compose = _read(REPO_ROOT / "docker-compose.yml")
    main_tf = _read(REPO_ROOT / "terraform" / "main.tf")

    compose_tag = re.search(r"image:\s*ollama/ollama:(\S+)", compose).group(1)
    terraform_tag = re.search(r'name\s*=\s*"ollama/ollama:([^"]+)"', main_tf).group(1)

    assert compose_tag == terraform_tag == EXPECTED_PINS["ollama/ollama"]

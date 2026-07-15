"""Regression test for docker/entrypoint.sh's api_key merge (issue #3177).

The entrypoint's sed pattern must merge NYXGPT_AUTH_API_KEY into config.ini
regardless of whether the shipped `api_key` line has no trailing space, a
trailing space, or an existing value.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"


def _run_entrypoint(tmp_path: Path, config_line: str, api_key: str) -> str:
    """Run the entrypoint's merge step against a config.ini seeded with `config_line`.

    Returns the resulting config.ini contents. The entrypoint's final `exec
    uvicorn ...` is expected to fail (uvicorn isn't on PATH in this test),
    which is fine -- the merge happens before that exec.
    """
    config_dir = tmp_path / ".nyxGPT"
    config_dir.mkdir()
    config_path = config_dir / "config.ini"
    config_path.write_text(f"[auth]\n{config_line}\nheader = X-API-Key\n")

    subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env={"HOME": str(tmp_path), "NYXGPT_AUTH_API_KEY": api_key, "PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )

    return config_path.read_text()


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_line",
    ["api_key =", "api_key = ", "api_key = stale-value"],
)
def test_entrypoint_merges_api_key_regardless_of_existing_line_format(tmp_path, config_line):
    result = _run_entrypoint(tmp_path, config_line, "super-secret-key")
    assert "api_key = super-secret-key" in result.splitlines()


@pytest.mark.unit
def test_entrypoint_leaves_config_untouched_when_env_var_unset(tmp_path):
    config_dir = tmp_path / ".nyxGPT"
    config_dir.mkdir()
    config_path = config_dir / "config.ini"
    config_path.write_text("[auth]\napi_key =\nheader = X-API-Key\n")

    subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )

    assert "api_key =" in config_path.read_text().splitlines()

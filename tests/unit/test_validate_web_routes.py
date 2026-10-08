"""The proxy-seam guard in `scripts/agents/validate-web-routes.sh` (#4136).

The script's original job was "does a `route.ts` exist for every frontend
`fetch('/api/v1/...')`". It answered that by stripping the query string before
looking anything up, which made a whole class of defect invisible: a page can
ask for a parameter its proxy never forwards, and no gate anywhere sees it.
The page tests mock the proxy route, the FastAPI tests call the backend
directly, and the parameter is dropped in between.

That is not hypothetical. The Infrastructure page fetched
`?probe_health=true&verify_host=true` while the route forwarded `probe_health`
alone, so the dashboard asked AWS to confirm a billing EC2 Mac Dedicated Host,
never asked, and reported "never confirmed at AWS" no matter how often it was
reloaded — with every suite green.

These tests run the real script against purpose-built trees, including the
negative control: the pre-fix route shape must FAIL, or the guard is not
evidence of anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "validate-web-routes.sh"


def _tree(root: Path, *, page_query: str, route_body: str) -> None:
    """A minimal repo-shaped tree: one page that fetches, one proxy route."""
    page = root / "web" / "src" / "app" / "admin" / "thing"
    page.mkdir(parents=True)
    (page / "page.tsx").write_text(
        "export default function Page() {\n"
        f"  fetch('/api/v1/cloud/deploy{page_query}', {{ cache: 'no-store' }});\n"
        "  return null;\n"
        "}\n"
    )
    route = root / "web" / "src" / "app" / "api" / "v1" / "cloud" / "deploy"
    route.mkdir(parents=True)
    (route / "route.ts").write_text(route_body)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


# The shape that shipped the defect: one parameter read, the other discarded.
PICKS_ONE = """
export const GET = async (request: Request) => {
  const probeHealth = new URL(request.url).searchParams.get("probe_health");
  return fetch(`/api/v1/cloud/deploy${probeHealth ? `?probe_health=1` : ""}`);
};
"""

# The fix: every parameter the caller may send is named.
PICKS_BOTH = """
export const GET = async (request: Request) => {
  const incoming = new URL(request.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const name of ["probe_health", "verify_host"]) {
    const value = incoming.get(name);
    if (value) forwarded.set(name, value);
  }
  return fetch(`/api/v1/cloud/deploy?${forwarded.toString()}`);
};
"""

# The other legitimate shape: forward the query string whole.
FORWARDS_ALL = """
export const GET = async (request: Request) => {
  const { search } = new URL(request.url);
  return fetch(`/api/v1/cloud/deploy${search}`);
};
"""


def test_a_dropped_parameter_fails_the_check(tmp_path: Path) -> None:
    """The negative control. Without this failing, nothing below is evidence."""
    _tree(tmp_path, page_query="?probe_health=true&verify_host=true", route_body=PICKS_ONE)

    result = _run(tmp_path)

    assert result.returncode == 1, result.stdout
    assert "Dropped query parameter" in result.stdout
    assert "verify_host" in result.stdout
    # The parameter that IS forwarded must not be reported — a guard that
    # flags everything gets switched off.
    assert "deploy?probe_health" not in result.stdout.replace("↳ forwards ?probe_health", "")


def test_an_enumerated_route_passes(tmp_path: Path) -> None:
    _tree(tmp_path, page_query="?probe_health=true&verify_host=true", route_body=PICKS_BOTH)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "forwards ?verify_host" in result.stdout


def test_a_route_forwarding_the_whole_query_string_passes(tmp_path: Path) -> None:
    """`admin/activity` is this shape. It carries every parameter by
    construction, so there is nothing for the guard to enumerate."""
    _tree(tmp_path, page_query="?limit=25&offset=50", route_body=FORWARDS_ALL)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "forwards the whole query string" in result.stdout


def test_a_missing_route_still_fails(tmp_path: Path) -> None:
    """The script's original job, unchanged by the parameter check."""
    _tree(tmp_path, page_query="", route_body=PICKS_BOTH)
    (tmp_path / "web/src/app/api/v1/cloud/deploy/route.ts").unlink()

    result = _run(tmp_path)

    assert result.returncode == 1, result.stdout
    assert "Missing route" in result.stdout


def test_a_templated_url_is_skipped_rather_than_guessed_at(tmp_path: Path) -> None:
    """A `${...}` path cannot be resolved statically, and reporting it would
    be a false positive on every dynamic route."""
    page = tmp_path / "web" / "src" / "app" / "admin"
    page.mkdir(parents=True)
    (page / "page.tsx").write_text("fetch('/api/v1/sessions/${name}?format=json');\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "Missing route" not in result.stdout


def test_the_repo_itself_passes() -> None:
    """The guard is only useful while the tree satisfies it."""
    repo = Path(__file__).resolve().parents[2]

    result = _run(repo)

    assert result.returncode == 0, result.stdout + result.stderr

"""Tests for the Support surface: packaged docs and the issue-form link (#3745).

A PyPI/Homebrew install has no repo checkout, so the docs a user reads have
to come out of the installed package -- these pin that they do, that the
links between documents resolve to in-app routes rather than checkout paths,
and that a slug from the URL cannot walk out of the packaged docs directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from nyxgpt import support
from nyxgpt.app import api, app
from nyxgpt.version import running_version

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_docs_markdown_file_is_packaged_and_listed():
    """The packaged set is the whole `docs/*.md` tree, not a curated subset."""
    on_disk = {p.stem for p in (_REPO_ROOT / "docs").glob("*.md")}
    listed = {doc["slug"] for doc in support.list_documents()}
    assert on_disk, "no docs/*.md found -- the fixture assumption is wrong"
    assert listed == on_disk


def test_docs_resolve_through_importlib_resources_not_the_checkout():
    """Resolution goes via `nyxgpt.resources`, the mechanism a wheel carries."""
    assert support.DOCS_RESOURCE_PACKAGE == "nyxgpt.resources"
    assert support.docs_dir().is_dir()
    # No module-level constant may point at a repo-relative path: that is
    # exactly what breaks on an install with no checkout.
    source = (_REPO_ROOT / "src" / "nyxgpt" / "support.py").read_text(encoding="utf-8")
    assert "parents[2]" not in source
    assert "__file__" not in source


def test_index_lists_title_and_summary_with_the_docs_index_first():
    documents = support.list_documents()
    assert documents[0]["slug"] == support.INDEX_SLUG
    assert all(doc["title"] for doc in documents)
    # Titles after the index read alphabetically.
    rest = [doc["title"].lower() for doc in documents[1:]]
    assert rest == sorted(rest)


def test_titles_and_summaries_come_from_the_document_body():
    assert support._title_of("# Configuration\n\nHow to configure.\n", "configuration") == (
        "Configuration"
    )
    # No heading at all falls back to the slug rather than showing nothing.
    assert support._title_of("no heading here\n", "rag") == "rag"
    assert support._summary_of("# T\n\nFirst para.\nStill first.\n\nSecond.\n") == (
        "First para. Still first."
    )


def test_render_rewrites_links_between_docs_to_in_app_routes():
    """No rendered link may stay relative -- only a checkout could resolve one."""
    for slug in (doc["slug"] for doc in support.list_documents()):
        soup = BeautifulSoup(support.render_document(slug)["html"], "html.parser")
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not isinstance(href, str) or not href:
                continue
            # Every surviving link is one of three absolute forms: an in-app
            # docs route, an in-page anchor, or a URL with a scheme. A bare
            # relative path (`configuration.md`, `../src/nyxgpt/ops.py`)
            # would 404 for a user who installed a wheel.
            assert href.startswith(
                (support.DOCS_ROUTE_PREFIX, "#", "http://", "https://", "mailto:")
            ), f"{slug}: unrewritten relative link {href!r}"


def test_render_preserves_anchors_and_marks_external_links():
    rendered = support.render_document(support.INDEX_SLUG)
    assert rendered["slug"] == support.INDEX_SLUG
    assert rendered["title"]
    assert "<" in rendered["html"]
    # Anything pointing off-box opens in a new tab, safely.
    if 'target="_blank"' in rendered["html"]:
        assert 'rel="noopener noreferrer"' in rendered["html"]


def test_render_link_rewriting_rules():
    assert support._rewrite_link("configuration.md") == "/support/docs/configuration"
    assert support._rewrite_link("./rag.md") == "/support/docs/rag"
    assert support._rewrite_link("security.md#tls") == "/support/docs/security#tls"
    # The root README is not part of the packaged tree; the index is what it
    # points into.
    assert support._rewrite_link("../README.md") == "/support/docs/README"
    # In-page anchors and absolute URLs are left exactly as they are.
    assert support._rewrite_link("#section") == "#section"
    assert support._rewrite_link("https://example.com/x") == "https://example.com/x"
    # A repo path that only a checkout has becomes the hosted copy. Assert the
    # *whole* URL: a prefix assertion passes even when the path is mangled.
    blob = f"{support.ISSUE_REPO_URL}/blob/{support.REPO_DEFAULT_BRANCH}"
    assert support._rewrite_link("../src/nyxgpt/ops.py") == f"{blob}/src/nyxgpt/ops.py"
    assert support._rewrite_link("scripts/agents/") == f"{blob}/scripts/agents/"
    assert support._rewrite_link("../../pyproject.toml") == f"{blob}/pyproject.toml"
    # A dotfile directory keeps its leading dot: stripping `./` as a character
    # set (rather than as a prefix) turned `.github` into `github`, a dead
    # link for the real `docs/portability-matrix.md` reference.
    assert (
        support._rewrite_link("../.github/workflows/linux-native-smoke.yml")
        == f"{blob}/.github/workflows/linux-native-smoke.yml"
    )


def test_render_never_drops_a_leading_dot_from_a_repo_path():
    """Tree-wide: no shipped doc renders a `.github/...` link as `github/...`.

    `docs/portability-matrix.md` links to `../.github/workflows/...`; with a
    character-set strip that shipped as a dead GitHub URL.
    """
    blob = f"{support.ISSUE_REPO_URL}/blob/{support.REPO_DEFAULT_BRANCH}"
    mangled = []
    for slug in (doc["slug"] for doc in support.list_documents()):
        soup = BeautifulSoup(support.render_document(slug)["html"], "html.parser")
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if isinstance(href, str) and href.startswith(f"{blob}/github/"):
                mangled.append(f"{slug}: {href}")
    assert not mangled, f"dotfile directory mangled in rendered links: {mangled}"


def test_rendered_html_carries_no_active_content():
    """Docs are trusted packaged content; the page still injects HTML, so strip it."""
    dirty = (
        "<p onclick='steal()'>hi</p><script>alert(1)</script>"
        "<style>body{}</style><a href='javascript:alert(1)'>x</a>"
    )
    cleaned = support._sanitize(dirty)
    assert "<script" not in cleaned
    assert "<style" not in cleaned
    assert "onclick" not in cleaned
    assert "javascript:" not in cleaned
    assert "hi" in cleaned


@pytest.mark.parametrize(
    "slug",
    [
        "../pyproject",
        "../../etc/passwd",
        "..",
        "sub/dir",
        "config uration",
        "",
        "nonexistent-document",
    ],
)
def test_bad_slugs_never_read_outside_the_packaged_docs(slug):
    with pytest.raises(support.DocumentNotFoundError):
        support.render_document(slug)


def test_issue_form_url_prefills_environment_and_declares_no_label():
    url = support.issue_form_url(
        {"version": "3.0.0", "platform": "Darwin 24.5.0 (arm64)", "python": "3.11.9"}
    )
    assert url.startswith(f"{support.ISSUE_REPO_URL}/issues/new?")
    assert "template=support.yml" in url
    assert "version=3.0.0" in url
    assert "Darwin" in url
    # The template declares `Support` itself. A `labels=` parameter would be
    # silently dropped for a filer without write access -- exactly the filer
    # this form exists for -- so it must not be sent.
    assert "labels=" not in url


def test_environment_summary_reports_the_running_version():
    env = support.environment_summary()
    assert env["version"] == running_version()
    assert env["platform"]
    assert env["python"]


def test_docs_index_endpoint():
    client = TestClient(app)
    res = client.get("/api/v1/support/docs")
    assert res.status_code == 200
    documents = res.json()["documents"]
    assert documents[0]["slug"] == support.INDEX_SLUG


def test_document_endpoint_renders_html():
    client = TestClient(app)
    res = client.get(f"/api/v1/support/docs/{support.INDEX_SLUG}")
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == support.INDEX_SLUG
    assert "<" in body["html"]


def test_unknown_document_is_a_404_not_a_500():
    client = TestClient(app)
    assert client.get("/api/v1/support/docs/nope-not-a-doc").status_code == 404


def test_context_endpoint_carries_the_issue_link_and_network_caveat():
    client = TestClient(app)
    res = client.get("/api/v1/support/context")
    assert res.status_code == 200
    body = res.json()
    assert body["issue_form_url"].startswith(support.ISSUE_REPO_URL)
    assert body["environment"]["version"] == running_version()
    assert body["docs_route"] == support.DOCS_ROUTE_PREFIX
    # Docs work offline; filing does not, and the UI must be able to say so.
    assert body["requires_network"] is True
    assert "GitHub account" in body["network_note"]


def test_support_surface_is_read_only():
    """nyxGPT never files an issue for the user -- there is nothing to POST to."""
    # The versioned router, not `app.routes`: the app includes it lazily, so
    # the concrete endpoints only exist on `api`.
    support_routes = [r for r in api.routes if "/support" in getattr(r, "path", "")]
    assert support_routes
    for route in support_routes:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD", "OPTIONS"}

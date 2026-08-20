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


#: Documents #3809 removed from the artifact: how this repository builds
#: itself, not how to use nyxGPT. Named rather than derived, so re-adding one
#: to the wheel has to be a deliberate edit here too.
_NOT_PRODUCT_DOCS = {
    "KNOWN_LIMITATIONS",
    "acceptance-drain-gate",
    "adding-api-endpoints",
    "agent-comment-tokens",
    "agent-smoke",
    "cloud-artifact-smoke",
    "development",
    "file-lock-audit",
    "github-tokens",
    "how-this-project-is-run",
    "live-verification-ci",
    "portability-matrix",
    "reviewable-head-gate",
    "security-scanning-ci",
    "sprint-autopilot",
    "testing",
}


def test_the_packaged_set_is_exactly_the_grouped_selection():
    """Packaged files and `DOC_SECTIONS` hold the same slugs, both ways (#3809).

    This is the regression gate the issue asks for. A new process document
    dropped into `docs/` is not symlinked into `nyxgpt/resources/docs/`, so
    it fails nothing and ships nowhere; a new *product* document that is
    packaged but placed in no section fails here rather than appearing in the
    viewer ungrouped.
    """
    packaged = support.packaged_slugs()
    grouped = set(support.PACKAGED_SLUGS)
    assert packaged == grouped, (
        f"packaged but ungrouped: {sorted(packaged - grouped)}; "
        f"grouped but not packaged: {sorted(grouped - packaged)}"
    )
    # No slug listed twice across sections -- the index would render it twice.
    assert len(support.PACKAGED_SLUGS) == len(grouped)


def test_process_and_contributor_docs_are_absent_from_the_artifact():
    """Not merely hidden in the UI: the files are not in the package at all."""
    packaged = support.packaged_slugs()
    assert not (packaged & _NOT_PRODUCT_DOCS), sorted(packaged & _NOT_PRODUCT_DOCS)
    listed = {doc["slug"] for doc in support.list_documents()}
    assert not (listed & _NOT_PRODUCT_DOCS)
    # `nyxgpt/resources/docs/` is the packaged directory itself -- assert on
    # the checked-in symlinks, which is what a wheel build dereferences.
    resource_docs = _REPO_ROOT / "src" / "nyxgpt" / "resources" / "docs"
    assert {p.stem for p in resource_docs.glob("*.md")} == set(support.PACKAGED_SLUGS)


def test_excluded_docs_stay_in_the_repository():
    """Removing them from the artifact must not remove them from the repo.

    The agent loop and `CLAUDE.md`'s bootstrap read these from `docs/`.
    """
    on_disk = {p.stem for p in (_REPO_ROOT / "docs").glob("*.md")}
    assert on_disk, "no docs/*.md found -- the fixture assumption is wrong"
    missing = _NOT_PRODUCT_DOCS - on_disk
    assert not missing, f"excluded docs deleted from the repository: {sorted(missing)}"
    # Every repo doc is either packaged product documentation or a named
    # exclusion -- a new document cannot land in neither and go unnoticed.
    unclassified = on_disk - set(support.PACKAGED_SLUGS) - _NOT_PRODUCT_DOCS
    assert not unclassified, (
        f"docs/*.md neither packaged nor listed as non-product: {sorted(unclassified)}. "
        "Symlink it into src/nyxgpt/resources/docs/ and add it to "
        "support.DOC_SECTIONS, or add it to _NOT_PRODUCT_DOCS."
    )


def test_sections_are_ordered_deliberately_and_never_empty():
    """Install before use before reference -- and no flat alphabetical list."""
    sections = support.list_sections()
    assert [section["title"] for section in sections] == [
        "Getting started",
        "Using nyxGPT",
        "Configuration",
        "Operating",
        "Reference",
        "Help",
    ]
    for section in sections:
        assert section["documents"], f"empty section rendered: {section['title']}"
        for doc in section["documents"]:
            assert doc["title"]
    # The install docs come before the reference ones, not after them.
    flat = [doc["slug"] for doc in support.list_documents()]
    assert flat.index("homebrew") < flat.index("cli")


def test_index_endpoint_serves_the_groups_not_a_flat_alphabetical_list():
    client = TestClient(app)
    body = client.get("/api/v1/support/docs").json()
    assert [section["title"] for section in body["sections"]] == [
        title for title, _slugs in support.DOC_SECTIONS
    ]
    # `documents` stays available, and is the same set in the same order.
    assert [doc["slug"] for doc in body["documents"]] == list(support.PACKAGED_SLUGS)
    titles = [doc["title"] for doc in body["documents"]]
    assert titles != sorted(titles, key=str.lower), "the index is still alphabetical"


def test_no_packaged_doc_renders_a_dead_link_into_an_excluded_doc():
    """Criterion: remaining docs must not link into removed ones.

    A relative `.md` link is rewritten to the hosted repository copy when its
    target is not packaged, so a reference that survives in prose lands on a
    real page instead of a 404 inside the viewer.
    """
    dead = []
    for slug in support.PACKAGED_SLUGS:
        soup = BeautifulSoup(support.render_document(slug)["html"], "html.parser")
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not isinstance(href, str) or not href.startswith(support.DOCS_ROUTE_PREFIX):
                continue
            target = href[len(support.DOCS_ROUTE_PREFIX) :].lstrip("/").split("#")[0]
            if target and target not in support.PACKAGED_SLUGS:
                dead.append(f"{slug} -> {href}")
    assert not dead, f"in-app links to documents that are not packaged: {dead}"


def test_excluded_docs_resolve_to_their_hosted_copy():
    blob = f"{support.ISSUE_REPO_URL}/blob/{support.REPO_DEFAULT_BRANCH}/docs"
    assert support._rewrite_link("github-tokens.md") == f"{blob}/github-tokens.md"
    assert (
        support._rewrite_link("portability-matrix.md#the-matrix")
        == f"{blob}/portability-matrix.md#the-matrix"
    )
    # A packaged sibling still browses in-app.
    assert support._rewrite_link("ops.md") == "/support/docs/ops"


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
    # Ordering is the manifest's, not the filesystem's or the alphabet's.
    assert [doc["slug"] for doc in documents] == list(support.PACKAGED_SLUGS)


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


def test_issue_form_url_prefills_the_ticket_type_the_filer_chose():
    """The classification happens in nyxGPT, not on GitHub (#3811)."""
    env = {"version": "3.0.0", "platform": "Darwin 24.5.0 (arm64)", "python": "3.11.9"}
    url = support.issue_form_url(env, "Feature Request")
    assert "ticket_type=Feature+Request" in url
    # Still a plain link to GitHub's form: prefilling is the only thing
    # nyxGPT does. It does not file anything on the user's behalf.
    assert url.startswith(f"{support.ISSUE_REPO_URL}/issues/new?")
    assert "labels=" not in url


def test_an_unknown_ticket_type_is_refused_rather_than_passed_through():
    """GitHub ignores a prefill matching no option -- silently.

    Passing one through would demote a required question to an unanswered
    one with nothing anywhere saying why, which is the same failure shape as
    the label that did not exist (#3810).
    """
    with pytest.raises(ValueError):
        support.issue_form_url(
            {"version": "3.0.0", "platform": "Linux", "python": "3.11.9"},
            "Production Defect",
        )


def test_every_ticket_type_gets_its_own_prefilled_link():
    env = {"version": "3.0.0", "platform": "Linux 6.1 (x86_64)", "python": "3.11.9"}
    options = support.ticket_type_options(env)
    assert [option["value"] for option in options] == list(support.TICKET_TYPES)
    for option in options:
        assert option["description"]
        assert "ticket_type=" in option["url"]
        assert "version=3.0.0" in option["url"]


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
    # The Support menu builds one filing entry per ticket type from this, so
    # the filer classifies the ticket before leaving nyxGPT (#3811).
    assert [option["value"] for option in body["ticket_types"]] == list(support.TICKET_TYPES)


def test_reading_the_docs_stays_read_only():
    """The docs half of Support writes nothing, whatever filing does.

    This used to assert the *whole* Support surface was read-only, on the
    reasoning that nyxGPT never files an issue for the user. The owner
    rejected that design in acceptance (#3811): filing is now a POST, and
    `tests/unit/test_support_intake.py` pins that it is the only one. Docs
    are unchanged -- rendering a packaged Markdown file has no business
    writing anything.
    """
    # The versioned router, not `app.routes`: the app includes it lazily, so
    # the concrete endpoints only exist on `api`.
    docs_routes = [r for r in api.routes if "/support/docs" in getattr(r, "path", "")]
    assert docs_routes
    for route in docs_routes:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD", "OPTIONS"}

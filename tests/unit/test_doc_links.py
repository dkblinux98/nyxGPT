"""`nyxgpt.doc_links` -- where a message points when it names a document (#4182).

The behaviour these pin is the repo-less requirement applied to prose: a
pointer the product prints must resolve to something a machine with no
checkout can open. The companion guard
(`tests/unit/test_no_repo_relative_user_paths.py`) fails the build on a
message that still names a repository path; this module pins what the
replacement actually produces.
"""

from __future__ import annotations

import pytest

from nyxgpt import doc_links, support

pytestmark = pytest.mark.unit


def test_a_packaged_document_resolves_to_the_in_app_route_and_the_hosted_copy():
    """Both, in that order, and the order is the point: an operator whose
    stack is up reads it locally with no network, and the hosted URL is what
    works when it is not."""
    pointer = doc_links.see_doc("docs/ops.md")
    assert "/support/docs/ops" in pointer
    assert doc_links.doc_url("docs/ops.md") in pointer
    assert pointer.index("/support/docs/ops") < pointer.index("https://")


def test_an_unpackaged_document_names_only_the_hosted_copy():
    """`docs/github-tokens.md` is a process document #3809 deliberately kept
    out of the artifact, so the in-app route would be a 404 -- which is not a
    graceful degradation, it is a second dead pointer."""
    assert "github-tokens" not in support.PACKAGED_SLUGS
    pointer = doc_links.see_doc("docs/github-tokens.md")
    assert "/support/docs/" not in pointer
    assert pointer.endswith("/docs/github-tokens.md")


def test_product_management_is_never_an_in_app_route():
    """It is not shipped in any form, which is what made
    `product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md` the worst of the
    reported pointers: no install carries it anywhere."""
    url = doc_links.doc_url("product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md")
    assert doc_links.doc_route("product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md") == ""
    assert url.endswith("/product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md")


def test_an_anchor_survives_both_renderings():
    assert doc_links.doc_route("docs/ops.md#tls").endswith("/support/docs/ops#tls")
    assert doc_links.doc_url("docs/ops.md#tls").endswith("/docs/ops.md#tls")


def test_any_repository_path_resolves_not_only_documents():
    """`portability.py` cites workflows and source modules, and a reader of
    that report has no checkout either."""
    assert doc_links.doc_url(".github/workflows/macos-brew-smoke.yml").endswith(
        "/blob/master/.github/workflows/macos-brew-smoke.yml"
    )


def test_the_hosted_url_names_the_default_branch_not_a_release_line():
    """A release branch is deleted when its line retires, which would turn
    every pointer in the product into a 404 at the next roll -- the same time
    bomb `test_no_hardcoded_release_version.py` exists to stop."""
    assert doc_links.REPO_DEFAULT_BRANCH == "master"
    assert "/blob/master/" in doc_links.doc_url("docs/ops.md")


def test_support_reads_its_repo_coordinates_from_here():
    """One source for "which repository, which branch" (#4182): the support
    intake files tickets into the same repository these links point into, and
    two copies is two places for the next divergence."""
    assert support.ISSUE_REPO_URL == doc_links.REPO_URL
    assert support.REPO_DEFAULT_BRANCH == doc_links.REPO_DEFAULT_BRANCH
    assert support.DOCS_ROUTE_PREFIX == doc_links.DOCS_ROUTE_PREFIX

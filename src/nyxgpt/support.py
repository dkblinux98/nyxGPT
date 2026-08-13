"""Packaged documentation and the support-report link behind the web UI's Support menu (#3745).

An install from PyPI or Homebrew never has a repo checkout, so `docs/*.md`
ships inside the wheel as package data (`nyxgpt.resources/docs`, the same
mechanism #3621 gave the ops layer's runtime data) and is resolved here via
`importlib.resources` -- never relative to a source tree. The packaged
snapshot therefore matches the installed version by construction: the docs
a user reads under Support -> Docs are the docs that shipped with the code
that is running.

Two surfaces live here:

* **Docs** -- an index of the packaged Markdown documents plus a rendered
  HTML view of one document, with the links *between* docs rewritten to
  in-app routes so the tree browses as a unit offline.
* **File an Issue** -- the GitHub issue-form URL, prefilled with the running
  version and this machine's platform so a report carries its environment
  without the user having to look either up. This is a link, not an API
  call: nyxGPT never files an issue on the user's behalf, and the form
  itself needs internet and a GitHub account.
"""

from __future__ import annotations

import importlib.resources
import platform
import re
from importlib.resources.abc import Traversable
from typing import Any
from urllib.parse import urlencode

import markdown as markdown_lib
from bs4 import BeautifulSoup

from nyxgpt.version import running_version

#: Where the docs tree lives inside the installed package. `nyxgpt.resources`
#: holds a `docs` symlink back to the canonical top-level `docs/`, which
#: setuptools dereferences when it builds the wheel -- one place to edit each
#: document, real file content in the artifact.
DOCS_RESOURCE_PACKAGE = "nyxgpt.resources"
DOCS_SUBDIR = "docs"

#: `docs/README.md` is the index into the rest of the tree, so it leads the
#: list and is where the root README's pointer lands.
INDEX_SLUG = "README"

ISSUE_REPO_URL = "https://github.com/dkblinux98/nyxGPT"
ISSUE_FORM_TEMPLATE = "support.yml"
REPO_DEFAULT_BRANCH = "master"

#: The in-app route the docs viewer serves documents from; links between
#: packaged documents are rewritten onto it.
DOCS_ROUTE_PREFIX = "/support/docs"

#: A slug names one file directly inside the packaged docs directory. Anchored
#: and free of `/`, `\` and `..` by construction, so a slug taken from a URL
#: cannot traverse out of that directory.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: A relative link to a sibling document: `configuration.md`, `./rag.md`,
#: `security.md#tls`. The `(?!\w+:)` guard keeps absolute URLs out.
_RELATIVE_DOC_LINK_RE = re.compile(
    r"^(?!\w+:)(?:\./)?(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]*)\.md(?P<anchor>#.*)?$"
)

#: Rendered docs are injected into the page as HTML. The content is trusted
#: (it ships in the wheel), but rendering it as active content would turn any
#: future docs edit into a scripting surface, so strip it unconditionally.
_STRIPPED_TAGS = ("script", "style", "iframe", "object", "embed", "form")
_EVENT_ATTR_PREFIX = "on"
_UNSAFE_URL_SCHEMES = ("javascript:", "data:text/html", "vbscript:")


class DocumentNotFoundError(LookupError):
    """Raised when a slug names no packaged document."""


def docs_dir() -> Traversable:
    """Return the packaged docs directory as an `importlib.resources` traversable."""
    return importlib.resources.files(DOCS_RESOURCE_PACKAGE) / DOCS_SUBDIR


def _document_path(slug: str) -> Traversable:
    """Resolve `slug` to a packaged document, rejecting anything that isn't one.

    Raises:
        DocumentNotFoundError: the slug is malformed, or names no `.md` file
            directly inside the packaged docs directory.
    """
    if not _SLUG_RE.match(slug):
        raise DocumentNotFoundError(f"Unknown document: {slug!r}")
    path = docs_dir() / f"{slug}.md"
    if not path.is_file():
        raise DocumentNotFoundError(f"Unknown document: {slug!r}")
    return path


def _title_of(text: str, slug: str) -> str:
    """Return the document's first level-1 heading, falling back to its slug."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug


def _summary_of(text: str) -> str:
    """Return the document's first prose paragraph, condensed to one line."""
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        # Skip the title, and any structural opener (list, table, fence,
        # blockquote, badge line) that isn't the prose we want to show.
        if stripped.startswith(("#", "-", "*", "|", "```", ">", "[!")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)


def list_documents() -> list[dict[str, str]]:
    """Return every packaged document as `{slug, title, summary}`.

    The docs index (`README`) sorts first -- it is the map into the rest --
    and the remainder sort by title so the menu reads alphabetically.
    """
    documents = []
    for entry in docs_dir().iterdir():
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        slug = entry.name[: -len(".md")]
        text = entry.read_text(encoding="utf-8")
        documents.append(
            {"slug": slug, "title": _title_of(text, slug), "summary": _summary_of(text)}
        )
    documents.sort(key=lambda doc: (doc["slug"] != INDEX_SLUG, doc["title"].lower()))
    return documents


def _rewrite_link(href: str) -> str:
    """Resolve a Markdown link so it works from an install with no checkout.

    Three cases:

    * a sibling document (`configuration.md#tls`) becomes the in-app route
      `/support/docs/configuration#tls`, so the tree browses as a unit;
    * the root README (`../README.md`) becomes the docs index -- it is not
      part of the packaged docs tree, and the index is what it points into;
    * anything else (absolute URL, in-page anchor, or a path pointing at
      source files that only a checkout has) is sent to GitHub on the
      default branch, which is where that file actually exists for a user
      who installed an artifact.
    """
    if not href or href.startswith("#"):
        return href
    if href in ("../README.md", "../../README.md"):
        return f"{DOCS_ROUTE_PREFIX}/{INDEX_SLUG}"
    match = _RELATIVE_DOC_LINK_RE.match(href)
    if match:
        return f"{DOCS_ROUTE_PREFIX}/{match.group('slug')}{match.group('anchor') or ''}"
    if re.match(r"^\w+:", href) or href.startswith("//"):
        return href
    # A repo-relative path to something that isn't a packaged doc
    # (`../src/nyxgpt/ops.py`, `scripts/agents/`): only a checkout has it, so
    # point at the hosted copy instead of emitting a dead relative link.
    return f"{ISSUE_REPO_URL}/blob/{REPO_DEFAULT_BRANCH}/{href.lstrip('./')}"


def _sanitize(html: str) -> str:
    """Drop active content from rendered docs HTML (see `_STRIPPED_TAGS`)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIPPED_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith(_EVENT_ATTR_PREFIX):
                del tag[attr]
        for attr in ("href", "src"):
            value = tag.get(attr)
            if isinstance(value, str):
                collapsed = "".join(value.split()).lower()
                if collapsed.startswith(_UNSAFE_URL_SCHEMES):
                    del tag[attr]
    return str(soup)


def render_document(slug: str) -> dict[str, str]:
    """Render one packaged document to HTML for the Support -> Docs viewer.

    Returns `{slug, title, html}`. Links to sibling documents are rewritten
    onto `DOCS_ROUTE_PREFIX`; links out to the web keep their targets and are
    marked to open in a new tab.

    Raises:
        DocumentNotFoundError: `slug` names no packaged document.
    """
    text = _document_path(slug).read_text(encoding="utf-8")
    html = markdown_lib.markdown(text, extensions=["fenced_code", "tables", "toc"])

    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        rewritten = _rewrite_link(href)
        anchor["href"] = rewritten
        if rewritten.startswith(("http://", "https://")):
            anchor["target"] = "_blank"
            anchor["rel"] = "noopener noreferrer"

    return {
        "slug": slug,
        "title": _title_of(text, slug),
        "html": _sanitize(str(soup)),
    }


def environment_summary() -> dict[str, str]:
    """Describe what is running, for prefilling a support report.

    The version comes from installed package metadata (`running_version`), so
    it is the version actually running rather than anything configured.
    """
    return {
        "version": running_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
    }


def issue_form_url(environment: dict[str, str] | None = None) -> str:
    """Return the GitHub issue-form URL with environment details prefilled.

    Prefill happens through query parameters keyed by the form's field ids
    (`.github/ISSUE_TEMPLATE/support.yml`), which is GitHub's supported
    mechanism for issue forms. The template declares the `Support` label
    itself, so no label parameter is needed -- and none is sent, since a
    label parameter would silently apply nothing for a filer without write
    access, which is exactly the filer this form exists for.
    """
    env = environment or environment_summary()
    params = urlencode(
        {
            "template": ISSUE_FORM_TEMPLATE,
            "version": env["version"],
            "platform": f"{env['platform']}, Python {env['python']}",
        }
    )
    return f"{ISSUE_REPO_URL}/issues/new?{params}"


def support_context() -> dict[str, Any]:
    """Return everything the Support menu needs: environment plus the issue link."""
    env = environment_summary()
    return {
        "environment": env,
        "issue_form_url": issue_form_url(env),
        "docs_route": DOCS_ROUTE_PREFIX,
        # Docs render offline; filing does not. The UI says so rather than
        # letting the link fail mysteriously on an air-gapped install.
        "requires_network": True,
        "network_note": (
            "Filing an issue opens GitHub in your browser and needs internet "
            "access and a GitHub account. The documentation above is packaged "
            "with nyxGPT and works offline."
        ),
    }

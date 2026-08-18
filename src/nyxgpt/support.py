"""Packaged documentation and the support-report link behind the web UI's Support menu (#3745).

An install from PyPI or Homebrew never has a repo checkout, so the product
documentation ships inside the wheel as package data
(`nyxgpt.resources/docs`, the same mechanism #3621 gave the ops layer's
runtime data) and is resolved here via `importlib.resources` -- never
relative to a source tree. The packaged snapshot therefore matches the
installed version by construction: the docs a user reads under Support ->
Docs are the docs that shipped with the code that is running.

What ships is a named selection, not the whole `docs/` tree: `DOC_SECTIONS`
below lists the product documents, grouped as the viewer presents them.
Everything about how *this repository* builds itself -- the agent loop, CI
process, contributor setup -- stays in the repository and out of the
artifact (#3809).

Two surfaces live here:

* **Docs** -- an index of the packaged Markdown documents plus a rendered
  HTML view of one document, with the links *between* docs rewritten to
  in-app routes so the tree browses as a unit offline.
* **File an Issue** -- the GitHub issue-form URL, prefilled with the ticket
  type the filer picked in nyxGPT plus the running version and this
  machine's platform, so a report carries its environment and its
  classification without the user having to look either up. This is a link,
  not an API call: nyxGPT never files an issue on the user's behalf, and the
  form itself needs internet and a GitHub account.
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

#: Where the docs tree lives inside the installed package.
#: `nyxgpt/resources/docs/` holds one symlink per *product* document back to
#: the canonical top-level `docs/`, which setuptools dereferences when it
#: builds the wheel -- one place to edit each document, real file content in
#: the artifact, and the directory listing is itself the selection (#3809).
DOCS_RESOURCE_PACKAGE = "nyxgpt.resources"
DOCS_SUBDIR = "docs"

#: `docs/README.md` is the index into the rest of the tree, so it leads the
#: list and is where the root README's pointer lands.
INDEX_SLUG = "README"

#: The product documentation, grouped for the Support -> Docs index (#3809).
#:
#: This is the selection, expressed as data rather than inferred from
#: filenames: `docs/` also holds the documents about how *this repository*
#: builds itself -- the agent loop, CI process, contributor setup, this
#: project's own GitHub tokens -- and none of that is product help. Those
#: documents stay in the repository (the agent loop and `CLAUDE.md`'s
#: bootstrap read them from there); they are simply not symlinked into
#: `nyxgpt/resources/docs/`, so they are absent from the wheel rather than
#: shipped and filtered.
#:
#: Two invariants, both pinned by `tests/unit/test_support_docs.py`:
#:
#: * this list and the packaged directory hold exactly the same slugs, so a
#:   newly added process document cannot appear in the viewer, and a newly
#:   added product document cannot appear ungrouped;
#: * section order is deliberate -- install, then use, then configure,
#:   operate, and look things up -- not alphabetical.
DOC_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Getting started",
        (INDEX_SLUG, "homebrew", "systemd", "docker-compose", "kubernetes", "terraform", "cloud"),
    ),
    ("Using nyxGPT", ("sessions", "rag", "ui", "service-worker-pwa")),
    ("Configuration", ("configuration", "session-storage", "security")),
    ("Operating", ("ops", "self-healing", "alerting", "performance", "deployment-checklist")),
    ("Reference", ("cli", "api", "architecture")),
    ("Help", ("troubleshooting",)),
)

#: Every packaged slug, in the order the index presents them.
PACKAGED_SLUGS: tuple[str, ...] = tuple(slug for _title, slugs in DOC_SECTIONS for slug in slugs)

#: Membership test for link rewriting. Static (from the manifest) rather than
#: a directory read, so rendering one document does not stat the docs tree
#: once per link.
_PACKAGED_SLUG_SET = frozenset(PACKAGED_SLUGS)

ISSUE_REPO_URL = "https://github.com/dkblinux98/nyxGPT"
ISSUE_FORM_TEMPLATE = "support.yml"
REPO_DEFAULT_BRANCH = "master"

#: What kind of ticket this is, chosen by the filer in the nyxGPT UI (#3811).
#: These are the `Ticket Type` options on the Support project, mirrored here
#: because the filer picks one *before* leaving nyxGPT -- the value travels as
#: a prefill for the form's `ticket_type` dropdown and lands in the issue
#: body, where the owner reads it when setting the project field. They are
#: NOT labels: only `Support` is, and only `Support` routes anything.
#:
#: Must stay in step with the dropdown options in
#: `.github/ISSUE_TEMPLATE/support.yml` -- GitHub ignores a prefill value
#: that is not one of the declared options, so a drift here degrades to an
#: unanswered required field rather than a wrong answer.
TICKET_TYPES: tuple[str, ...] = ("Bug Found", "Feature Request", "Question")

#: What each type means, in the filer's terms rather than the project's.
TICKET_TYPE_DESCRIPTIONS: dict[str, str] = {
    "Bug Found": "Something is broken or behaving wrongly",
    "Feature Request": "Something nyxGPT should be able to do",
    "Question": "How do I …? / Is this supposed to …?",
}

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

#: Leading `./` / `../` segments on a repo-relative link, stripped as a prefix
#: before the path is appended to the hosted repo URL. This must not be a
#: `str.lstrip('./')`: that strips a *character set*, so `../.github/...`
#: would lose the dot of `.github` and produce a dead link.
_RELATIVE_PREFIX_RE = re.compile(r"^(?:\.\.?/)+")

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


def packaged_slugs() -> set[str]:
    """Return the slugs actually present in the packaged docs directory.

    The manifest says what *should* ship; this says what did. They are
    asserted equal by the packaging test -- reading the directory here keeps
    the runtime honest if a build ever drops a file.
    """
    return {
        entry.name[: -len(".md")]
        for entry in docs_dir().iterdir()
        if entry.is_file() and entry.name.endswith(".md")
    }


def _summarize(slug: str) -> dict[str, str] | None:
    """Return `{slug, title, summary}` for one packaged document, or None."""
    path = docs_dir() / f"{slug}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return {"slug": slug, "title": _title_of(text, slug), "summary": _summary_of(text)}


def list_sections() -> list[dict[str, Any]]:
    """Return the packaged documents grouped into `DOC_SECTIONS` order.

    `[{title, documents: [{slug, title, summary}, ...]}, ...]`. A section
    whose documents are all missing from the package is dropped rather than
    rendered empty.
    """
    sections: list[dict[str, Any]] = []
    for title, slugs in DOC_SECTIONS:
        documents = [doc for doc in (_summarize(slug) for slug in slugs) if doc is not None]
        if documents:
            sections.append({"title": title, "documents": documents})
    return sections


def list_documents() -> list[dict[str, str]]:
    """Return every packaged document as `{slug, title, summary}`, flat.

    Manifest order, so the docs index (`README`) still leads. `list_sections`
    is what the viewer renders; this stays for callers that just want the
    set of documents.
    """
    return [doc for section in list_sections() for doc in section["documents"]]


def _rewrite_link(href: str) -> str:
    """Resolve a Markdown link so it works from an install with no checkout.

    Three cases:

    * a sibling *packaged* document (`configuration.md#tls`) becomes the
      in-app route `/support/docs/configuration#tls`, so the tree browses as
      a unit;
    * a sibling document that is **not** packaged -- the process and
      contributor docs #3809 kept out of the artifact -- goes to the hosted
      copy under `docs/`, which is where it still lives. An in-app route
      would be a 404 for the reader;
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
        slug = match.group("slug")
        anchor = match.group("anchor") or ""
        if slug in _PACKAGED_SLUG_SET:
            return f"{DOCS_ROUTE_PREFIX}/{slug}{anchor}"
        blob = f"{ISSUE_REPO_URL}/blob/{REPO_DEFAULT_BRANCH}/{DOCS_SUBDIR}/{slug}.md"
        return f"{blob}{anchor}"
    if re.match(r"^\w+:", href) or href.startswith("//"):
        return href
    # A repo-relative path to something that isn't a packaged doc
    # (`../src/nyxgpt/ops.py`, `scripts/agents/`): only a checkout has it, so
    # point at the hosted copy instead of emitting a dead relative link.
    relative = _RELATIVE_PREFIX_RE.sub("", href)
    return f"{ISSUE_REPO_URL}/blob/{REPO_DEFAULT_BRANCH}/{relative}"


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


def issue_form_url(
    environment: dict[str, str] | None = None,
    ticket_type: str | None = None,
) -> str:
    """Return the GitHub issue-form URL with the report's details prefilled.

    Prefill happens through query parameters keyed by the form's field ids
    (`.github/ISSUE_TEMPLATE/support.yml`), which is GitHub's supported
    mechanism for issue forms. The template declares the `Support` label
    itself, so no label parameter is needed -- and none is sent, since a
    label parameter would silently apply nothing for a filer without write
    access, which is exactly the filer this form exists for.

    `ticket_type`, when given, prefills the form's `ticket_type` dropdown so
    the choice the filer already made in nyxGPT is not asked again (#3811).

    Raises:
        ValueError: `ticket_type` is not one of `TICKET_TYPES`. Passing an
            unrecognized value through would be worse than refusing it:
            GitHub ignores an unmatched dropdown prefill, so the filer would
            silently be asked to choose again.
    """
    env = environment or environment_summary()
    params: dict[str, str] = {
        "template": ISSUE_FORM_TEMPLATE,
        "version": env["version"],
        "platform": f"{env['platform']}, Python {env['python']}",
    }
    if ticket_type is not None:
        if ticket_type not in TICKET_TYPES:
            raise ValueError(f"Unknown ticket type: {ticket_type!r}")
        params["ticket_type"] = ticket_type
    return f"{ISSUE_REPO_URL}/issues/new?{urlencode(params)}"


def ticket_type_options(environment: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Return one prefilled filing link per ticket type, for the Support menu.

    The filer chooses what kind of ticket this is *in nyxGPT* rather than on
    GitHub, which is what gets the type recorded at all: the Support project
    types tickets with a project field, and nothing maps a form answer onto
    one automatically (#3811).
    """
    env = environment or environment_summary()
    return [
        {
            "value": ticket_type,
            "description": TICKET_TYPE_DESCRIPTIONS[ticket_type],
            "url": issue_form_url(env, ticket_type),
        }
        for ticket_type in TICKET_TYPES
    ]


def support_context() -> dict[str, Any]:
    """Return everything the Support menu needs: environment plus the issue links."""
    env = environment_summary()
    return {
        "environment": env,
        # Kept as the untyped entry point: a filer who reaches the form some
        # other way still gets version/platform prefilled and answers the
        # dropdown on GitHub.
        "issue_form_url": issue_form_url(env),
        "ticket_types": ticket_type_options(env),
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

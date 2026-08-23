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
* **File an Issue** -- an intake nyxGPT files *itself*. The filer answers
  the questions on a nyxGPT page (`/support/new`, served by this product's
  own web interface), `submit_ticket` creates the issue through the GitHub
  API with the environment already filled in, and the next page they see is
  the ticket that was created. The GitHub compose page is not on that path
  at any point, and the Support menu entry that reaches the page is a plain
  link -- it asks nothing and decides nothing, so there is no runtime state
  that can route it somewhere else (#3811 re-test).

**How the credential question was answered (#3811, ledger D-034, closing
Q-006's first half).** Creating
an issue needs a GitHub credential, and an install has one only when
`[github] pat` is configured. So there are two cases and they are not
symmetric:

* **configured** -- the owner's install, and any operator's -- the ticket is
  filed from the running install and the filer never leaves nyxGPT. This is
  the surface the product is built around, and the one the acceptance
  criteria describe.
* **not configured** -- the only case the product genuinely cannot file for.
  `issue_form_url` still builds GitHub's prefilled form, and the intake page
  explains the situation and offers that form as a link, because a support
  feature that answers "you cannot report this" is worse than one that hands
  over. The filer clicks it or does not: the fallback is never a redirect,
  which is what made the earlier version fail acceptance -- every degraded
  path took the filer to github.com without asking. A hosted intake that
  would remove even this case is the owner's call and stays open; nothing
  here forecloses it.

Both paths apply the same `Support` label, which is the routing key: the
Support project auto-adds `is:issue is:open label:Support`, and the agent
loop skips anything carrying it (`scripts/agents/lib/support_label.py`). On
the path nyxGPT files itself the label is *verified on the created issue*
rather than assumed -- GitHub drops `labels` from an issue created by a
token without push access, and it does it silently, which is the exact shape
of the #3810 leak.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
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

#: Where support tickets go. This is the *product's* repository, not
#: anything the install configures: a user filing a ticket is reporting a
#: problem with nyxGPT, wherever their own agent tooling happens to point.
ISSUE_REPO_OWNER = "dkblinux98"
ISSUE_REPO_NAME = "nyxGPT"
ISSUE_REPO_URL = f"https://github.com/{ISSUE_REPO_OWNER}/{ISSUE_REPO_NAME}"
ISSUE_FORM_TEMPLATE = "support.yml"
REPO_DEFAULT_BRANCH = "master"

#: The label that routes a support ticket, and the only thing that does. The
#: Support project auto-adds `is:issue is:open label:Support` and every
#: agent-loop guard is keyed on it, so a ticket without it is a ticket in the
#: development queue (#3810).
SUPPORT_LABEL = "Support"

#: GitHub's REST root. Overridable by environment so the intake can be driven
#: end to end against a stub in CI (`support-intake-smoke.yml`) without
#: filing anything into the live repository -- and so a GitHub Enterprise
#: host is a config change rather than a code change.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_BASE_ENV = "NYXGPT_GITHUB_API_BASE"

#: Every support ticket's title starts here. `support_intake_guard.yml` reads
#: it as one of its two "this is support-shaped" tells, and it is what the
#: issue template's `title:` prefix produces for a filer who goes to GitHub
#: directly -- the two intakes must not produce differently-shaped tickets.
TICKET_TITLE_PREFIX = "support: "

#: Bounds on what the intake accepts. The API model enforces the same
#: numbers at the edge; these are here because `submit_ticket` is also
#: reachable from Python and an empty title reaches GitHub as a 422 that
#: says nothing useful to the person who typed it.
SUMMARY_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 8000

#: How long to wait on GitHub before telling the filer it did not work.
#: Long enough for a slow link, short enough that a hung request does not
#: leave someone staring at a spinner wondering whether they filed twice.
SUBMIT_TIMEOUT_SECONDS = 20.0

#: What kind of ticket this is, chosen by the filer on nyxGPT's own intake
#: page (#3811). These are the `Ticket Type` options on the Support project,
#: mirrored here because the ticket is created from this install: the answer
#: is rendered into the issue body, where the owner reads it when setting the
#: project field. They are NOT labels: only `Support` is, and only `Support`
#: routes anything.
#:
#: Must stay in step with the dropdown options in
#: `.github/ISSUE_TEMPLATE/support.yml`, which asks the same questions for
#: the one case nyxGPT cannot file for -- an install with no credential.
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

#: Where the UI posts a ticket. Reported in `support_context` rather than
#: written into the frontend a second time, so the two cannot drift.
SUBMIT_ROUTE = "/api/v1/support/tickets"

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


log = logging.getLogger("nyxgpt.support")


class DocumentNotFoundError(LookupError):
    """Raised when a slug names no packaged document."""


class SupportTicketError(RuntimeError):
    """Filing a ticket on the user's behalf failed.

    The message is written to be shown to the *filer*: they are a user with a
    problem, not an operator, so it says what happened to their report and
    what they can do next -- never a stack trace and never a bare status code.
    """


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


def issue_form_url(environment: dict[str, str] | None = None) -> str:
    """Return the GitHub issue-form URL with the report's details prefilled.

    This is the **fallback**, not the intake: it exists for an install with
    no GitHub credential, which cannot file for anyone. The intake page
    offers it as a link the filer may choose, never as somewhere they are
    sent (#3811).

    Prefill happens through query parameters keyed by the form's field ids
    (`.github/ISSUE_TEMPLATE/support.yml`), which is GitHub's supported
    mechanism for issue forms. The template declares the `Support` label
    itself, so no label parameter is needed -- and none is sent, since a
    label parameter would silently apply nothing for a filer without write
    access, which is exactly the filer this form exists for.

    **No `ticket_type` prefill.** The previous version passed the type the
    filer had chosen in the Support menu, on the assumption that GitHub
    preselects the dropdown from it. The owner's acceptance screenshot shows
    the field arriving as `None` regardless: the choice was thrown away
    silently and the same question was asked twice. The type is now asked
    once, on the nyxGPT intake page; on this fallback path it is answered on
    the GitHub form, which is the only place it can be recorded when nyxGPT
    is not the one creating the issue.
    """
    env = environment or environment_summary()
    params: dict[str, str] = {
        "template": ISSUE_FORM_TEMPLATE,
        "version": env["version"],
        "platform": f"{env['platform']}, Python {env['python']}",
    }
    return f"{ISSUE_REPO_URL}/issues/new?{urlencode(params)}"


def ticket_type_options() -> list[dict[str, str]]:
    """Return the ticket types the intake page asks about, with descriptions.

    The filer chooses what kind of ticket this is *in nyxGPT* rather than on
    GitHub, which is what gets the type recorded at all: the Support project
    types tickets with a project field, and nothing maps a GitHub form answer
    onto one automatically (#3811).

    No per-type GitHub link rides along any more. That link *was* the defect
    the owner re-tested: choosing a type in the Support menu navigated away
    to the GitHub compose page, so the product intake was never reached.
    """
    return [
        {
            "value": ticket_type,
            "description": TICKET_TYPE_DESCRIPTIONS[ticket_type],
        }
        for ticket_type in TICKET_TYPES
    ]


def github_api_base() -> str:
    """Return the GitHub REST root the intake files against.

    `NYXGPT_GITHUB_API_BASE` overrides it, which is how the smoke job drives
    a real filing end to end against a stub instead of writing a throwaway
    ticket into the live repository on every push.
    """
    return (os.environ.get(GITHUB_API_BASE_ENV, "") or GITHUB_API_BASE).rstrip("/")


def ticket_title(summary: str) -> str:
    """Return the issue title for `summary`, prefixed exactly once.

    A filer who types "support: docs are a mess" gets one prefix, not two:
    the prefix is a routing tell, and doubling it looks like a bug in the
    product they are already reporting a bug about.
    """
    cleaned = " ".join(summary.split())
    if cleaned.lower().startswith(TICKET_TITLE_PREFIX.strip().lower()):
        cleaned = cleaned[len(TICKET_TITLE_PREFIX.strip()) :].strip()
    return f"{TICKET_TITLE_PREFIX}{cleaned}"


def ticket_body(ticket_type: str, description: str, environment: dict[str, str]) -> str:
    """Render the issue body for a ticket filed from inside nyxGPT.

    Deliberately the same shape GitHub renders for
    `.github/ISSUE_TEMPLATE/support.yml`: one `###` heading per answer, in
    the form's order. Two things depend on that and neither is cosmetic --
    triage reads one ticket format rather than two, and
    `support_intake_guard.yml` recognises a support-shaped issue by the
    `### Installed version` heading, so a ticket that arrived unlabeled would
    still be repaired.
    """
    return "\n\n".join(
        (
            "### Ticket type",
            ticket_type,
            "### What happened?",
            description.strip(),
            "### Installed version",
            environment["version"],
            "### Platform",
            f"{environment['platform']}, Python {environment['python']}",
            "<sub>Filed from nyxGPT → Support → File an Issue. The filer never "
            "left the app (#3811).</sub>",
        )
    )


def _validated_ticket(ticket_type: str, summary: str, description: str) -> tuple[str, str]:
    """Check one ticket's fields, returning `(title, description)`.

    Raises:
        ValueError: the type is not one of `TICKET_TYPES`, or a required
            field is blank or over length. Refusing here means the filer is
            told what to fix; passing it through means GitHub answers 422 and
            the UI has nothing useful to show.
    """
    if ticket_type not in TICKET_TYPES:
        raise ValueError(f"Unknown ticket type: {ticket_type!r}")
    cleaned_summary = summary.strip()
    cleaned_description = description.strip()
    if not cleaned_summary:
        raise ValueError("A ticket needs a one-line summary.")
    if not cleaned_description:
        raise ValueError("A ticket needs a description of what happened.")
    if len(cleaned_summary) > SUMMARY_MAX_LENGTH:
        raise ValueError(f"The summary is longer than {SUMMARY_MAX_LENGTH} characters.")
    if len(cleaned_description) > DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"The description is longer than {DESCRIPTION_MAX_LENGTH} characters.")
    return ticket_title(cleaned_summary), cleaned_description


#: What each GitHub status means *to the filer*. Anything not listed falls
#: back to a generic message naming the status, because a wrong-but-specific
#: explanation is worse than an honest vague one.
_SUBMIT_STATUS_MESSAGES: dict[int, str] = {
    401: (
        "GitHub rejected the credential this install files tickets with. "
        "Check `[github] pat` in your nyxGPT configuration (`nyxgpt config`) "
        "-- the token may have expired."
    ),
    403: (
        "GitHub refused the request. The configured token may lack access to "
        "the nyxGPT repository, or you may have hit a rate limit -- wait a "
        "minute and try again."
    ),
    404: (
        "GitHub could not find the nyxGPT issue tracker with the configured "
        "credential. If `[github] pat` is a fine-grained token, it needs "
        "read/write access to issues."
    ),
    410: "Issues are disabled on the nyxGPT repository, so the ticket could not be filed.",
    422: "GitHub rejected the ticket's contents. Shortening the summary usually fixes it.",
}


def submit_ticket(
    ticket_type: str,
    summary: str,
    description: str,
    token: str,
    environment: dict[str, str] | None = None,
    timeout: float = SUBMIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """File one support ticket on the user's behalf and return what was created.

    `{number, url, title, labeled}` -- the number and URL are the created
    issue's, so the UI can show the filer the ticket rather than a promise
    that one exists.

    `labeled` reports whether `SUPPORT_LABEL` is actually on the created
    issue. It is read back from GitHub's response rather than inferred from
    the request, because GitHub *silently* drops `labels` for a token
    without push access: assuming it applied is precisely how #3810 put an
    unrouted ticket in front of the agent loop. A false here is not a failed
    filing -- the ticket exists and `support_intake_guard.yml` repairs it on
    the `issues: opened` event -- so it is reported, logged, and not raised.

    Raises:
        ValueError: the ticket's own fields are unusable (see
            `_validated_ticket`).
        SupportTicketError: GitHub was unreachable, refused the request, or
            answered something this cannot read. The message is filer-facing.
    """
    import httpx

    title, cleaned_description = _validated_ticket(ticket_type, summary, description)
    env = environment or environment_summary()
    payload = {
        "title": title,
        "body": ticket_body(ticket_type, cleaned_description, env),
        "labels": [SUPPORT_LABEL],
    }
    url = f"{github_api_base()}/repos/{ISSUE_REPO_OWNER}/{ISSUE_REPO_NAME}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        # The filer's own network is the likeliest cause and the only one
        # they can act on; the exception text goes to the operator's log.
        log.warning("Filing a support ticket failed to reach GitHub: %s", exc)
        raise SupportTicketError(
            "Could not reach GitHub to file the ticket. Check this machine's "
            "internet connection and try again."
        ) from exc

    if response.status_code != 201:
        log.warning(
            "GitHub answered HTTP %s filing a support ticket: %s",
            response.status_code,
            response.text[:500],
        )
        raise SupportTicketError(
            _SUBMIT_STATUS_MESSAGES.get(
                response.status_code,
                f"GitHub answered HTTP {response.status_code} and the ticket was not filed.",
            )
        )

    try:
        created = response.json()
        number = int(created["number"])
        issue_url = str(created["html_url"])
    except (ValueError, KeyError, TypeError) as exc:
        # The ticket may well exist; what is missing is the way to show it.
        log.warning("GitHub's issue-creation response could not be read: %s", exc)
        raise SupportTicketError(
            "The ticket was sent, but GitHub's reply could not be read, so "
            f"there is no link to show. Check {ISSUE_REPO_URL}/issues before "
            "filing it again."
        ) from exc

    labels = created.get("labels") or []
    labeled = any(
        isinstance(label, dict) and label.get("name") == SUPPORT_LABEL for label in labels
    )
    if not labeled:
        log.warning(
            "Support ticket #%s was created WITHOUT the %r label -- the token "
            "likely has no push access, so GitHub dropped it. "
            "support_intake_guard.yml repairs this on the issues:opened event.",
            number,
            SUPPORT_LABEL,
        )

    return {"number": number, "url": issue_url, "title": title, "labeled": labeled}


def support_context(can_submit: bool = False) -> dict[str, Any]:
    """Return everything the Support menu needs to file a ticket.

    `can_submit` says whether this install holds a credential to file with;
    the caller resolves it (`app.py` from `[github] pat`) because this module
    deliberately knows nothing about configuration.

    What the UI does with it changed in the #3811 re-test, and the change is
    the point: it is read on the intake page to decide what that page *says*
    -- the form, or "this install cannot file for you" plus the GitHub form
    as an offer. It no longer decides where a menu entry goes. A definite
    `false` is acted on; an absent or unreadable answer is not, so a context
    call that fails leaves the filer with a working form rather than a trip
    to github.com.
    """
    env = environment_summary()
    return {
        "environment": env,
        # Kept as the untyped entry point: the fallback for an install with
        # no credential, and for a filer who reaches the form some other way
        # -- version/platform still prefilled, type answered on GitHub.
        "issue_form_url": issue_form_url(env),
        "ticket_types": ticket_type_options(),
        "docs_route": DOCS_ROUTE_PREFIX,
        # Whether nyxGPT can file the ticket itself, and where it posts when
        # it can. The route is reported rather than hardcoded in the UI so
        # the two cannot drift apart silently.
        "can_submit": can_submit,
        "submit_route": SUBMIT_ROUTE,
        "ticket_type_descriptions": dict(TICKET_TYPE_DESCRIPTIONS),
        # Docs render offline; filing does not, whichever path it takes. The
        # UI says so rather than letting it fail mysteriously on an
        # air-gapped install.
        "requires_network": True,
        "network_note": (
            "Filing a ticket sends it to the nyxGPT issue tracker and needs "
            "internet access; without a configured GitHub token it opens "
            "GitHub in your browser instead and needs a GitHub account. The "
            "documentation above is packaged with nyxGPT and works offline."
        ),
    }

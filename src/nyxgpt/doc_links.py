"""Where a user-facing message points when it names a document (#4182).

Every `nyxgpt` command, API error and UI string that said "see docs/ops.md"
was naming a path only a *checkout* has. On a keg, a wheel, a container or a
Pod there is no `docs/` directory beside the running code, so about twenty
messages across the CLI and the API told operators to read a file their
install does not contain -- and two pointed at
`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`, which is not even
shipped in the docs tree. That breaks the repo-less requirement directly:
"the entire stack must be installable and runnable without checking out or
downloading the code repository", so every pointer must work without one.

This module is the single place that turns a repo-relative document path into
something a reader on any install can follow. It is deliberately tiny and
dependency-free -- `support.py` owns the docs *viewer* (and imports
`markdown` and `beautifulsoup4` to render it), while `ops.py`, `cli.py` and
the `cloud_*` modules only need to name a destination, and must not drag that
rendering stack into a `nyxgpt ops status` run.

Two destinations, and the choice between them is not a style preference:

* **a packaged document** (`support.PACKAGED_SLUGS` -- the product help
  symlinked into the wheel) is *also* served by the running web UI at
  `/support/docs/<slug>`, so the pointer names that route as well as the
  hosted copy. An operator with the stack up reads it without leaving the
  machine;
* **anything else** -- the process and contributor documents #3809 kept out
  of the artifact, and anything under `product_management/` -- exists only in
  the repository, so the hosted URL is the only honest pointer.

`tests/unit/test_no_repo_relative_user_paths.py` fails the build on a
user-facing string that names a repo path directly, which is what keeps this
from drifting back: the rule was already implied by the repo-less requirement
and went unenforced for twenty messages.
"""

from __future__ import annotations

import re

__all__ = [
    "DOCS_ROUTE_PREFIX",
    "REPO_DEFAULT_BRANCH",
    "REPO_NAME",
    "REPO_OWNER",
    "REPO_URL",
    "doc_route",
    "doc_url",
    "see_doc",
]

#: The *product's* repository, not anything an install configures: a reader
#: following one of these pointers is reading about nyxGPT, wherever their own
#: tooling happens to point. `support.py` re-exports these as
#: `ISSUE_REPO_OWNER`/`ISSUE_REPO_NAME`/`ISSUE_REPO_URL` for the support
#: intake, which files tickets into the same repository.
REPO_OWNER = "dkblinux98"
REPO_NAME = "nyxGPT"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"

#: Where the hosted copy of a repository file is read from. The default
#: branch, not the release line: a release branch is deleted when its line is
#: retired, which would turn every link in the product into a 404 at the next
#: roll (the same time-bomb `test_no_hardcoded_release_version.py` exists to
#: stop).
REPO_DEFAULT_BRANCH = "master"

#: The in-app route the packaged docs viewer serves. Must match
#: `support.DOCS_ROUTE_PREFIX`, which is asserted by
#: `tests/unit/test_support_docs.py`.
DOCS_ROUTE_PREFIX = "/support/docs"

#: `docs/<slug>.md`, with an optional `#anchor`. Only `docs/` slugs can be
#: packaged, so only they are matched here.
_DOC_PATH = re.compile(r"^(?:\./)?docs/(?P<slug>[A-Za-z0-9_.-]+)\.md(?P<anchor>#[-\w]+)?$")


def _packaged_slugs() -> frozenset[str]:
    """The packaged slug set, imported lazily.

    `support` pulls in `markdown` and `beautifulsoup4`; this module is
    imported by `ops`, and a `nyxgpt ops status` run must not pay for an HTML
    renderer to print a link. The import happens inside the one function that
    needs the answer, and only when a `docs/` path is actually being resolved.
    """
    from nyxgpt.support import PACKAGED_SLUGS

    return frozenset(PACKAGED_SLUGS)


def doc_url(path: str) -> str:
    """The hosted URL for a repository-relative `path`, anchor preserved.

    Works for any tracked file, not only documents: `docs/ops.md#tls`,
    `product_management/VISION.md`, `k8s/configmap.yaml`. The returned URL is
    readable from a machine that has never seen the repository, which is the
    whole requirement.
    """
    # Not `lstrip("./")`: that strips a *character set*, so `.github/...`
    # loses its leading dot and the URL 404s.
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    anchor = ""
    if "#" in cleaned:
        cleaned, _, fragment = cleaned.partition("#")
        anchor = f"#{fragment}"
    return f"{REPO_URL}/blob/{REPO_DEFAULT_BRANCH}/{cleaned}{anchor}"


def doc_route(path: str) -> str:
    """The in-app docs route for a packaged `docs/<slug>.md`, else `""`.

    `""` means "this document is not in the artifact", and a caller must then
    use `doc_url` -- an in-app route for an unpackaged document is a 404, not
    a graceful degradation.
    """
    match = _DOC_PATH.match(path.strip())
    if match is None:
        return ""
    slug = match.group("slug")
    if slug not in _packaged_slugs():
        return ""
    return f"{DOCS_ROUTE_PREFIX}/{slug}{match.group('anchor') or ''}"


def see_doc(path: str, *, topic: str = "") -> str:
    """A pointer phrase for a user-facing message: where to read `path`.

    The form every CLI/API string should use in place of a bare `docs/*.md`.
    A packaged document names the in-app route first -- an operator whose
    stack is up reads it locally, with no network -- and the hosted URL
    second, which is the one that works when it is not. An unpackaged one
    names only the hosted URL, because that is the only place it exists.

    `topic` is an optional section name to carry through, e.g.
    `see_doc("docs/cloud.md", topic="EC2 Mac targets")`.
    """
    where = f' ("{topic}")' if topic else ""
    route = doc_route(path)
    if route:
        return f"see the in-app docs at {route}{where}, or {doc_url(path)}"
    return f"see {doc_url(path)}{where}"

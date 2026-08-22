"""Tests for the in-app support intake: nyxGPT files the ticket (#3811).

The acceptance failure these pin is a UX one with a mechanical core. The
filer must not be handed to github.com's compose page, so the product creates
the issue itself -- and the moment it does, it owns two things GitHub's form
used to own for free:

* the `Support` label, which is the only thing that routes a ticket away from
  the agent loop. GitHub drops `labels` *silently* for a token without push
  access, so the created issue is read back rather than assumed (#3810 is
  what "assumed" looks like);
* every failure the filer can see. A user reporting a problem is the last
  person who should meet a stack trace, so each GitHub refusal maps to a
  sentence about their ticket, and an install with no credential gets the
  prefilled form rather than a dead end.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from nyxgpt import api_models, support
from nyxgpt import app as app_module
from nyxgpt.app import api, app

pytestmark = pytest.mark.unit

# The web route the menu links to. A literal, because it is the Next.js
# page's path and nothing in the Python package routes it -- the docstrings
# below are the only place the backend names it, which is why they drift.
INTAKE_PAGE_ROUTE = "/support/new"

ENV = {"version": "3.0.0", "platform": "Linux 6.8.0 (x86_64)", "python": "3.11.9"}


class _Response:
    """The parts of `httpx.Response` `submit_ticket` reads."""

    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def _created(number: int = 4242, labels: list[dict[str, str]] | None = None) -> _Response:
    """GitHub's 201 for a created issue."""
    return _Response(
        201,
        {
            "number": number,
            "html_url": f"{support.ISSUE_REPO_URL}/issues/{number}",
            "labels": [{"name": support.SUPPORT_LABEL}] if labels is None else labels,
        },
    )


def _capture(monkeypatch, response: _Response) -> dict[str, Any]:
    """Answer every `httpx.post` with `response`, recording the request."""
    seen: dict[str, Any] = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


# --- What the ticket looks like ------------------------------------------


def test_the_body_matches_the_shape_github_s_own_form_renders():
    """One `###` heading per answer, in the form's order.

    Two things ride on this and neither is cosmetic: triage reads one ticket
    format rather than two, and `support_intake_guard.yml` recognises a
    support-shaped issue by the `### Installed version` heading -- so a
    ticket that somehow arrived unlabeled would still be repaired.
    """
    body = support.ticket_body("Bug Found", "The spinner never stops.", ENV)

    assert body.index("### Ticket type") < body.index("### What happened?")
    assert body.index("### What happened?") < body.index("### Installed version")
    assert body.index("### Installed version") < body.index("### Platform")
    assert "Bug Found" in body
    assert "The spinner never stops." in body
    assert "3.0.0" in body
    assert "Linux 6.8.0 (x86_64), Python 3.11.9" in body


def test_the_guard_workflow_s_tell_survives_a_ticket_this_intake_filed():
    """`support_intake_guard.yml` keys on this exact string. Pin it here.

    The guard is the backstop for an unlabeled ticket, and it only fires on
    issues it can recognise. A body rendered without this heading would make
    the backstop inert for exactly the tickets nyxGPT itself filed.
    """
    assert "### Installed version" in support.ticket_body("Question", "How do I ...?", ENV)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("Docs are a mess", "support: Docs are a mess"),
        # Already prefixed, in either case: prefix once, not twice.
        ("support: Docs are a mess", "support: Docs are a mess"),
        ("Support: Docs are a mess", "support: Docs are a mess"),
        ("  spaced   out  ", "support: spaced out"),
    ],
)
def test_the_title_carries_exactly_one_support_prefix(summary, expected):
    assert support.ticket_title(summary) == expected


# --- Refusing a ticket that cannot be filed ------------------------------


@pytest.mark.parametrize(
    ("ticket_type", "summary", "description"),
    [
        ("Production Defect", "s", "d"),  # not a type the Support project has
        ("Bug Found", "   ", "d"),
        ("Bug Found", "s", "  \n "),
        ("Bug Found", "x" * (support.SUMMARY_MAX_LENGTH + 1), "d"),
        ("Bug Found", "s", "x" * (support.DESCRIPTION_MAX_LENGTH + 1)),
    ],
)
def test_an_unusable_ticket_is_refused_before_it_reaches_github(
    monkeypatch, ticket_type, summary, description
):
    """The filer is told what to fix, rather than GitHub answering 422.

    A 422 arrives with GitHub's vocabulary, not the form's, and the UI has
    nothing useful to show for it.
    """
    called = _capture(monkeypatch, _created())
    with pytest.raises(ValueError):
        support.submit_ticket(ticket_type, summary, description, token="t", environment=ENV)
    assert called == {}


# --- Filing --------------------------------------------------------------


def test_a_filed_ticket_carries_the_support_label_and_comes_back_with_its_url(monkeypatch):
    seen = _capture(monkeypatch, _created(number=3999))

    result = support.submit_ticket(
        "Feature Request",
        "Add a dark mode toggle",
        "It is too bright.",
        token="tok",
        environment=ENV,
    )

    assert result == {
        "number": 3999,
        "url": f"{support.ISSUE_REPO_URL}/issues/3999",
        "title": "support: Add a dark mode toggle",
        "labeled": True,
    }
    # The label is the routing key, so it is on the request, not hoped for.
    assert seen["json"]["labels"] == [support.SUPPORT_LABEL]
    assert seen["json"]["title"] == "support: Add a dark mode toggle"
    assert "### Ticket type" in seen["json"]["body"]
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["url"] == (
        f"{support.GITHUB_API_BASE}/repos/"
        f"{support.ISSUE_REPO_OWNER}/{support.ISSUE_REPO_NAME}/issues"
    )
    assert seen["timeout"] == support.SUBMIT_TIMEOUT_SECONDS


def test_the_environment_is_filled_in_from_the_running_install_when_not_supplied(monkeypatch):
    """The filer is never asked for their version -- the install knows it."""
    monkeypatch.setattr(support, "environment_summary", lambda: dict(ENV))
    seen = _capture(monkeypatch, _created())

    support.submit_ticket("Question", "How do I export?", "Cannot find it.", token="t")

    assert "3.0.0" in seen["json"]["body"]


def test_a_label_github_dropped_is_reported_rather_than_assumed(monkeypatch, caplog):
    """The #3810 shape, from the other side.

    GitHub ignores `labels` from a token without push access and says
    nothing about it. The ticket is real, so this is not a failed filing --
    but the caller has to know the ticket is currently unrouted, and the
    operator's log has to say why. `support_intake_guard.yml` repairs it on
    the `issues: opened` event.
    """
    _capture(monkeypatch, _created(number=17, labels=[]))

    with caplog.at_level("WARNING"):
        result = support.submit_ticket("Bug Found", "s", "d", token="t", environment=ENV)

    assert result["labeled"] is False
    assert result["number"] == 17
    assert "WITHOUT the 'Support' label" in caplog.text


def test_a_label_list_of_plain_strings_does_not_crash_the_read(monkeypatch):
    """GitHub returns label objects; a proxy or stub may not.

    Reading `label["name"]` off a string would raise inside a successful
    filing, turning a filed ticket into an error the filer sees.
    """
    _capture(monkeypatch, _created(labels=["Support"]))

    result = support.submit_ticket("Bug Found", "s", "d", token="t", environment=ENV)

    assert result["labeled"] is False


# --- When GitHub says no -------------------------------------------------


def test_an_unreachable_github_tells_the_filer_about_their_connection(monkeypatch):
    def explode(*_args, **_kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", explode)

    with pytest.raises(support.SupportTicketError) as excinfo:
        support.submit_ticket("Bug Found", "s", "d", token="t", environment=ENV)

    assert "internet connection" in str(excinfo.value)
    # Never the transport's own words: "no route to host" is for the log.
    assert "no route to host" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "expired"),
        (403, "rate limit"),
        (404, "read/write access to issues"),
        (410, "Issues are disabled"),
        (422, "Shortening the summary"),
        (500, "HTTP 500"),
    ],
)
def test_each_github_refusal_becomes_a_sentence_about_the_ticket(monkeypatch, status, expected):
    """Including the unmapped case: an honest vague answer beats a wrong specific one."""
    _capture(monkeypatch, _Response(status, text="whatever GitHub said"))

    with pytest.raises(support.SupportTicketError) as excinfo:
        support.submit_ticket("Bug Found", "s", "d", token="t", environment=ENV)

    assert expected in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,  # not JSON at all
        {"html_url": "https://example.invalid/1"},  # no number
        {"number": "not-a-number", "html_url": "u"},
    ],
)
def test_an_unreadable_creation_response_says_the_ticket_may_exist(monkeypatch, payload):
    """The ticket was probably created; what is missing is the link to it.

    Telling the filer to file again would duplicate a real ticket, so the
    message sends them to the issue list instead.
    """
    _capture(monkeypatch, _Response(201, payload))

    with pytest.raises(support.SupportTicketError) as excinfo:
        support.submit_ticket("Bug Found", "s", "d", token="t", environment=ENV)

    assert "before filing it again" in str(excinfo.value)


# --- Where it files ------------------------------------------------------


def test_the_api_base_is_overridable_so_the_smoke_job_can_file_for_real(monkeypatch):
    """`support-intake-smoke.yml` drives a real filing against a stub.

    Without this the only executed proof available would write a throwaway
    ticket into the live repository on every push.
    """
    monkeypatch.setenv(support.GITHUB_API_BASE_ENV, "http://127.0.0.1:8931/")
    assert support.github_api_base() == "http://127.0.0.1:8931"

    monkeypatch.setenv(support.GITHUB_API_BASE_ENV, "")
    assert support.github_api_base() == support.GITHUB_API_BASE

    monkeypatch.delenv(support.GITHUB_API_BASE_ENV)
    assert support.github_api_base() == support.GITHUB_API_BASE


def test_tickets_go_to_the_product_s_repository_not_the_install_s_configured_one():
    """A user reporting a nyxGPT problem is reporting it to nyxGPT.

    The install's `[github] repo_owner`/`repo_name` belong to its own agent
    tooling and may point anywhere; routing a support ticket by them would
    file a stranger's report into a stranger's fork.
    """
    assert support.ISSUE_REPO_URL.startswith("https://github.com/")
    assert support.ISSUE_REPO_URL.endswith(f"{support.ISSUE_REPO_OWNER}/{support.ISSUE_REPO_NAME}")
    assert support.ISSUE_REPO_OWNER == "dkblinux98"
    assert support.ISSUE_REPO_NAME == "nyxGPT"


# --- The endpoints -------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


def _with_token(monkeypatch, token: str) -> None:
    """Pretend this install's config does (or does not) hold a GitHub PAT."""
    monkeypatch.setattr("nyxgpt.app.get_github_pat", lambda _cfg: token)


def test_the_context_says_whether_this_install_can_file_for_the_user(client, monkeypatch):
    _with_token(monkeypatch, "tok")
    body = client.get("/api/v1/support/context").json()
    assert body["can_submit"] is True
    assert body["submit_route"] == support.SUBMIT_ROUTE

    _with_token(monkeypatch, "")
    body = client.get("/api/v1/support/context").json()
    assert body["can_submit"] is False
    # The fallback is still there: no credential is not "you cannot report
    # this", it is "here is the form, prefilled".
    assert body["issue_form_url"].startswith(support.ISSUE_REPO_URL)


def test_filing_through_the_endpoint_returns_the_created_ticket(client, monkeypatch):
    _with_token(monkeypatch, "tok")
    _capture(monkeypatch, _created(number=4300))

    res = client.post(
        "/api/v1/support/tickets",
        json={
            "ticket_type": "Bug Found",
            "summary": "Docs are a mess",
            "description": "I cannot find the install steps.",
        },
    )

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "filed"
    assert body["number"] == 4300
    # The link is the point: the UI shows the filer their ticket rather than
    # a promise that one exists somewhere.
    assert body["url"].endswith("/issues/4300")
    assert body["labeled"] is True


def test_an_install_with_no_credential_gets_the_prefilled_form_not_a_dead_end(client, monkeypatch):
    _with_token(monkeypatch, "")

    res = client.post(
        "/api/v1/support/tickets",
        json={"ticket_type": "Question", "summary": "s", "description": "d"},
    )

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "no_credential"
    assert "template=support.yml" in body["issue_form_url"]


def test_an_unknown_ticket_type_is_a_400_not_a_500(client, monkeypatch):
    _with_token(monkeypatch, "tok")
    called = _capture(monkeypatch, _created())

    res = client.post(
        "/api/v1/support/tickets",
        json={"ticket_type": "Production Defect", "summary": "s", "description": "d"},
    )

    assert res.status_code == 400
    assert called == {}


def test_a_github_refusal_reaches_the_filer_as_a_502_with_a_readable_reason(client, monkeypatch):
    _with_token(monkeypatch, "tok")
    _capture(monkeypatch, _Response(403, text="rate limited"))

    res = client.post(
        "/api/v1/support/tickets",
        json={"ticket_type": "Bug Found", "summary": "s", "description": "d"},
    )

    assert res.status_code == 502
    # `apiErrorText` unwraps this envelope in the UI; what matters here is
    # that the text is about the ticket and not about HTTP.
    assert "rate limit" in json.dumps(res.json())


def test_filing_is_the_only_write_on_the_support_surface():
    """The docs half stays read-only, and there is exactly one way to file.

    This replaces the old `test_support_surface_is_read_only`: that pinned
    the design the owner rejected in acceptance (the GitHub handoff), so it
    could not simply be kept. What it was really protecting -- that Support
    is not a general-purpose write surface -- is what is asserted here.
    """
    support_routes = [r for r in api.routes if "/support" in getattr(r, "path", "")]
    assert support_routes

    writes = {
        getattr(route, "path", "")
        for route in support_routes
        if set(getattr(route, "methods", set())) - {"GET", "HEAD", "OPTIONS"}
    }
    # Compared against `SUBMIT_ROUTE` rather than a literal: that constant is
    # what the UI is told to post to, so this also pins that the advertised
    # route is the route that exists.
    assert writes == {support.SUBMIT_ROUTE}

    tickets = next(r for r in support_routes if getattr(r, "path", "") == support.SUBMIT_ROUTE)
    assert set(getattr(tickets, "methods", set())) == {"POST"}


def test_the_intake_documents_itself_as_a_page_not_as_a_dialog_in_the_chat():
    """Every docstring describing the intake names the page it actually is.

    This guards a defect that has now recurred, not a matter of style. The
    first build of this intake was a dialog opened from the chat and the
    owner failed it in acceptance: the surface is a route (`/support/new`)
    reached by a menu entry that asks nothing and decides nothing. Three
    docstrings describe that surface to the next reader; when the shape
    changed, two were corrected and `SupportTicketRequest` was left saying
    the filer answers "in the chat" -- a sentence that reads as current and
    is false, which is how a rejected design survives into the next change.

    So the claim is pinned where it can be checked mechanically: name the
    route, and do not describe the intake as living in the chat.
    """
    documented = {
        "nyxgpt.support module": support.__doc__,
        "POST /support/tickets": app_module.support_file_ticket.__doc__,
        "SupportTicketRequest": api_models.SupportTicketRequest.__doc__,
    }

    for name, doc in documented.items():
        assert doc, f"{name} has no docstring to check"
        assert (
            INTAKE_PAGE_ROUTE in doc
        ), f"{name} describes the intake without naming {INTAKE_PAGE_ROUTE}"
        assert "in the chat" not in doc, (
            f"{name} still says the filer answers 'in the chat'; the intake is "
            f"the {INTAKE_PAGE_ROUTE} page (#3811 re-test)"
        )

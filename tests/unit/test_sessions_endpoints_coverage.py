"""Unit tests for the /api/v1/sessions/* HTTP route handlers in nyxgpt/app.py.

These target the FastAPI route handlers themselves (sessions_list,
sessions_search, sessions_show, sessions_init, sessions_delete,
sessions_summarize, sessions_pin/unpin, sessions_title, sessions_tags_add/
remove, sessions_rename, sessions_sync_filename) -- the HTTP glue that
validates input, calls into `nyxgpt.sessions`, and translates the module's
`(ok, msg)` tuples / exceptions into HTTP responses. The `nyxgpt.sessions`
module itself is covered in detail by tests/unit/test_sessions.py and
tests/unit/test_session_auto_naming.py; this file intentionally does not
duplicate that coverage.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt import sessions as sessions_module
from nyxgpt.app import app

pytestmark = pytest.mark.unit

client = TestClient(app)

BASE = "/api/v1/sessions"


def _error_message(resp) -> str:
    """Extract the error envelope's message field, asserting its shape."""
    body = resp.json()
    assert "error" in body
    assert "message" in body["error"]
    assert "request_id" in body["error"]
    message = body["error"]["message"]
    assert isinstance(message, str)
    return message


# ----------------------------
# sessions_list (GET /sessions)
# ----------------------------


def test_sessions_list_maps_metadata_fields_with_mixed_types():
    """Covers the per-row metadata mapping, including the isinstance fallbacks."""
    rows = [
        {
            "name": "full-session",
            "messages": 5,
            "modified": "2026-07-01T00:00:00",
            "meta": {
                "pinned": True,
                "tags": ["a", "b"],
                "title": "My Title",
                "summary": "A summary",
                "token_estimate": 42,
                "model": "llama3",
            },
        },
        {
            "name": "sparse-session",
            "messages": 0,
            "modified": "2026-07-02T00:00:00",
            "meta": {
                # wrong/missing types should fall back to the "empty" defaults
                "pinned": False,
                "tags": "not-a-list",
                "title": None,
                "summary": 123,
                "token_estimate": "not-an-int",
                "model": None,
            },
        },
        {
            "name": "no-meta-session",
            "messages": 1,
            "modified": "2026-07-03T00:00:00",
            # meta key entirely absent -> r.get("meta") is None -> `or {}` branch
        },
    ]
    with patch("nyxgpt.app.sessions.list_sessions", return_value=rows) as mock_list:
        resp = client.get(BASE)

    assert resp.status_code == 200
    mock_list.assert_called_once()
    data = resp.json()["sessions"]
    assert len(data) == 3

    full = data[0]
    assert full["name"] == "full-session"
    assert full["pinned"] is True
    assert full["tags"] == ["a", "b"]
    assert full["title"] == "My Title"
    assert full["summary"] == "A summary"
    assert full["token_estimate"] == 42
    assert full["model"] == "llama3"

    sparse = data[1]
    assert sparse["pinned"] is False
    assert sparse["tags"] == []
    assert sparse["title"] == ""
    assert sparse["summary"] == ""
    assert sparse["token_estimate"] is None
    assert sparse["model"] == ""

    no_meta = data[2]
    assert no_meta["name"] == "no-meta-session"
    assert no_meta["pinned"] is False
    assert no_meta["tags"] == []


def test_sessions_list_honors_sessions_dir_query_param(tmp_path: Path):
    with patch("nyxgpt.app.sessions.list_sessions", return_value=[]) as mock_list:
        resp = client.get(BASE, params={"sessions_dir": str(tmp_path)})

    assert resp.status_code == 200
    called_dir = mock_list.call_args[0][0]
    assert Path(called_dir) == tmp_path


# ----------------------------
# sessions_search (GET /sessions/search)
# ----------------------------


def test_sessions_search_with_role_filter_converts_enum_to_string():
    fake_results = [
        {
            "session_name": "s1",
            "session_title": "Title",
            "message_index": 2,
            "role": "user",
            "content": "hello world",
            "content_preview": "...hello world...",
            "timestamp": "2026-07-01T00:00:00",
            "matches": 1,
        }
    ]
    with patch("nyxgpt.app.sessions.search_messages", return_value=fake_results) as mock_search:
        resp = client.get(
            f"{BASE}/search",
            params={"query": "hello", "role_filter": "user", "limit": 10},
        )

    assert resp.status_code == 200
    # role_filter is a MessageRole enum on the way in; the handler must
    # convert it to its plain string .value before calling sessions.search_messages.
    assert mock_search.call_args.kwargs["role_filter"] == "user"
    body = resp.json()
    assert body["query"] == "hello"
    assert body["total_results"] == 1
    assert body["results"][0]["session_name"] == "s1"
    assert body["results"][0]["session_title"] == "Title"
    assert body["results"][0]["matches"] == 1


def test_sessions_search_without_role_filter_passes_none():
    with patch("nyxgpt.app.sessions.search_messages", return_value=[]) as mock_search:
        resp = client.get(f"{BASE}/search", params={"query": "hello"})

    assert resp.status_code == 200
    assert mock_search.call_args.kwargs["role_filter"] is None
    body = resp.json()
    assert body["total_results"] == 0
    assert body["results"] == []


def test_sessions_search_honors_sessions_dir_param(tmp_path: Path):
    with patch("nyxgpt.app.sessions.search_messages", return_value=[]) as mock_search:
        resp = client.get(
            f"{BASE}/search",
            params={"query": "x", "sessions_dir": str(tmp_path)},
        )

    assert resp.status_code == 200
    called_dir = mock_search.call_args.kwargs["sessions_dir"]
    assert Path(called_dir) == tmp_path


# ----------------------------
# sessions_show (GET /sessions/{name})
# ----------------------------


def test_sessions_show_not_found_returns_404():
    resp = client.get(f"{BASE}/does-not-exist-xyz")
    assert resp.status_code == 404
    assert _error_message(resp) == "No such session"


def test_sessions_show_negative_offset_returns_400(tmp_path: Path):
    sessions_module.init_session(
        "show-neg-offset", tmp_path, new_session=True, model="m", system=None
    )
    resp = client.get(
        f"{BASE}/show-neg-offset",
        params={"sessions_dir": str(tmp_path), "offset": -1},
    )
    assert resp.status_code == 400
    assert "offset" in _error_message(resp)


def test_sessions_show_negative_limit_returns_400(tmp_path: Path):
    sessions_module.init_session(
        "show-neg-limit", tmp_path, new_session=True, model="m", system=None
    )
    resp = client.get(
        f"{BASE}/show-neg-limit",
        params={"sessions_dir": str(tmp_path), "limit": -1},
    )
    assert resp.status_code == 400
    assert "limit" in _error_message(resp)


def test_sessions_show_paginated_loads_subset(tmp_path: Path):
    sf, mf, msgs, meta = sessions_module.init_session(
        "show-paginated", tmp_path, new_session=True, model="m", system="sys prompt"
    )
    msgs = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    sessions_module.save_session_messages(sf, msgs)

    resp = client.get(
        f"{BASE}/show-paginated",
        params={"sessions_dir": str(tmp_path), "offset": 1, "limit": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "show-paginated"
    assert body["total"] == 3
    assert body["offset"] == 1
    assert body["limit"] == 1
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "two"


def test_sessions_show_full_without_pagination_params(tmp_path: Path):
    sf, mf, msgs, meta = sessions_module.init_session(
        "show-full", tmp_path, new_session=True, model="m", system=None
    )
    sessions_module.save_session_messages(
        sf, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    )

    resp = client.get(f"{BASE}/show-full", params={"sessions_dir": str(tmp_path)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["offset"] == 0
    assert body["limit"] == 2
    assert len(body["messages"]) == 2


# ----------------------------
# sessions_init (POST /sessions/init)
# ----------------------------


def test_sessions_init_internal_error_returns_500():
    with patch(
        "nyxgpt.app.sessions.session_file_for",
        side_effect=RuntimeError("disk exploded"),
    ):
        resp = client.post(f"{BASE}/init", json={"name": "boom"})

    assert resp.status_code == 500
    assert _error_message(resp) == "Internal server error"


def test_sessions_init_existing_session_returns_existed_true(tmp_path: Path):
    sessions_module.init_session(
        "already-there", tmp_path, new_session=True, model="m", system=None
    )
    with patch("nyxgpt.app.get_sessions_dir", return_value=tmp_path):
        resp = client.post(f"{BASE}/init", json={"name": "already-there"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "name": "already-there", "existed": True}


def test_sessions_init_failure_from_init_session_returns_400(tmp_path: Path):
    with (
        patch("nyxgpt.app.get_sessions_dir", return_value=tmp_path),
        patch(
            "nyxgpt.app.sessions.init_session",
            side_effect=ValueError("bad init"),
        ),
    ):
        resp = client.post(f"{BASE}/init", json={"name": "new-session-fails"})

    assert resp.status_code == 400
    assert _error_message(resp) == "bad init"


# ----------------------------
# sessions_delete (DELETE /sessions/{name})
# ----------------------------


def test_sessions_delete_success_returns_ok():
    with patch("nyxgpt.app.sessions.delete_session", return_value=True) as mock_del:
        resp = client.delete(f"{BASE}/whatever")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_del.assert_called_once()


def test_sessions_delete_not_found_returns_404():
    with patch("nyxgpt.app.sessions.delete_session", return_value=False):
        resp = client.delete(f"{BASE}/missing")

    assert resp.status_code == 404


# ----------------------------
# sessions_summarize (POST /sessions/{name}/summarize)
# ----------------------------


def test_sessions_summarize_failure_returns_400():
    with patch(
        "nyxgpt.app.sessions.summarize_session",
        return_value=(False, "Session has no messages"),
    ):
        resp = client.post(f"{BASE}/foo/summarize")

    assert resp.status_code == 400
    assert _error_message(resp) == "Session has no messages"


def test_sessions_summarize_success_returns_ok():
    with patch("nyxgpt.app.sessions.summarize_session", return_value=(True, "OK")) as mock_sum:
        resp = client.post(f"{BASE}/foo/summarize")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_sum.assert_called_once()


# ----------------------------
# sessions_pin / sessions_unpin
# ----------------------------


def test_sessions_pin_success_returns_ok():
    with patch("nyxgpt.app.sessions.set_pinned", return_value=(True, "OK")) as mock_pin:
        resp = client.post(f"{BASE}/foo/pin")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert mock_pin.call_args[0][1] is True


def test_sessions_pin_failure_returns_400():
    with patch("nyxgpt.app.sessions.set_pinned", return_value=(False, "No such session")):
        resp = client.post(f"{BASE}/foo/pin")

    assert resp.status_code == 400
    assert _error_message(resp) == "No such session"


def test_sessions_unpin_success_returns_ok():
    with patch("nyxgpt.app.sessions.set_pinned", return_value=(True, "OK")) as mock_unpin:
        resp = client.post(f"{BASE}/foo/unpin")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert mock_unpin.call_args[0][1] is False


def test_sessions_unpin_failure_returns_400():
    with patch("nyxgpt.app.sessions.set_pinned", return_value=(False, "No such session")):
        resp = client.post(f"{BASE}/foo/unpin")

    assert resp.status_code == 400
    assert _error_message(resp) == "No such session"


# ----------------------------
# sessions_title
# ----------------------------


def test_sessions_title_success_returns_ok():
    with patch("nyxgpt.app.sessions.set_title", return_value=(True, "OK")) as mock_title:
        resp = client.post(f"{BASE}/foo/title", json={"title": "New Title"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert mock_title.call_args[0][1] == "New Title"


def test_sessions_title_failure_returns_400():
    with patch("nyxgpt.app.sessions.set_title", return_value=(False, "No such session")):
        resp = client.post(f"{BASE}/foo/title", json={"title": "New Title"})

    assert resp.status_code == 400
    assert _error_message(resp) == "No such session"


# ----------------------------
# sessions_tags_add / sessions_tags_remove
# ----------------------------


def test_sessions_tags_add_empty_tags_returns_400():
    resp = client.post(f"{BASE}/foo/tags/add", json={"tags": []})
    assert resp.status_code == 400
    assert _error_message(resp) == "At least one tag is required"


def test_sessions_tags_add_failure_returns_400():
    with patch("nyxgpt.app.sessions.add_tags", return_value=(False, "No such session")):
        resp = client.post(f"{BASE}/foo/tags/add", json={"tags": ["a", "b"]})

    assert resp.status_code == 400
    assert _error_message(resp) == "No such session"


def test_sessions_tags_add_success_returns_ok():
    with patch("nyxgpt.app.sessions.add_tags", return_value=(True, "OK")) as mock_add:
        resp = client.post(f"{BASE}/foo/tags/add", json={"tags": ["a", "b"]})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert mock_add.call_args[0][1] == ["a", "b"]


def test_sessions_tags_remove_empty_tags_returns_400():
    resp = client.post(f"{BASE}/foo/tags/remove", json={"tags": []})
    assert resp.status_code == 400
    assert _error_message(resp) == "At least one tag is required"


def test_sessions_tags_remove_failure_returns_400():
    with patch("nyxgpt.app.sessions.remove_tags", return_value=(False, "No such session")):
        resp = client.post(f"{BASE}/foo/tags/remove", json={"tags": ["a"]})

    assert resp.status_code == 400
    assert _error_message(resp) == "No such session"


def test_sessions_tags_remove_success_returns_ok():
    with patch("nyxgpt.app.sessions.remove_tags", return_value=(True, "OK")) as mock_remove:
        resp = client.post(f"{BASE}/foo/tags/remove", json={"tags": ["a"]})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert mock_remove.call_args[0][1] == ["a"]


# ----------------------------
# sessions_rename
# ----------------------------


def test_sessions_rename_not_found_returns_404():
    with patch(
        "nyxgpt.app.sessions.session_file_for",
        return_value=Path("/nonexistent/does-not-exist.json"),
    ):
        resp = client.post(f"{BASE}/foo/rename", json={"new_name": "bar"})

    assert resp.status_code == 404
    assert "not found" in _error_message(resp)


def test_sessions_rename_sync_mode_title_failure_returns_400(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.set_title",
            return_value=(False, "title rejected"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/rename", json={"new_name": "New Title"})

    assert resp.status_code == 400
    assert _error_message(resp) == "title rejected"


def test_sessions_rename_sync_mode_sync_failure_returns_500(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch("nyxgpt.app.sessions.set_title", return_value=(True, "OK")),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(False, "Session is busy, please try again", "foo"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/rename", json={"new_name": "New Title"})

    assert resp.status_code == 500
    assert "filename sync failed" in _error_message(resp)


def test_sessions_rename_sync_mode_success(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch("nyxgpt.app.sessions.set_title", return_value=(True, "OK")),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(True, "renamed", "new-title"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/rename", json={"new_name": "New Title"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["old_name"] == "foo"
    assert body["new_name"] == "new-title"
    assert body["message"] == "Session renamed and filename synced"


def test_sessions_rename_direct_mode_failure_returns_400(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.rename_session",
            return_value=(False, "target exists"),
        ),
    ):
        resp = client.post(
            f"{BASE}/foo/rename",
            json={"new_name": "bar-baz", "sync_filename": False},
        )

    assert resp.status_code == 400
    assert _error_message(resp) == "target exists"


def test_sessions_rename_direct_mode_success(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch("nyxgpt.app.sessions.rename_session", return_value=(True, "OK")) as mock_rename,
    ):
        resp = client.post(
            f"{BASE}/foo/rename",
            json={"new_name": "bar-baz", "sync_filename": False},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["old_name"] == "foo"
    assert body["new_name"] == "bar-baz"
    assert body["message"] == "Session renamed"
    mock_rename.assert_called_once()


# ----------------------------
# sessions_sync_filename
# ----------------------------


def test_sessions_sync_filename_not_found_returns_404():
    with patch(
        "nyxgpt.app.sessions.session_file_for",
        return_value=Path("/nonexistent/does-not-exist.json"),
    ):
        resp = client.post(f"{BASE}/foo/sync-filename")

    assert resp.status_code == 404
    assert "not found" in _error_message(resp)


def test_sessions_sync_filename_failure_returns_500(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(False, "Session is busy, please try again", "foo"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/sync-filename")

    assert resp.status_code == 500
    assert "Filename sync failed" in _error_message(resp)


def test_sessions_sync_filename_renamed_status(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(True, "renamed", "new-name"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/sync-filename")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "old_name": "foo",
        "new_name": "new-name",
        "message": "Filename synced with title",
    }


def test_sessions_sync_filename_no_title_status(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(True, "no_title", "foo"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/sync-filename")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "message": "No title set, filename unchanged",
        "name": "foo",
    }


def test_sessions_sync_filename_no_change_status(tmp_path: Path):
    existing = tmp_path / "foo.json"
    existing.write_text("[]", encoding="utf-8")
    with (
        patch("nyxgpt.app.sessions.session_file_for", return_value=existing),
        patch(
            "nyxgpt.app.sessions.sync_filename_with_title",
            return_value=(True, "no_change", "foo"),
        ),
    ):
        resp = client.post(f"{BASE}/foo/sync-filename")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "message": "Filename already matches title",
        "name": "foo",
    }

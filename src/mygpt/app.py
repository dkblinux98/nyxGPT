from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from mygpt.config import (
    load_config,
    get_default_model,
    get_ollama_base_url,
    get_sessions_dir,
)
from mygpt.ollama_client import ollama_chat
from mygpt import sessions
from mygpt import tools_fs


app = FastAPI(title="myGPT", version="1.0.0")


# ----------------------------
# Models
# ----------------------------


class InfoResponse(BaseModel):
    ollama_base_url: str
    default_model: str
    sessions_dir: str


class ChatRequest(BaseModel):
    prompt: str
    session: str = "default"
    new: bool = False
    model: Optional[str] = None
    system: Optional[str] = None
    sessions_dir: Optional[str] = None


class ChatResponse(BaseModel):
    session: str
    model: str
    reply: str


class SessionsListResponse(BaseModel):
    sessions: list[dict[str, Any]]


class TitleRequest(BaseModel):
    title: str


class TagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class ToolLsRequest(BaseModel):
    path: str


class ToolCatRequest(BaseModel):
    path: str
    head: Optional[int] = None
    tail: Optional[int] = None


class ToolGrepRequest(BaseModel):
    pattern: str
    path: str
    max: int = 50


class ToolTextResponse(BaseModel):
    output: str


# ----------------------------
# Helpers
# ----------------------------


def _cfg(cfg_path: Path | None = None):
    return load_config(cfg_path)


def _sessions_dir_from_str(s: str | None) -> Path | None:
    if not s:
        return None
    return Path(s).expanduser()


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return int(rc), out.getvalue(), err.getvalue()


# ----------------------------
# Routes
# ----------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/info", response_model=InfoResponse)
def info() -> InfoResponse:
    cfg = _cfg(None)
    base_url = get_ollama_base_url(cfg)
    model = get_default_model(cfg)
    return InfoResponse(
        ollama_base_url=base_url,
        default_model=model,
        sessions_dir=str(get_sessions_dir(cfg)),
    )


@app.get("/sessions", response_model=SessionsListResponse)
def sessions_list(sessions_dir: Optional[str] = None) -> SessionsListResponse:
    cfg = _cfg(None)
    effective_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(cfg)
    rows = sessions.list_sessions(effective_dir)
    # Flatten meta fields for easy UI usage.
    out: list[dict[str, Any]] = []
    for r in rows:
        meta = r.get("meta") or {}
        out.append(
            {
                "name": r.get("name"),
                "messages": r.get("messages"),
                "modified": r.get("modified"),
                "pinned": bool(meta.get("pinned")),
                "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
                "title": meta.get("title") if isinstance(meta.get("title"), str) else "",
                "summary": meta.get("summary") if isinstance(meta.get("summary"), str) else "",
                "token_estimate": meta.get("token_estimate") if isinstance(meta.get("token_estimate"), int) else None,
                "model": meta.get("model") if isinstance(meta.get("model"), str) else "",
            }
        )
    return SessionsListResponse(sessions=out)


@app.get("/sessions/{name}")
def sessions_show(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    sd = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    sf = sessions.session_file_for(name, sd or sessions.default_sessions_dir())
    mf = sessions.meta_file_for(sf)
    msgs = sessions.load_session_messages(sf)
    meta = sessions.load_session_meta(mf)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="No such session")
    return {
        "name": name,
        "messages": msgs,
        "meta": meta,
    }


@app.delete("/sessions/{name}")
def sessions_delete(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok = sessions.delete_session(name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=404, detail="No such session")
    return {"ok": True}


@app.post("/sessions/{name}/summarize")
def sessions_summarize(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.summarize_session(name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/sessions/{name}/pin")
def sessions_pin(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(name, True, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/sessions/{name}/unpin")
def sessions_unpin(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(name, False, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/sessions/{name}/title")
def sessions_title(name: str, req: TitleRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_title(name, req.title, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/sessions/{name}/tags/add")
def sessions_tags_add(name: str, req: TagsRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.add_tags(name, req.tags, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/sessions/{name}/tags/remove")
def sessions_tags_remove(name: str, req: TagsRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.remove_tags(name, req.tags, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    cfg = _cfg(None)
    base_url = get_ollama_base_url(cfg)
    model = req.model or get_default_model(cfg)

    session_file, meta_file, messages, _meta = sessions.init_session(
        session_name=req.session,
        sessions_dir=_sessions_dir_from_str(req.sessions_dir) or get_sessions_dir(cfg),
        new_session=req.new,
        model=model,
        system=req.system,
    )

    messages.append({"role": "user", "content": req.prompt})
    try:
        reply = ollama_chat(base_url=base_url, model=model, messages=messages)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    messages.append({"role": "assistant", "content": reply})
    sessions.persist_after_exchange(session_file=session_file, meta_file=meta_file, messages=messages, model=model)

    return ChatResponse(session=req.session, model=model, reply=reply)


@app.post("/tools/ls", response_model=ToolTextResponse)
def tool_ls(req: ToolLsRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.ls, Path(req.path))
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "ls failed"))
    return ToolTextResponse(output=out)


@app.post("/tools/cat", response_model=ToolTextResponse)
def tool_cat(req: ToolCatRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.cat, Path(req.path), head=req.head, tail=req.tail)
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "cat failed"))
    return ToolTextResponse(output=out)


@app.post("/tools/grep", response_model=ToolTextResponse)
def tool_grep(req: ToolGrepRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.grep, req.pattern, Path(req.path), max_matches=req.max)
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "grep failed"))
    return ToolTextResponse(output=out)
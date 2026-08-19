#!/usr/bin/env python3
"""Executed evidence for #3824: the first chat works, with no manual model pull.

The defect this exists to catch: `nyxgpt ops install` reported every service
healthy on a machine whose Ollama had never downloaded a model. Health checks
passed, the web UI loaded, `ops status` was clean -- and the user's first chat
message failed. No amount of unit testing or manifest review sees that; only
sending the message on a real machine does.

Run against a stack that is already up (the caller's `nyxgpt up` / `ops
install` did the installing -- this script must never pull a model itself, or
it would pre-satisfy the very thing it verifies, ledger V-017). It proves both
halves, per the fault-injection rule (#3753):

  1. The stack the install produced reports both required models present, and
     a first chat message returns a reply.
  2. The embedding model really answers `/api/embed`, so turning RAG on
     mid-session cannot stall on a download.
  3. Injected fault: delete the chat model from the store. Readiness must flip
     to missing and the chat must fail -- otherwise steps 1-2 were passing for
     some other reason and prove nothing.
  4. Re-run the install command. It must pull the model back and the chat must
     work again -- which is the actual claim: the install is what makes the
     first chat possible.

Usage:
  scripts/first-chat-smoke.py [--api-url URL] [--ollama-url URL]
                              [--install-cmd CMD] [--skip-fault-injection]
"""

from __future__ import annotations

import argparse
import configparser
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CHAT_PROMPT = "Reply with exactly one word: OK"
CHAT_SESSION = "first-chat-smoke"


def log(message: str) -> None:
    print(f"[first-chat-smoke] {message}", flush=True)


class SmokeFailure(RuntimeError):
    """A check failed -- the message is what the operator needs to read."""


def _api_key() -> str:
    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg_path.exists():
        return ""
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception:
        return ""
    return parser.get("auth", "api_key", fallback="").strip()


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    api_key: str = "",
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Return `(status, body)`, with transport failures reported as status 0."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 -- unreachable is a status, not a crash
        return 0, str(e)


def readiness(api_url: str, api_key: str) -> dict[str, Any]:
    status, body = _request(f"{api_url}/api/v1/models/required", api_key=api_key, timeout=60.0)
    if status != 200:
        raise SmokeFailure(
            f"GET /api/v1/models/required returned HTTP {status or 'unreachable'}: {body[:400]}"
        )
    return json.loads(body)


def assert_ready(api_url: str, api_key: str) -> dict[str, Any]:
    info = readiness(api_url, api_key)
    if not info.get("reachable"):
        raise SmokeFailure(f"Ollama is unreachable from the api: {info.get('error')}")
    missing = [m["model"] for m in info["models"] if m.get("present") is False]
    if missing:
        raise SmokeFailure(
            "the install left required model(s) unpulled: "
            + ", ".join(missing)
            + " -- this is exactly the state #3824 exists to eliminate. "
            + str(info.get("remediation") or "")
        )
    if not info["models"]:
        raise SmokeFailure("the api reports no required models at all")
    log(
        "required models present: "
        + ", ".join(f"{m['model']} ({m['role']})" for m in info["models"])
    )
    return info


def chat(api_url: str, api_key: str, timeout: float) -> str:
    status, body = _request(
        f"{api_url}/api/v1/chat",
        method="POST",
        payload={"prompt": CHAT_PROMPT, "session": CHAT_SESSION, "new": True},
        api_key=api_key,
        timeout=timeout,
    )
    if status != 200:
        raise SmokeFailure(f"chat returned HTTP {status or 'unreachable'}: {body[:400]}")
    reply = str(json.loads(body).get("reply") or "").strip()
    if not reply:
        raise SmokeFailure(f"chat returned an empty reply: {body[:400]}")
    return reply


def chat_fails(api_url: str, api_key: str, timeout: float) -> str:
    """Return why the chat failed, or raise if it unexpectedly succeeded."""
    try:
        reply = chat(api_url, api_key, timeout)
    except SmokeFailure as e:
        return str(e)
    raise SmokeFailure(
        "chat still answered after the configured chat model was deleted "
        f"(reply: {reply[:80]!r}) -- this check cannot tell a pulled model from an "
        "unpulled one, so a green run would be meaningless"
    )


def embedding_works(ollama_url: str, model: str, timeout: float) -> None:
    """Prove the embedding model is usable, not merely listed.

    This is the RAG half of the acceptance: RAG is a per-session toggle, so a
    user can turn it on at any moment and must not then wait on a download.
    """
    status, body = _request(
        f"{ollama_url.rstrip('/')}/api/embed",
        method="POST",
        payload={"model": model, "input": "nyxgpt first chat smoke"},
        timeout=timeout,
    )
    if status != 200:
        raise SmokeFailure(
            f"embedding request for {model!r} returned HTTP {status or 'unreachable'}: "
            f"{body[:400]} -- a RAG-enabled first message would fail here"
        )
    vectors = json.loads(body).get("embeddings") or []
    if not vectors or not vectors[0]:
        raise SmokeFailure(f"embedding request for {model!r} returned no vector: {body[:200]}")
    log(f"embedding model {model} answered /api/embed with a {len(vectors[0])}-dim vector")


def delete_model(api_url: str, api_key: str, model: str) -> None:
    status, body = _request(
        f"{api_url}/api/v1/models/{model}", method="DELETE", api_key=api_key, timeout=120.0
    )
    if status != 200:
        raise SmokeFailure(f"could not delete {model!r} for fault injection: HTTP {status} {body}")


def run_install(install_cmd: str) -> None:
    log(f"re-running the install: {install_cmd}")
    cp = subprocess.run(shlex.split(install_cmd), text=True, capture_output=True)
    sys.stdout.write(cp.stdout)
    sys.stderr.write(cp.stderr)
    if cp.returncode != 0:
        raise SmokeFailure(f"{install_cmd!r} exited {cp.returncode}")


def wait_for_api(api_url: str, api_key: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        status, _ = _request(f"{api_url}/health", api_key=api_key, timeout=10.0)
        if status == 200:
            return
        if time.monotonic() >= deadline:
            raise SmokeFailure(f"{api_url}/health did not answer 200 within {timeout:.0f}s")
        time.sleep(3.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--install-cmd",
        default="nyxgpt ops install --skip-observability",
        help="The install this asserts on; re-run after the injected fault.",
    )
    parser.add_argument(
        "--skip-fault-injection",
        action="store_true",
        help="Only assert the positive half (for a run that must not spend a second pull).",
    )
    parser.add_argument("--chat-timeout", type=float, default=300.0)
    args = parser.parse_args()

    api_key = _api_key()
    try:
        wait_for_api(args.api_url, api_key, timeout=120.0)

        # 1. What the install produced.
        log("checking the stack the install produced is chattable")
        info = assert_ready(args.api_url, api_key)
        reply = chat(args.api_url, api_key, args.chat_timeout)
        log(f"first chat answered: {reply[:80]!r}")

        # 2. ...including with RAG on, which needs the embedding model.
        embedding = next(
            (m["model"] for m in info["models"] if m["role"] == "embedding"),
            info["models"][0]["model"],
        )
        embedding_works(args.ollama_url, embedding, timeout=180.0)

        if args.skip_fault_injection:
            log("PASS (fault injection skipped by request)")
            return 0

        # 3. Injected fault: the pre-fix state, reproduced exactly -- a stack
        #    whose services are all healthy and whose chat model is absent.
        chat_model = next(
            (m["model"] for m in info["models"] if m["role"] == "chat"),
            info["models"][0]["model"],
        )
        log(f"fault injection: deleting the chat model {chat_model} from the store")
        delete_model(args.api_url, api_key, chat_model)

        after = readiness(args.api_url, api_key)
        if not any(m.get("present") is False for m in after["models"]):
            raise SmokeFailure(
                "readiness still reports every model present after deleting "
                f"{chat_model} -- it is not actually reading the model store"
            )
        log("readiness correctly reports the deleted model missing")
        log("chat correctly failed without it: " + chat_fails(args.api_url, api_key, 120.0)[:200])

        # 4. The claim itself: the install is what makes the first chat work.
        run_install(args.install_cmd)
        wait_for_api(args.api_url, api_key, timeout=120.0)
        assert_ready(args.api_url, api_key)
        reply = chat(args.api_url, api_key, args.chat_timeout)
        log(f"first chat answered again after the install re-pulled it: {reply[:80]!r}")
    except SmokeFailure as e:
        print(f"[first-chat-smoke] ERROR: {e}", file=sys.stderr)
        return 1

    log("PASS: install -> models present -> first chat works, and fails without them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

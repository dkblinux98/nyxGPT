#!/usr/bin/env python3
"""Executed evidence for #3864: the RAG collection endpoints on an *empty* Cassandra.

Owner acceptance of rc12 on EC2 found the RAG Collections page unusable on
every clean deployment: `GET /api/v1/rag/collections` returned HTTP 500 and
Create Collection failed with ``Keyspace 'nyxgpt' does not exist``. Only the
ingest path ever created the keyspace, so until a document happened to be
ingested, list and create both died.

Unit tests cannot close this out. The mocked-driver suites decide for
themselves what a missing keyspace does; only a real Cassandra decides whether
``USE`` on a keyspace that was never created raises, whether the CQL this
product writes actually creates that keyspace, and whether a
``VECTOR<FLOAT, n>`` column with an SAI index is accepted by the server the
owner runs. This script answers those against a live, genuinely empty server,
driving the real API over real HTTP.

Phases (a pass cannot be vacuous -- phase 2 is the fault injection required by
the executed-verification gate: it demonstrates that the pre-fix statement
still fails on this very cluster, so phases 3-6 are proving the fix rather
than proving the cluster was never empty):

  1. Precondition -- the keyspace really does not exist in system_schema.
  2. Fault injection -- ``USE <keyspace>`` raises InvalidRequest here. That is
     the exact statement pre-fix ``list_collections()`` issued, i.e. the
     product before this change fails on this cluster.
  3. GET  /api/v1/rag/collections  -> 200 with an empty list (was 500).
  4. POST /api/v1/rag/collections  -> 201, before anything was ever ingested
     (was 500 "Keyspace 'nyxgpt' does not exist"), and the keyspace, table and
     SAI index now exist on the server.
  5. GET  /api/v1/rag/collections  -> the new collection, with its model.
  6. POST the same name again      -> 409, i.e. the duplicate check survived.

Usage:
    python scripts/rag-fresh-cassandra-smoke.py [--host H] [--port P]
                                                [--keyspace KS]

Exit code 0 on success; non-zero with the failing phase named on failure.
"""

from __future__ import annotations

import argparse
import configparser
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(phase: str, msg: str) -> None:
    log(f"::error::[{phase}] {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def wait_for_port(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError:
            time.sleep(2.0)
    fail("setup", f"nothing listening on {host}:{port} after {timeout:.0f}s")


def write_config(host: str, port: int, keyspace: str) -> Path:
    """Point the API at the smoke cluster via a real ~/.nyxGPT/config.ini."""
    config_path = Path.home() / ".nyxGPT" / "config.ini"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "example.config.ini", config_path)

    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    if not cfg.has_section("rag"):
        cfg.add_section("rag")
    cfg["rag"]["cassandra_hosts"] = host
    cfg["rag"]["cassandra_port"] = str(port)
    cfg["rag"]["cassandra_keyspace"] = keyspace
    with config_path.open("w") as fh:
        cfg.write(fh)

    log(f"[setup] config written: {config_path} (keyspace={keyspace})")
    return config_path


def connect(host: str, port: int):
    """Open a driver session with no keyspace selected."""
    from cassandra.cluster import Cluster

    deadline = time.time() + 300
    last: Exception | None = None
    while time.time() < deadline:
        try:
            cluster = Cluster([host], port=port)
            return cluster, cluster.connect()
        except Exception as exc:  # server up but not yet accepting CQL
            last = exc
            time.sleep(5.0)
    fail("setup", f"could not open a CQL session on {host}:{port}: {last}")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------


class ApiServer:
    """The real API, served by uvicorn, the way the owner's instance serves it."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.log_path = Path("/tmp/rag-fresh-cassandra-smoke-api.log")

    def __enter__(self) -> ApiServer:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.handle = self.log_path.open("w")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "nyxgpt.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "info",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self.handle,
            stderr=subprocess.STDOUT,
        )
        self._wait_ready()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                self.proc.kill()
        self.handle.close()

    def _wait_ready(self) -> None:
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                log(self.log_path.read_text())
                fail("setup", "uvicorn exited before becoming ready")
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=3) as resp:
                    if resp.status == 200:
                        log(f"[setup] API ready on {self.base}")
                        return
            except Exception:
                time.sleep(1.0)
        log(self.log_path.read_text())
        fail("setup", "API never became ready")

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
        import json

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def phase1_keyspace_absent(session, keyspace: str) -> None:
    rows = list(
        session.execute(
            "SELECT keyspace_name FROM system_schema.keyspaces WHERE keyspace_name = %s",
            (keyspace,),
        )
    )
    if rows:
        fail("phase1", f"keyspace {keyspace!r} already exists -- the cluster is not fresh")
    log(f"[phase1] OK: keyspace {keyspace!r} does not exist on this cluster")


def phase2_use_still_fails(session, keyspace: str) -> None:
    from cassandra import InvalidRequest

    try:
        session.execute(f"USE {keyspace}")
    except InvalidRequest as exc:
        if "does not exist" not in str(exc).lower():
            fail("phase2", f"USE failed for an unexpected reason: {exc}")
        log(f"[phase2] OK: pre-fix statement `USE {keyspace}` still fails here -- {exc}")
        return
    fail(
        "phase2",
        f"`USE {keyspace}` succeeded, so this cluster cannot demonstrate the defect "
        "and phases 3-6 would pass vacuously",
    )


def phase3_list_empty(api: ApiServer) -> None:
    import json

    status, body = api.request("GET", "/api/v1/rag/collections")
    if status != 200:
        fail("phase3", f"GET /api/v1/rag/collections returned {status}: {body}")
    payload = json.loads(body)
    if payload.get("collections") != []:
        fail("phase3", f"expected an empty collection list, got: {body}")
    log("[phase3] OK: GET /api/v1/rag/collections -> 200 [] on a keyspace-less Cassandra")


def phase4_create(api: ApiServer, session, keyspace: str, collection: str, table: str) -> None:
    import json

    status, body = api.request(
        "POST",
        "/api/v1/rag/collections",
        {"name": collection, "embedding_dim": 768, "embedding_model": "nomic-embed-text"},
    )
    if status != 201:
        fail("phase4", f"POST /api/v1/rag/collections returned {status}: {body}")

    keyspaces = list(
        session.execute(
            "SELECT keyspace_name FROM system_schema.keyspaces WHERE keyspace_name = %s",
            (keyspace,),
        )
    )
    if not keyspaces:
        fail("phase4", f"endpoint returned 201 but keyspace {keyspace!r} was not created")

    tables = list(
        session.execute(
            "SELECT table_name FROM system_schema.tables "
            "WHERE keyspace_name = %s AND table_name = %s",
            (keyspace, table),
        )
    )
    if not tables:
        fail("phase4", f"table {keyspace}.{table} was not created")

    indexes = {
        row.index_name
        for row in session.execute(
            "SELECT index_name FROM system_schema.indexes WHERE keyspace_name = %s "
            "AND table_name = %s",
            (keyspace, table),
        )
    }
    if f"{table}_embedding_sai" not in indexes:
        fail("phase4", f"SAI vector index missing on {keyspace}.{table}: {sorted(indexes)}")

    log(
        f"[phase4] OK: POST created {keyspace}.{table} with its SAI vector index "
        f"before any ingest ({json.loads(body).get('status')})"
    )


def phase5_list_shows_collection(api: ApiServer, collection: str) -> None:
    import json

    status, body = api.request("GET", "/api/v1/rag/collections")
    if status != 200:
        fail("phase5", f"GET /api/v1/rag/collections returned {status}: {body}")
    collections = json.loads(body).get("collections", [])
    names = [c.get("name") for c in collections]
    if names != [collection]:
        fail("phase5", f"expected exactly [{collection!r}], got {names}")
    if collections[0].get("embedding_model") != "nomic-embed-text":
        fail("phase5", f"submitted embedding model not persisted: {body}")
    log(f"[phase5] OK: the new collection is listed with its model -- {names}")


def phase6_duplicate_conflicts(api: ApiServer, collection: str) -> None:
    status, body = api.request(
        "POST", "/api/v1/rag/collections", {"name": collection, "embedding_dim": 768}
    )
    if status != 409:
        fail("phase6", f"expected 409 on a duplicate create, got {status}: {body}")
    log("[phase6] OK: duplicate create still conflicts (409)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CASSANDRA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CASSANDRA_PORT", "9042")))
    parser.add_argument(
        "--keyspace",
        default=os.environ.get("SMOKE_KEYSPACE", "nyxgpt"),
        help="keyspace that must NOT exist yet (the fault condition)",
    )
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("SMOKE_API_PORT", 8123)))
    parser.add_argument("--collection", default="smoke_fresh")
    args = parser.parse_args()

    log(f"[setup] waiting for Cassandra at {args.host}:{args.port}")
    wait_for_port(args.host, args.port, timeout=300)
    write_config(args.host, args.port, args.keyspace)

    cluster, session = connect(args.host, args.port)
    try:
        phase1_keyspace_absent(session, args.keyspace)
        phase2_use_still_fails(session, args.keyspace)

        with ApiServer(args.api_port) as api:
            phase3_list_empty(api)
            phase4_create(
                api,
                session,
                args.keyspace,
                args.collection,
                f"rag_chunks_{args.collection}",
            )
            phase5_list_shows_collection(api, args.collection)
            phase6_duplicate_conflicts(api, args.collection)
    finally:
        cluster.shutdown()

    log("")
    log("RAG fresh-Cassandra smoke: PASS -- list and create both work before any ingest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

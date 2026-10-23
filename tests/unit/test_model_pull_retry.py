"""A required-model pull survives a transient registry failure (#4192).

On 2026-10-23 the required `terraform-artifact-smoke` check went red on a
commit that had nothing to do with it:

    Failed to pull the chat model 'qwen3.5:0.8b' ([nyxgpt] default_model)
    from Ollama at http://127.0.0.1:11434: Model failed to run -- ...
    (Ollama HTTP 500: {"error":"pull model manifest: 503: upstream connect
    error or disconnect/reset before headers. reset reason: connection
    termination"})

A pull is the one Ollama request whose failures are mostly NOT about this
host: Ollama fetches the manifest and blobs from the public model registry
and relays the registry's failure as its own 5xx. `pull_model` attempted it
once, so one blip failed `nyxgpt ops install` outright -- on a user's machine
as readily as in CI.

"Pull the required models, and what if the pull fails?" is one decision this
tree makes in three places, and before this change they disagreed:

    k8s/statefulset-ollama.yaml  postStart: 5 attempts, 5s doubling  <- right
    docker-compose.yml           entrypoint: one attempt, `|| true`
    nyxgpt.models.pull_model     one attempt                          <- the red check

So these tests hold all three to ONE rule (#4183, D-066) rather than
correcting the copy that happened to be looked at, and they EXECUTE each one:
the Python path against a stub `urlopen` replaying the capture above, the two
shell loops lifted out of their own manifests and run against a stub `ollama`
that fails a fixed number of times. Both directions throughout -- the retired
single-attempt form must fail where the shipped form succeeds, or the test is
asserting nothing.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml

from nyxgpt import model_bootstrap, models, ollama_client

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
K8S_OLLAMA_FILE = REPO_ROOT / "k8s" / "statefulset-ollama.yaml"

#: The body Ollama really returned when the registry refused it, byte for
#: byte out of run 40031233611's log. Fabricating a tidier one would be
#: testing a message this code has never seen.
REGISTRY_503_BODY = (
    '{"error":"pull model manifest: 503: upstream connect error or disconnect/reset '
    'before headers. reset reason: connection termination"}'
)


class _FakeResponse:
    """Enough of an `http.client.HTTPResponse` for `post_json` to read it."""

    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        if isinstance(payload, list):
            self._body = b"".join(json.dumps(event).encode() + b"\n" for event in payload)
        else:
            self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))

    def close(self) -> None:
        return None

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://127.0.0.1:11434/api/pull",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode()),
    )


@pytest.fixture
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff delays instead of waiting them out."""
    slept: list[float] = []
    monkeypatch.setattr(ollama_client.time, "sleep", lambda s: slept.append(s))
    return slept


def _stub_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
    *,
    code: int = 500,
    body: str = REGISTRY_503_BODY,
    success: Any = None,
) -> list[int]:
    """Make the next `failures` requests fail with `code`, then succeed.

    Returns a one-element list holding the number of attempts made, so a test
    can assert the retry happened rather than inferring it from the result.
    """
    calls = [0]

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        calls[0] += 1
        if calls[0] <= failures:
            raise _http_error(code, body)
        return _FakeResponse(success if success is not None else {"status": "success"})

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake_urlopen)
    return calls


# --------------------------------------------------------------------------
# The Python path: the one the red check actually died on.
# --------------------------------------------------------------------------


def test_a_transient_registry_failure_is_retried_and_the_pull_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    calls = _stub_urlopen(monkeypatch, failures=2)

    result = models.pull_model("qwen3.5:0.8b", base_url="http://127.0.0.1:11434")

    assert result == {"status": "success"}
    assert calls[0] == 3, "the pull should have been attempted again after each 5xx"
    assert no_sleeping == [5.0, 10.0], "backoff should double from the k8s copy's 5s"


def test_the_retired_single_attempt_form_fails_on_the_same_bytes(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    """Failure reproduced without the fix (#3775): one attempt, one 503, dead."""
    calls = _stub_urlopen(monkeypatch, failures=2)

    with pytest.raises(ollama_client.ModelRuntimeError) as excinfo:
        models.pull_model("qwen3.5:0.8b", base_url="http://127.0.0.1:11434", attempts=1)

    assert calls[0] == 1
    assert no_sleeping == []
    assert "upstream connect error" in str(
        excinfo.value
    ), "the registry's own reason must still reach the operator"


def test_a_pull_that_exhausts_every_attempt_still_reports_the_upstream_reason(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    calls = _stub_urlopen(monkeypatch, failures=99)

    with pytest.raises(ollama_client.ModelRuntimeError) as excinfo:
        models.pull_model("qwen3.5:0.8b", base_url="http://127.0.0.1:11434")

    assert calls[0] == models.MODEL_PULL_ATTEMPTS
    assert no_sleeping == [5.0, 10.0, 20.0, 40.0], "the last delay is capped, not unbounded"
    assert "upstream connect error" in str(excinfo.value)


@pytest.mark.parametrize("code", [400, 404])
def test_a_definite_answer_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float], code: int
) -> None:
    """A model that does not exist will not start existing after 40 seconds.

    The discriminator is the exception TYPE, not a substring of the message:
    a 4xx becomes `RuntimeError`, a 5xx/timeout becomes `ModelRuntimeError`.
    Matching text instead is the defect this same issue fixed in
    `classify_error`, where `529` inside a commit SHA read as an API overload.
    """
    calls = _stub_urlopen(monkeypatch, failures=99, code=code, body='{"error":"model not found"}')

    with pytest.raises(RuntimeError) as excinfo:
        models.pull_model("no-such-model:1b", base_url="http://127.0.0.1:11434")

    assert not isinstance(excinfo.value, ollama_client.ModelRuntimeError)
    assert calls[0] == 1, "a 4xx must cost one attempt, not five"
    assert no_sleeping == []


def test_a_model_name_containing_503_is_not_what_decides(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    """The adversarial case: the digits are in the model, not in the status."""
    calls = _stub_urlopen(
        monkeypatch, failures=99, code=404, body='{"error":"model \'tiny-503:0.5b\' not found"}'
    )

    with pytest.raises(RuntimeError) as excinfo:
        models.pull_model("tiny-503:0.5b", base_url="http://127.0.0.1:11434")

    assert not isinstance(excinfo.value, ollama_client.ModelRuntimeError)
    assert calls[0] == 1


def test_the_streaming_pull_is_retried_too(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    """`nyxgpt models pull` streams progress; it must not be the unprotected path."""
    calls = _stub_urlopen(
        monkeypatch,
        failures=1,
        success=[{"status": "pulling manifest"}, {"status": "success"}],
    )
    seen: list[str] = []

    result = models.pull_model(
        "qwen3.5:0.8b",
        base_url="http://127.0.0.1:11434",
        progress_callback=lambda status, pct: seen.append(status),
    )

    assert calls[0] == 2
    assert result == {"status": "success"}
    assert seen == ["pulling manifest", "success"]


def test_the_install_step_reports_the_model_installed_after_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    """End to end through the step that reddened the check.

    `ensure_required_models` is what `nyxgpt ops install` (native, `--dev`,
    `--terraform`) runs, and its outcome is what the install reports.
    """
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": "qwen3.5:0.8b"}
    cfg["ollama"] = {"base_url": "http://127.0.0.1:11434"}
    cfg["rag"] = {"embedding_model": "qwen3.5:0.8b"}

    monkeypatch.setattr(model_bootstrap, "installed_model_names", lambda base_url=None: set())
    calls = _stub_urlopen(monkeypatch, failures=2)

    outcomes = model_bootstrap.ensure_required_models(base_url="http://127.0.0.1:11434", cfg=cfg)

    assert [o.ok for o in outcomes] == [True]
    assert calls[0] == 3


# --------------------------------------------------------------------------
# One rule, three copies: the manifests are read, not described.
# --------------------------------------------------------------------------


def _compose_ollama_command() -> str:
    doc = yaml.safe_load(COMPOSE_FILE.read_text())
    command = doc["services"]["ollama"]["command"]
    body = command[0] if isinstance(command, list) else command
    # Compose escapes a literal `$` as `$$`; the container's shell sees one.
    return str(body).replace("$$", "$")


def _k8s_poststart_command() -> str:
    doc = yaml.safe_load(K8S_OLLAMA_FILE.read_text())
    for container in doc["spec"]["template"]["spec"]["containers"]:
        hook = container.get("lifecycle", {}).get("postStart", {}).get("exec", {})
        if hook:
            return str(hook["command"][-1])
    raise AssertionError("k8s/statefulset-ollama.yaml has no postStart hook to check")


def _pull_loop(body: str) -> str:
    """Lift just the model loop out of a container command.

    The surrounding lines start `ollama serve` and `wait` on it, which would
    hang a unit test forever; the loop is the part that makes the decision
    under test, and it is taken from the real file rather than retyped.
    """
    match = re.search(r"^(\s*)for model in .*?^\1done\s*$", body, re.S | re.M)
    assert match, f"no `for model in ... done` loop found in:\n{body}"
    return match.group(0)


@pytest.mark.parametrize("body_source", ["compose", "k8s"])
def test_every_container_copy_retries_five_times_with_doubling_backoff(
    body_source: str,
) -> None:
    body = _compose_ollama_command() if body_source == "compose" else _k8s_poststart_command()
    loop = _pull_loop(body)

    assert "attempt=1" in loop, f"{body_source} does not count attempts"
    assert "-le 5" in loop, f"{body_source} does not stop at five attempts"
    assert "delay=5" in loop, f"{body_source} does not start at the shared 5s delay"
    assert "delay * 2" in loop, f"{body_source} does not double its delay"


def test_the_python_copy_uses_the_same_budget_as_the_containers() -> None:
    """The numbers in three languages are one rule or they are three rules."""
    assert models.MODEL_PULL_ATTEMPTS == 5
    assert models.MODEL_PULL_RETRY_INITIAL_DELAY_S == 5.0
    assert models.MODEL_PULL_RETRY_MAX_DELAY_S == 40.0

    for body in (_compose_ollama_command(), _k8s_poststart_command()):
        loop = _pull_loop(body)
        assert f"-le {models.MODEL_PULL_ATTEMPTS}" in loop
        assert f"delay={int(models.MODEL_PULL_RETRY_INITIAL_DELAY_S)}" in loop


# --------------------------------------------------------------------------
# The shell copies, executed: a stub `ollama` that fails on purpose.
# --------------------------------------------------------------------------

_STUB_OLLAMA = """#!/bin/sh
# Fails `pull` the first $FAIL_TIMES times it is asked, then succeeds.
# `show` always fails, so the loop always reaches a pull.
count_file="$STUB_STATE/pulls"
case "$1" in
  show) exit 1 ;;
  list) exit 0 ;;
  pull)
    n=$(cat "$count_file" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$count_file"
    if [ "$n" -le "$FAIL_TIMES" ]; then
      echo "pull model manifest: 503: upstream connect error" >&2
      exit 1
    fi
    exit 0 ;;
esac
exit 0
"""


def _run_loop(tmp_path: Path, loop: str, fail_times: int) -> tuple[int, int, str]:
    """Run a lifted loop against the stub and return (exit, pulls, output)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ollama"
    stub.write_text(_STUB_OLLAMA)
    stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "pulls").unlink(missing_ok=True)

    script = (
        'NYXGPT_DEFAULT_MODEL="fixture-chat:0.1b"\n'
        'NYXGPT_EMBEDDING_MODEL=""\n'
        "sleep() { :; }\n"  # the backoff is the point, waiting it out is not
        f"{loop}\n"
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "STUB_STATE": str(state),
            "FAIL_TIMES": str(fail_times),
            "HOME": str(tmp_path),
        },
    )
    pulls = int((state / "pulls").read_text().strip()) if (state / "pulls").exists() else 0
    return done.returncode, pulls, done.stdout + done.stderr


@pytest.mark.parametrize("body_source", ["compose", "k8s"])
def test_the_lifted_loop_survives_two_transient_failures(tmp_path: Path, body_source: str) -> None:
    body = _compose_ollama_command() if body_source == "compose" else _k8s_poststart_command()
    rc, pulls, out = _run_loop(tmp_path, _pull_loop(body), fail_times=2)

    assert pulls == 3, f"{body_source} gave up early:\n{out}"
    assert rc == 0, f"{body_source} reported failure after a successful pull:\n{out}"


def test_the_retired_single_attempt_loop_dies_on_the_first_failure(tmp_path: Path) -> None:
    """Fault injection: the shape each manifest used to have, on the same stub.

    Compose's retired form swallowed the failure with `|| true` and the
    container then sat unhealthy; the point here is the single attempt, which
    is what both retired forms share.
    """
    retired = (
        'for model in "$NYXGPT_DEFAULT_MODEL" "$NYXGPT_EMBEDDING_MODEL"; do\n'
        '  [ -n "$model" ] || continue\n'
        '  ollama show "$model" >/dev/null 2>&1 || ollama pull "$model" || true\n'
        "done"
    )
    _rc, pulls, out = _run_loop(tmp_path, retired, fail_times=2)

    assert pulls == 1, f"the retired form must stop at one attempt, got {pulls}:\n{out}"


@pytest.mark.parametrize("body_source", ["compose", "k8s"])
def test_a_model_already_present_is_never_pulled(tmp_path: Path, body_source: str) -> None:
    """The retry must not cost anything on a warm volume."""
    body = _compose_ollama_command() if body_source == "compose" else _k8s_poststart_command()
    loop = _pull_loop(body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "ollama"
    stub.write_text('#!/bin/sh\ncase "$1" in pull) echo PULLED; exit 0 ;; esac\nexit 0\n')
    stub.chmod(0o755)

    done = subprocess.run(
        ["/bin/sh", "-c", f'NYXGPT_DEFAULT_MODEL="warm:1b"\nNYXGPT_EMBEDDING_MODEL=""\n{loop}\n'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert "PULLED" not in done.stdout, f"{body_source} re-pulled a model it already had"

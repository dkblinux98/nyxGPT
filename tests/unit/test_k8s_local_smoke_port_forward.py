"""How `k8s-local-smoke.sh` reaches the web UI -- and why it no longer forwards.

**History, because it is the point.** This file was written for #3825, when the
smoke opened its own `kubectl port-forward svc/nyxgpt-web 3000:3000`. Step 8
healed a web Pod for real and returned as soon as the REPLACEMENT POD EXISTED,
not when its server was listening; the tunnel opened seconds later, `kubectl
port-forward` treated the first refused in-pod connection as fatal ("error:
lost connection to pod") and exited, and the probe loop spent its full budget
curling a process that had died in the first two seconds -- failing with "the
UI itself is unreachable" while every Pod in the cluster was Running/Ready.
Run 32214550593 is that failure and run 32207748890 is the same script content
passing, which is the definition of a race. The fix was to rebuild the tunnel
and to wait for the Deployment first.

**#3986 removed the tunnel instead**, and with it the race. Two things changed,
and this file now guards both:

* The smoke opens no forward at all. That it ever did was itself the blind
  spot: the smoke reached the UI because IT had built a tunnel, while an
  operator finishing the very same install had nothing listening on the host
  -- which is the acceptance failure #3986 reports. Every user-path step now
  drives `http://127.0.0.1:3000` exactly as a browser does, and reaching it is
  an assertion rather than setup.
* Where a forward is still unavoidable (a bring-your-own cluster), it is
  `ops._supervise_port_forward` that owns it, and restarting a dead one is its
  whole job -- see `test_supervisor_restarts_a_forward_that_died` in
  test_k8s_host_access.py. The #3825 failure mode is handled there, in code,
  rather than in a shell loop inside a test harness.

The pre-fix loop is still reconstructed below and driven through the same
stubs, so "the tunnel shape was fragile" stays a measured fact rather than a
claim -- and the shipped `wait_for_web` is driven through an address that only
starts answering on the fourth probe, so its retry is measured too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "k8s-local-smoke.sh"

# The pre-#3825 loop, verbatim in shape: one tunnel, opened once, never
# rebuilt, and no wait for the web Deployment to report available. Kept here so
# the fault injection below is a real comparison rather than a claim about one.
PRE_FIX_FUNCTIONS = """
start_port_forward() {
    kubectl -n "$NAMESPACE" port-forward "svc/nyxgpt-web" "${WEB_PORT}:3000" \
        >/tmp/k8s-smoke-portforward.log 2>&1 &
    PF_PID=$!
    for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then return 0; fi
        sleep 2
    done
    fail "web Service never answered on ${BASE} -- the UI itself is unreachable"
}
"""


def _shipped_wait_for_web() -> str:
    """The real `wait_for_web` body, sliced out of the shipped script.

    Sourcing the whole script would run the fifteen-step smoke, so the tests
    execute exactly the region under test -- and read it from the shipped file
    rather than restating it, so a regression in the script fails these tests.
    """
    text = SMOKE_SCRIPT.read_text()
    start = text.index("wait_for_web() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _write_stubs(tmp_path: Path, *, answers_on: int) -> Path:
    """A PATH whose `curl` starts answering only on the `answers_on`-th probe.

    Models the real window this covers -- kube-proxy programming a freshly
    published node port, or a web Pod a second or two from listening -- without
    needing either. `kubectl port-forward` dies on its first open exactly as it
    did on the runner, so the pre-fix loop below meets the same race it met
    there. `sleep` is a no-op so a 90s budget costs milliseconds.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    (bin_dir / "kubectl").write_text(f"""#!/usr/bin/env bash
STATE="{state}"
echo "$*" >>"$STATE/kubectl.log"
case "$*" in
    *"rollout status"*) exit 0 ;;
    *port-forward*)
        n=$(cat "$STATE/opens" 2>/dev/null || echo 0)
        n=$((n + 1))
        echo "$n" >"$STATE/opens"
        # "error: lost connection to pod" -- the Pod refused :3000.
        exit 1
        ;;
esac
exit 0
""")
    (bin_dir / "curl").write_text(f"""#!/usr/bin/env bash
STATE="{state}"
n=$(cat "$STATE/probes" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" >"$STATE/probes"
[ "$n" -ge {answers_on} ]
""")
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "nyxgpt").write_text("#!/usr/bin/env bash\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, functions: str, entry: str, *, answers_on: int = 4):
    bin_dir = _write_stubs(tmp_path, answers_on=answers_on)
    harness = f"""
set -u
NAMESPACE=nyxgpt
WEB_PORT=3000
BASE="http://127.0.0.1:${{WEB_PORT}}"
PF_PID=""
fail() {{ echo "[FAIL] $*" >&2; exit 1; }}
{functions}
{entry} && echo "[WEB-OK]"
"""
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=120,
    )


def test_the_smoke_opens_no_tunnel_of_its_own(tmp_path: Path) -> None:
    """The blind spot #3986 removed, asserted against the shipped script.

    A smoke that builds its own tunnel cannot see an install that leaves the
    operator without one -- which is the whole acceptance failure. This is the
    guard that stops a future edit from quietly restoring it.
    """
    text = SMOKE_SCRIPT.read_text()

    assert "wait_for_web() {" in text
    assert "start_port_forward" not in text
    # `nyxgpt ops port-forward --status` is a diagnostic the script prints; an
    # actual `kubectl port-forward` is what it must never run.
    assert 'kubectl -n "$NAMESPACE" port-forward' not in text
    assert 'port-forward "svc/nyxgpt-web"' not in text


def test_the_pre_fix_tunnel_loop_never_recovers_from_a_tunnel_that_died(tmp_path: Path) -> None:
    """Fault injection, kept: the tunnel shape really was fragile.

    The tunnel dies on its first open, the old loop reopens nothing, and every
    probe is spent against a process that no longer exists -- the exact shape
    of run 32214550593, and the reason the smoke no longer depends on one.
    """
    result = _run(tmp_path, PRE_FIX_FUNCTIONS, "start_port_forward", answers_on=10_000)

    assert result.returncode != 0, (
        "the pre-#3825 loop recovered from a dead tunnel -- the stub is not "
        "reproducing the race this test exists to measure"
    )
    assert "web Service never answered" in result.stderr
    assert "[WEB-OK]" not in result.stdout
    assert (tmp_path / "state" / "opens").read_text().strip() == "1"


def test_wait_for_web_retries_until_the_address_answers(tmp_path: Path) -> None:
    """The shipped probe tolerates the window without owning a process.

    A freshly published node port takes a moment to be programmed, and a web
    Pod replaced in step 7 takes a moment to listen. Neither is a failure, and
    neither needs a tunnel to be rebuilt -- there is no tunnel.
    """
    result = _run(tmp_path, _shipped_wait_for_web(), "wait_for_web", answers_on=4)

    assert result.returncode == 0, result.stderr
    assert "[WEB-OK]" in result.stdout
    assert int((tmp_path / "state" / "probes").read_text().strip()) >= 4, (
        "the probe succeeded on its first attempt -- the stub is not exercising "
        "the retry this test measures"
    )
    assert not (tmp_path / "state" / "opens").exists(), (
        "wait_for_web opened a port-forward -- it must reach the address the "
        "install established, not build its own (#3986)"
    )


def test_wait_for_web_fails_with_the_reason_that_names_the_defect(tmp_path: Path) -> None:
    """An address that never answers is #3986's failure, and must read as it."""
    result = _run(tmp_path, _shipped_wait_for_web(), "wait_for_web", answers_on=10_000)

    assert result.returncode != 0
    assert "with no port-forward running" in result.stderr
    assert "#3986" in result.stderr

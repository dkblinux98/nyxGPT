"""`start_port_forward` must survive a web Pod that is not listening yet (#3825).

Why this file exists, rather than a ledger entry saying the same thing.
`k8s-local-smoke` step 8 heals a web Pod for real and returns as soon as the
REPLACEMENT POD EXISTS -- not when its server is listening. Step 9 then opened
the tunnel roughly two seconds later. `kubectl port-forward` treats the first
refused in-pod connection as fatal ("error: lost connection to pod") and exits,
and the old probe loop had no notion of a dead tunnel: it spent its full 60s
budget curling a process that had died in the first two seconds, then failed
with "web Service never answered -- the UI itself is unreachable" while every
Pod in the cluster was Running/Ready. Run 32214550593 is that failure; the
identical script content passed on run 32207748890, which is the definition of
a race rather than a defect in whatever PR happened to be under review.

The real race needs a live kind cluster and a three-second window, so it cannot
be reproduced here. What CAN be executed here is the logic that turned the
window into a hard failure, and that is what these tests do: they run the real
`start_port_forward` body out of the shipped script against stubbed `kubectl`,
`curl` and `sleep`, with the stub tunnel dying on its first open exactly as
kubectl did on the runner. The pre-fix loop is reconstructed alongside it and
driven through the same stubs, so the fix is measured by the difference rather
than asserted -- the old shape fails, the shipped shape recovers.
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


def _shipped_functions() -> str:
    """The real `_open_tunnel` / `start_port_forward` bodies, sliced out.

    Sourcing the whole script would run the nine-step smoke, so the tests
    execute exactly the region under test -- and read it from the shipped file
    rather than restating it, so a regression in the script fails these tests.
    """
    text = SMOKE_SCRIPT.read_text()
    start = text.index("_open_tunnel() {")
    end = text.index("stop_port_forward() {")
    body = text[start:end]
    assert "start_port_forward() {" in body, (
        "scripts/k8s-local-smoke.sh no longer defines start_port_forward between "
        "_open_tunnel and stop_port_forward -- this test is slicing the wrong region"
    )
    return body


def _write_stubs(tmp_path: Path) -> Path:
    """A PATH whose kubectl reproduces the runner's racing tunnel.

    First `port-forward` dies immediately, the way kubectl does when the Pod
    refuses the connection; any later one stays up and marks the tunnel live.
    `curl` answers only while that marker exists, and `sleep` is a no-op so the
    60s budget costs milliseconds here.
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
        if [ "$n" -eq 1 ]; then
            # "error: lost connection to pod" -- the Pod refused :3000.
            rm -f "$STATE/tunnel_up"
            exit 1
        fi
        touch "$STATE/tunnel_up"
        exec sleep 300
        ;;
esac
exit 0
""")
    (bin_dir / "curl").write_text(f"""#!/usr/bin/env bash
test -e "{state}/tunnel_up"
""")
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, functions: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _write_stubs(tmp_path)
    harness = f"""
set -u
NAMESPACE=nyxgpt
WEB_PORT=3000
BASE="http://127.0.0.1:${{WEB_PORT}}"
PF_PID=""
fail() {{ echo "[FAIL] $*" >&2; exit 1; }}
{functions}
start_port_forward && echo "[TUNNEL-OK]"
"""
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=120,
    )


def test_the_pre_fix_loop_never_recovers_from_a_tunnel_that_died(tmp_path: Path) -> None:
    """Fault injection: without the fix a three-second window is a hard failure.

    This is the half that must fail. The tunnel dies on its first open, the old
    loop reopens nothing, and all thirty probes are spent against a process
    that no longer exists -- the exact shape of run 32214550593.
    """
    result = _run(tmp_path, PRE_FIX_FUNCTIONS)

    assert result.returncode != 0, (
        "the pre-#3825 loop recovered from a dead tunnel -- the stub is not "
        "reproducing the race this test exists to measure"
    )
    assert "web Service never answered" in result.stderr
    assert "[TUNNEL-OK]" not in result.stdout
    assert (
        tmp_path / "state" / "opens"
    ).read_text().strip() == "1", "the pre-fix loop opened the tunnel more than once -- it did not"


def test_the_shipped_loop_rebuilds_the_tunnel_and_recovers(tmp_path: Path) -> None:
    """The same injected death, run through the shipped function, succeeds."""
    result = _run(tmp_path, _shipped_functions())

    assert (
        result.returncode == 0
    ), f"start_port_forward still fails when the first tunnel dies:\n{result.stderr}"
    assert "[TUNNEL-OK]" in result.stdout
    assert int((tmp_path / "state" / "opens").read_text().strip()) > 1, (
        "the tunnel was never rebuilt -- recovery came from somewhere other "
        "than the fix, so this test is not measuring it"
    )


def test_the_web_deployment_is_awaited_before_the_tunnel_opens(tmp_path: Path) -> None:
    """The settle that removes the race, rather than tolerating it.

    Rebuilding the tunnel recovers from the window; waiting for the Deployment
    to report available avoids opening inside it at all. Both matter -- a Pod
    can refuse a connection for reasons other than the heal in step 8 -- so the
    ordering is pinned here and cannot be dropped as redundant.
    """
    result = _run(tmp_path, _shipped_functions())
    assert result.returncode == 0

    calls = (tmp_path / "state" / "kubectl.log").read_text().splitlines()
    rollout = [i for i, c in enumerate(calls) if "rollout status" in c]
    forwards = [i for i, c in enumerate(calls) if "port-forward" in c]
    assert rollout, "start_port_forward no longer waits for the web Deployment at all"
    assert forwards, "start_port_forward no longer opens a tunnel"
    assert rollout[0] < forwards[0], (
        "the tunnel is opened before the web Deployment reports available -- "
        "that is the #3825 race, restored"
    )
    assert (
        "deployment/nyxgpt-web-stable" in calls[rollout[0]]
    ), "the wait names a workload other than the Deployment behind svc/nyxgpt-web"

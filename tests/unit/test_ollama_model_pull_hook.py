"""The ollama Pod's model-pull hook, EXECUTED rather than inspected (#3956).

The 2026-08-22 acceptance failure of `nyxgpt cloud deploy --kubernetes` had a
second defect underneath the DNS collision that caused it, and this file is
about that one: `k8s/statefulset-ollama.yaml`'s `postStart` hook ended in a
blanket `|| true`, so both model pulls failed instantly against a dead cluster
DNS, the hook reported success, and the Pod sat 0/1 behind a readiness probe
that could never pass. The deploy surfaced it as "Ollama did not become ready
in time" -- true, and three layers from the cause, with the actual error
(`lookup registry.ollama.ai ...: connection refused`) recorded nowhere.

That class of defect is invisible to a manifest-shape assertion: the old hook
and the new one both contain the string `ollama pull`. So these tests pull the
hook script out of the manifest and RUN it under `sh` against a stubbed
`ollama`, and assert on its exit status and its output -- which is what kubelet
reads to decide whether to record a `FailedPostStartHook` event.

`sleep` is stubbed too. The real backoff is 5 + 10 + 20 + 40 seconds, which is
the right wait on an instance and the wrong one in a unit test; stubbing it
keeps the attempt COUNT under test while removing the wall clock.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "k8s" / "statefulset-ollama.yaml"

DEFAULT_MODEL = "qwen3:0.6b"
EMBEDDING_MODEL = "nomic-embed-text"


def _hook_script() -> str:
    """The postStart script exactly as the manifest ships it."""
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    return container["lifecycle"]["postStart"]["exec"]["command"][-1]


def _run_hook(tmp_path: pathlib.Path, *, present: str, pull_fails_first: int, pull_ever: bool):
    """Run the hook with a stub `ollama` on PATH.

    `present` is a space-separated list of models already in the volume,
    `pull_fails_first` how many pull attempts fail before one succeeds, and
    `pull_ever` whether a pull ever succeeds at all.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "pull-attempts"
    counter.write_text("", encoding="utf-8")

    ollama = bindir / "ollama"
    ollama.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list) exit 0 ;;\n"
        f'  show) case " {present} " in *" $2 "*) exit 0 ;; *) exit 1 ;; esac ;;\n'
        "  pull)\n"
        f'    echo "$2" >> "{counter}"\n'
        # Per model, not global: the hook retries each model independently.
        f'    attempts=$(grep -Fx -c "$2" "{counter}")\n'
        f"    if [ \"{'1' if pull_ever else '0'}\" -eq 1 ] && "
        f'[ "$attempts" -gt {pull_fails_first} ]; then exit 0; fi\n'
        '    echo "pull model manifest: Get \\"https://registry.ollama.ai/...\\": '
        'dial tcp: lookup registry.ollama.ai on 100.97.0.10:53: connection refused" >&2\n'
        "    exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ollama.chmod(0o755)

    # The real backoff is minutes; the attempt count is what is under test.
    sleep = bindir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)

    home = tmp_path / "root" / ".ollama"
    home.mkdir(parents=True)
    script = _hook_script().replace("/root/.ollama", str(home))

    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "NYXGPT_DEFAULT_MODEL": DEFAULT_MODEL,
            "NYXGPT_EMBEDDING_MODEL": EMBEDDING_MODEL,
        },
        timeout=60,
    )
    attempts = [line for line in counter.read_text(encoding="utf-8").splitlines() if line]
    return result, attempts, home


def test_a_pull_that_never_succeeds_fails_the_hook(tmp_path):
    """The defect, directly: this exact run used to exit 0.

    Exiting non-zero is what makes kubelet record a `FailedPostStartHook`
    event -- the operator-visible record the old hook denied them -- and what
    makes the Pod retry instead of sitting 0/1 forever.
    """
    result, attempts, _ = _run_hook(tmp_path, present="", pull_fails_first=99, pull_ever=False)
    assert result.returncode != 0, (
        "the hook reported success with neither model pulled -- this is the "
        "2026-08-22 acceptance failure (#3956)"
    )
    assert len(attempts) == 10, attempts  # 5 attempts x 2 models


def test_the_ultimate_failure_carries_the_registry_error_kubelet_will_show(tmp_path):
    """ "Ollama did not become ready in time" is a symptom, not a diagnosis.

    kubelet puts the hook's own output in the event it records, so the error
    the registry actually returned has to be IN that output.
    """
    result, _, home = _run_hook(tmp_path, present="", pull_fails_first=99, pull_ever=False)
    combined = result.stdout + result.stderr
    assert "connection refused" in combined
    assert DEFAULT_MODEL in combined
    assert "giving up" in combined
    # ...and a copy survives on the volume, for a Pod already restarted past
    # the event's retention.
    assert (home / "nyxgpt-model-pull.err").read_text(encoding="utf-8").strip()


def test_a_transient_failure_is_retried_rather_than_swallowed(tmp_path):
    """The `|| true` it replaces did not retry at all.

    Two failures then a success is the transient-registry case the old comment
    claimed to protect, and it now ends with the model actually pulled.
    """
    result, attempts, _ = _run_hook(tmp_path, present="", pull_fails_first=2, pull_ever=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(attempts) == 6  # (2 failures + 1 success) x 2 models


def test_an_offline_restart_with_both_models_present_is_a_no_op(tmp_path):
    """The case the blanket `|| true` existed for, and the one that must stay.

    A Pod restarting with its volume already populated must not pull, must not
    fail, and must not crash-loop a cluster with no route to the registry.
    """
    result, attempts, _ = _run_hook(
        tmp_path,
        present=f"{DEFAULT_MODEL} {EMBEDDING_MODEL}",
        pull_fails_first=99,
        pull_ever=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == []


def test_only_the_missing_model_is_pulled(tmp_path):
    """A partly-populated volume pulls the gap, not both."""
    result, attempts, _ = _run_hook(
        tmp_path, present=DEFAULT_MODEL, pull_fails_first=0, pull_ever=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == [EMBEDDING_MODEL]


def test_the_hook_no_longer_ends_in_a_blanket_true():
    """The specific construct that swallowed the failure, named."""
    script = _hook_script()
    assert "|| true" not in script
    assert script.rstrip().endswith('exit "$rc"')


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_the_hook_parses_under_a_posix_shell(tmp_path, shell):
    """The container's shell is BusyBox/ash, not bash."""
    script = tmp_path / "hook.sh"
    script.write_text(_hook_script(), encoding="utf-8")
    assert subprocess.run([shell, "-n", str(script)], capture_output=True).returncode == 0

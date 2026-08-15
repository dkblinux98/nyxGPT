#!/usr/bin/env python3
"""Fault injection for the Linux Ollama bootstrap (#3508 acceptance).

`scripts/systemd-native-smoke.sh` proves the *fixed* behaviour: on a machine
with no `ollama`, `nyxgpt up` installs it and brings `nyxgpt-ollama.service`
up. That half alone is not enough evidence -- a runner that happened to ship
Ollama would pass it for the wrong reason, and it says nothing about whether
the check could ever have caught the defect it was written for.

So this script proves the other half, the way `macos-brew-smoke.yml`'s
`mac_ver()` step does: it restores the pre-fix behaviour (the native Ollama
install step does *not* install Ollama, it just reports "ollama not found on
PATH" and hands the operator a `curl ... | sh` to run) and asserts that the
step fails. Run before the smoke script, while the runner still has no
Ollama on PATH.

Exits 0 when the injected pre-fix behaviour is correctly detected as a
failure, 1 otherwise.

Usage:
    python3 scripts/ollama-bootstrap-smoke.py
"""

from __future__ import annotations

import platform
import shutil
import sys

from nyxgpt import ops


def main() -> int:
    if platform.system() != "Linux":
        print("SKIP: this check only exercises the Linux native path", file=sys.stderr)
        return 0

    if shutil.which("ollama") is not None:
        print(
            "ERROR: `ollama` is already on PATH, so the pre-fix behaviour cannot be "
            "reproduced here. This check must run before anything installs Ollama.",
            file=sys.stderr,
        )
        return 1

    # The pre-fix code: `_install_native_ollama_systemd` went straight to the
    # port-conflict check and the `_which("ollama")` lookup, with no install
    # step of its own. Returning nothing from the bootstrap reproduces that
    # exactly, without needing the old source.
    original = ops._ensure_ollama_installed
    ops._ensure_ollama_installed = lambda: []  # type: ignore[assignment]
    try:
        results = ops._install_native_ollama_systemd()
    finally:
        ops._ensure_ollama_installed = original  # type: ignore[assignment]

    for r in results:
        print(f"  {'OK' if r.ok else 'FAIL'} {r.message}")

    failures = [r for r in results if not r.ok]
    if not failures:
        print(
            "ERROR: with the Ollama bootstrap disabled, the native Ollama install step "
            "still reported success on a machine with no Ollama. The smoke test's "
            "green result would therefore not be evidence of the fix.",
            file=sys.stderr,
        )
        return 1

    if not any("ollama not found on PATH" in r.message for r in failures):
        print(
            "ERROR: the step failed, but not with the pre-fix 'ollama not found on "
            f"PATH' verdict: {[r.message for r in failures]}. Something other than the "
            "missing bootstrap is failing, so this is not the injection it claims.",
            file=sys.stderr,
        )
        return 1

    print(
        "\nPre-fix behaviour reproduced: without the bootstrap, the native Ollama "
        "install step fails with 'ollama not found on PATH' on a clean machine.\n"
        "The smoke run that follows exercises the same step with the bootstrap in "
        "place, so its green result is the fix and not the runner's luck."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Executed evidence for the cloud account/SSH prompts (#4186, #3775 gate).

Unit tests can prove the resolver's logic. They cannot prove the three things
this issue is actually about, because all three are properties of a *run*:

1. **A real terminal gets real prompts, and Enter is enough.** The owner's
   stop was a command that refused instead of asking. Only driving the shipped
   `nyxgpt cloud infra plan` under a pty, typing nothing but newlines, shows
   that the prompts appear, carry their defaults, and are satisfied by Enter.
2. **A run with no terminal never waits.** `nyxgpt cloud ops` goes over SSH
   and CI has no stdin; a prompt there is a hang, which no unit test that
   stubs `input` can observe. Each non-interactive shape is run under a hard
   timeout, so a regression that waits for input fails this job instead of
   hanging somebody's pipeline.
3. **The fingerprints really are the ones EC2 reports.** The resolver matches
   a local public key to an EC2 key pair by fingerprint, and hand-rolls the
   DER re-encoding an imported RSA pair's MD5 is taken over. The unit suite
   pins vectors; this recomputes them with `ssh-keygen` and `openssl` on the
   runner, which is the only check that is not the implementation agreeing
   with itself.

Nothing here reaches AWS or creates anything: the run is stopped at a stub
`terraform` on PATH, which is before the first AWS call and long before
anything bills. The one piece that cannot be executed in CI is the live
`describe_key_pairs` (there is no AWS account here, by design -- see
docs/live-verification-ci.md); its transport is faked while the fingerprint it
returns is computed by `openssl`, so the match itself is still proved rather
than asserted.

Every check also has its inverse (#3753's rule): a condition is injected under
which the shipped behaviour must fail, so a check cannot pass vacuously on a
runner that happens not to reproduce the case.

Run: python scripts/cloud-identity-prompt-smoke.py
"""

from __future__ import annotations

import base64
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The stub `terraform` every case puts first on PATH. Two jobs: make the run
# stop at a known pre-AWS point whether or not the runner image ships a real
# Terraform, and make that stop loud enough to recognise in the transcript.
TERRAFORM_STUB = """#!/bin/sh
echo "STUB-TERRAFORM-REFUSED: the resolver has already run; nothing may reach AWS here" >&2
exit 1
"""

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion, printing it either way so the log is the evidence."""
    global checks
    checks += 1
    if condition:
        print(f"  [ok] {label}")
        return
    print(f"  [FAIL] {label}" + (f"\n         {detail}" if detail else ""))
    failures.append(label)


def section(title: str) -> None:
    """Print a section header."""
    print(f"\n--- {title} ---")


def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run `command`, capturing text output."""
    return subprocess.run(command, capture_output=True, text=True, check=False, **kwargs)  # type: ignore[arg-type]


# --- The synthetic operator machine --------------------------------------


def make_home(with_ssh_key: bool) -> Path:
    """A HOME with no `~/.nyxGPT/cloud/infra.json` and, optionally, one SSH key.

    `with_ssh_key=False` is the inverse case: a machine the resolver has
    nothing to offer from, which must *fail* non-interactively rather than
    succeed by accident.
    """
    home = Path(tempfile.mkdtemp(prefix="cloud-identity-home-"))
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    if with_ssh_key:
        run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "owner@workstation",
                "-f",
                str(ssh_dir / "id_ed25519"),
                "-q",
            ]
        )
    bin_dir = home / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "terraform"
    stub.write_text(TERRAFORM_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return home


def command_env(home: Path, **extra: str) -> dict[str, str]:
    """The environment a probed `nyxgpt` runs in.

    HOME is redirected, which also moves the user site-packages an editable
    install lives in -- so both the checkout's `src` and the real user site
    are put back on `PYTHONPATH` explicitly. Without that the probe would be
    testing a `nyxgpt` that cannot import itself.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join([str(home / "stub-bin"), env.get("PATH", "")])
    user_site = next(
        (p for p in sys.path if p.endswith("site-packages") and "/.local/" in p),
        "",
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), user_site, env.get("PYTHONPATH", "")]
    )
    # Nothing in this job has AWS credentials, and an inherited empty-string
    # AWS_* variable makes botocore's own resolution noisier than the probe.
    for name in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        env.pop(name, None)
    env.update(extra)
    return env


# --- 1. The interactive path, satisfied with Enter alone -----------------


def drive_under_pty(home: Path, newlines: int = 3, timeout: float = 90.0) -> tuple[str, int]:
    """Run `nyxgpt cloud infra plan` on a real pty, sending only newlines.

    A pty rather than a pipe deliberately: `prompting_enabled` asks whether
    stdin and stdout are terminals, so a piped probe would exercise the
    non-interactive path and prove nothing about the prompts.
    """
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "nyxgpt", "cloud", "infra", "plan"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=command_env(home),
        close_fds=True,
    )
    os.close(slave)
    transcript = b""
    deadline = time.time() + timeout
    sent = 0
    while time.time() < deadline:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        transcript += chunk
        # Answer each prompt as it arrives rather than on a guessed sleep: a
        # prompt is the last thing written and ends in ": " by construction,
        # and the pty echoes our newline back, so the next read no longer
        # matches. `sent` bounds it either way.
        if sent < newlines and transcript.rstrip(b" ").endswith(b":"):
            os.write(master, b"\n")
            sent += 1
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        return transcript.decode(errors="replace"), -1
    os.close(master)
    return transcript.decode(errors="replace"), int(process.returncode or 0)


def case_interactive_defaults() -> None:
    """A terminal, no `infra.json`, nothing typed but Enter."""
    section("1. interactive: both defaults accepted with Enter, no infra.json")
    home = make_home(with_ssh_key=True)
    transcript, code = drive_under_pty(home)
    print("\n".join("      | " + line for line in transcript.splitlines()))

    check("the command asked for the AWS profile", "AWS profile" in transcript, transcript)
    check(
        "the SSH key was offered as a numbered choice with a default",
        "SSH key for the instance" in transcript and "(default)" in transcript,
        transcript,
    )
    check(
        "the offered key is the one on this machine",
        "id_ed25519.pub" in transcript,
        transcript,
    )
    check(
        "the private half was offered as the identity to authenticate with",
        "Private key to authenticate with" in transcript,
        transcript,
    )
    check(
        "the run announced the account and key it settled on",
        "AWS account:" in transcript and "SSH key:" in transcript,
        transcript,
    )
    check(
        "the run stopped at the stub terraform, before any AWS call",
        "STUB-TERRAFORM-REFUSED" in transcript,
        transcript,
    )
    check("the run did not have to be killed", code != -1, f"exit {code}")

    # The resolver's answer is recorded, which is what makes it visible to
    # `cloud status` and the dashboard afterwards with no credential.
    recorded = home / ".nyxGPT" / "cloud" / "infra.json"
    check("the choice was recorded in infra.json", recorded.is_file(), str(recorded))
    if recorded.is_file():
        text = recorded.read_text(encoding="utf-8")
        check("the recorded key is the one offered", "id_ed25519" in text, text)
    shutil.rmtree(home, ignore_errors=True)


# --- 2. The non-interactive shapes, none of which may wait ---------------


def case_non_interactive_shapes() -> None:
    """`--yes`, a pipe, and the environment override: defaults taken, nothing waited for."""
    section("2. non-interactive: defaults taken, the choice printed, nothing waits")
    home = make_home(with_ssh_key=True)
    shapes = [
        ("--yes with stdin from /dev/null", ["--yes"], {}),
        ("no flag, no terminal (a pipe)", [], {}),
        ("NYXGPT_CLOUD_NONINTERACTIVE set", [], {"NYXGPT_CLOUD_NONINTERACTIVE": "1"}),
    ]
    for label, flags, extra in shapes:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "nyxgpt", "cloud", "infra", "plan", *flags],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=command_env(home, **extra),
                # The hard bound IS the assertion: a resolver that prompts here
                # would sit on a closed stdin forever.
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            check(f"{label}: did not wait for input", False, "timed out after 60s")
            continue
        elapsed = time.monotonic() - started
        output = completed.stdout + completed.stderr
        print("\n".join("      | " + line for line in output.splitlines()))
        check(f"{label}: finished without waiting", elapsed < 60, f"{elapsed:.1f}s")
        check(
            f"{label}: printed the account it chose",
            "AWS account:" in output,
            output,
        )
        check(f"{label}: printed the SSH key it chose", "SSH key:" in output, output)
        check(
            f"{label}: asked nothing",
            "Private key to authenticate with" not in output,
            output,
        )
    shutil.rmtree(home, ignore_errors=True)


def case_no_usable_default() -> None:
    """The inverse: with nothing to default to, the scripted run must fail and say what is missing.

    Without this the case above proves little -- a resolver that accepted
    anything at all would pass it. It is also the message the owner reads when
    it genuinely cannot proceed, so the no-repository-path rule (#4182) is
    checked on the real output rather than on a unit-test string.
    """
    section("3. inverse: no SSH key anywhere -- the scripted run fails and names the flags")
    home = make_home(with_ssh_key=False)
    completed = subprocess.run(
        [sys.executable, "-m", "nyxgpt", "cloud", "infra", "plan", "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=command_env(home),
        timeout=60,
        check=False,
    )
    output = completed.stdout + completed.stderr
    print("\n".join("      | " + line for line in output.splitlines()))
    check("it failed rather than inventing a key", completed.returncode != 0, output)
    check("it names --ssh-key-name", "--ssh-key-name" in output, output)
    check("it names --ssh-public-key", "--ssh-public-key" in output, output)
    check("it says a terminal would have asked", "terminal" in output, output)
    check("it cites no repository path", "product_management/" not in output, output)
    shutil.rmtree(home, ignore_errors=True)


# --- 3. The fingerprints, recomputed outside the implementation ----------


def case_fingerprints_match_the_tools() -> None:
    """`ssh-keygen` and `openssl` must agree with `public_key_fingerprints`.

    These are the two formats EC2 reports -- base64 SHA-256 of the OpenSSH
    blob, and (for an *imported* RSA pair) the hex MD5 of the DER
    SubjectPublicKeyInfo. The second is re-encoded by hand in
    `cloud_identity`, because `cryptography` is not a declared dependency of
    this project, and only a tool outside that code can say the re-encoding is
    right.
    """
    section("4. fingerprints: recomputed by ssh-keygen and openssl, not by us")
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nyxgpt import cloud_identity

    work = Path(tempfile.mkdtemp(prefix="cloud-identity-fp-"))
    for key_type in ("rsa", "ed25519"):
        path = work / f"id_{key_type}"
        run(["ssh-keygen", "-t", key_type, "-N", "", "-C", "probe", "-f", str(path), "-q"])
        material = (work / f"id_{key_type}.pub").read_text(encoding="utf-8")
        ours = cloud_identity.public_key_fingerprints(material)

        listed = run(["ssh-keygen", "-lf", str(work / f"id_{key_type}.pub")]).stdout
        sha256 = next(
            (part[len("SHA256:") :] for part in listed.split() if part.startswith("SHA256:")),
            "",
        )
        check(
            f"{key_type}: ssh-keygen's SHA256 fingerprint is one of ours",
            bool(sha256) and sha256 in ours,
            f"ssh-keygen={sha256!r} ours={sorted(ours)!r}",
        )

        if key_type == "rsa":
            pkcs8 = run(["ssh-keygen", "-f", str(work / f"id_{key_type}.pub"), "-e", "-m", "PKCS8"])
            der = subprocess.run(
                ["openssl", "pkey", "-pubin", "-outform", "DER"],
                input=pkcs8.stdout.encode(),
                capture_output=True,
                check=False,
            ).stdout
            md5 = subprocess.run(
                ["openssl", "md5", "-c"], input=der, capture_output=True, check=False
            ).stdout.decode()
            expected = md5.split("=")[-1].strip()
            check(
                "rsa: openssl's MD5 of the DER SPKI is one of ours (EC2's imported-pair form)",
                bool(expected) and expected in ours,
                f"openssl={expected!r} ours={sorted(ours)!r}",
            )
            # The inverse: a one-bit change must NOT match, so the comparison
            # is not matching everything.
            blob = bytearray(cloud_identity.public_key_blob(material))
            blob[-1] ^= 0x01
            tampered = base64.b64encode(bytes(blob)).decode("ascii")
            try:
                mismatched = cloud_identity.public_key_fingerprints(f"ssh-rsa {tampered} probe")
            except Exception:  # a corrupted blob may not parse at all, which is also a non-match
                mismatched = set()
            check(
                "rsa: a one-bit-different key does not match",
                expected not in mismatched,
                f"{expected!r} matched a tampered key",
            )
    shutil.rmtree(work, ignore_errors=True)


# --- 4. A matching EC2 key pair is offered by name -----------------------


FAKE_EC2_PROBE = '''
"""Drive the real resolver with a faked EC2 transport and a real fingerprint.

CI has no AWS account (docs/live-verification-ci.md), so `describe_key_pairs`
is the one call that cannot be executed here. Only its transport is faked: the
fingerprint it returns was computed by `openssl` from this machine's own key,
so the match the resolver makes is a real one -- it is the same comparison a
live account would produce, with the network removed.
"""
import sys

from nyxgpt import cloud_identity

fingerprint = sys.argv[1]


class _FakeEC2:
    def describe_key_pairs(self):
        return {"KeyPairs": [{"KeyName": "nyxgpt-smoke-key", "KeyFingerprint": fingerprint}]}


cloud_identity._client = lambda service, profile, region: (
    _FakeEC2() if service == "ec2" else None
)
cloud_identity.account_id = lambda profile, region="": "066835328281"

choice = cloud_identity.resolve_ssh(
    __import__("argparse").Namespace(
        profile="nyxgpt", region="us-east-1", ssh_key_name=None, ssh_public_key=None,
        identity_file=None, yes=True,
    ),
    interactive=False,
)
print("KEY_NAME=" + choice.key_name)
print("SOURCE=" + choice.source)
print("IDENTITY=" + choice.identity_file)
print("LABEL=" + choice.label)
'''


def case_matching_key_pair_is_offered() -> None:
    """The owner's case: a pair already in the account, found by fingerprint."""
    section("5. a key pair already in the account is offered by name, with the file it matches")
    home = make_home(with_ssh_key=True)
    run(
        [
            "ssh-keygen",
            "-t",
            "rsa",
            "-b",
            "2048",
            "-N",
            "",
            "-C",
            "owner@workstation",
            "-f",
            str(home / ".ssh" / "id_rsa"),
            "-q",
        ]
    )
    pkcs8 = run(["ssh-keygen", "-f", str(home / ".ssh" / "id_rsa.pub"), "-e", "-m", "PKCS8"])
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=pkcs8.stdout.encode(),
        capture_output=True,
        check=False,
    ).stdout
    md5 = subprocess.run(
        ["openssl", "md5", "-c"], input=der, capture_output=True, check=False
    ).stdout.decode()
    fingerprint = md5.split("=")[-1].strip()

    probe = home / "probe.py"
    probe.write_text(FAKE_EC2_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(probe), fingerprint],
        capture_output=True,
        text=True,
        env=command_env(home),
        timeout=60,
        check=False,
    )
    output = completed.stdout + completed.stderr
    print("\n".join("      | " + line for line in output.splitlines()))
    check("the existing pair was chosen by name", "KEY_NAME=nyxgpt-smoke-key" in output, output)
    check(
        "it was chosen because its fingerprint matched a local key",
        f"SOURCE={'matched-key-pair'}" in output,
        output,
    )
    check(
        "the local file it matches is named alongside it",
        "id_rsa.pub" in output,
        output,
    )
    check(
        "the private half beside it became the identity file",
        "IDENTITY=" in output and str(home / ".ssh" / "id_rsa") in output,
        output,
    )

    # The inverse: a fingerprint that belongs to no local key must not be
    # offered as a match, or the lookup is matching anything at all.
    completed = subprocess.run(
        [sys.executable, str(probe), "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
        capture_output=True,
        text=True,
        env=command_env(home),
        timeout=60,
        check=False,
    )
    output = completed.stdout + completed.stderr
    check(
        "a pair matching nothing local is NOT offered as a match",
        "SOURCE=matched-key-pair" not in output,
        output,
    )
    shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    """Run every case and report."""
    print("cloud-identity-prompt-smoke: executed evidence for #4186")
    for missing in ("ssh-keygen", "openssl"):
        if shutil.which(missing) is None:
            print(f"FAIL: {missing} is required and is not on PATH")
            return 1
    case_interactive_defaults()
    case_non_interactive_shapes()
    case_no_usable_default()
    case_fingerprints_match_the_tools()
    case_matching_key_pair_is_offered()

    print(f"\n{checks} checks run.")
    if failures:
        print(f"{len(failures)} FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed. Nothing was created in AWS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

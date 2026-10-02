"""A wrapped screen-sharing path to the EC2 Mac target (#4121).

`nyxgpt cloud tunnel` forwards the stack's ports; nothing reached the
*machine's screen*. On the owner's 2026-09-30 acceptance round the only reason
to pay an EC2 Mac Dedicated Host's 24-hour minimum -- that the hardware is a
Mac -- was also the one thing no command could get at: the macOS bootstrap
failed in a platform-specific way (Homebrew python's TLS chain rejected by
Secure Transport), CI structurally cannot reproduce it (EC2 Mac hardware is on
the short D-006 exception list in `docs/live-verification-ci.md`), and the
plumbing to look at the screen was three raw commands the operator had to
invent -- `kickstart`, `passwd`, `ssh -L` -- one of which sets an account
password. That is exactly the raw-operations flow CLAUDE.md's Operational
Command Wrapping requirement forbids.

`nyxgpt cloud screen` is that plumbing, wrapped:

1. **Enable Screen Sharing on the Mac**, over the same SSH path every other
   remote step uses, with the VNC credential nyxGPT generated -- so no account
   password is ever set and nothing is typed interactively.
2. **Make it loopback-only before it listens.** See `render_enable_script`:
   the packet filter rule goes in *first*, and the agent is not activated if
   it could not be loaded.
3. **Forward 5900 over SSH** and record the tunnel, so `nyxgpt cloud status`
   can report whether the path is open.

**The constraint that is not traded away.**
`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`: "Nothing is ever
listening on a non-loopback address on the deployments." This module opens no
security-group port -- not even one scoped to the operator's /32, which is the
alternative that decision considered and deliberately rejected. The Mac's
group stays TCP 22 only, and the screen is reached the same way the app ports
are: through the SSH tunnel, to a loopback address.

Observable, not operable (Definition of Done): the *command* is the control
surface. `nyxgpt cloud status` and the admin Infrastructure page report
whether the screen path is open and name this command; neither opens it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from nyxgpt import cloud_deploy, cloud_mac
from nyxgpt.cloud import CloudCommandError

# Apple's Screen Sharing / VNC port. Fixed on both ends: the remote half is
# macOS's own `vnc-server` service port, and the local half defaults to the
# same number so a VNC client pointed at `localhost` needs no configuration.
# `--local-port` moves the local half for an operator whose own Mac is already
# sharing its screen on 5900.
SCREEN_PORT = 5900

# The screen tunnel's pid, the Mac it reaches and what was configured on it, so
# `--stop`, `--status` and `nyxgpt cloud status` can find a path opened by an
# earlier process. Deliberately a different file from `tunnel.json`: the two
# are opened, closed and reported independently, and a single record would
# make closing one look like closing both.
SCREEN_STATE_FILE = cloud_deploy.CLOUD_DIR / "screen.json"

# Where the detached background tunnel's ssh stderr goes, for the same reason
# `cloud_deploy.TUNNEL_LOG_FILE` exists: the child outlives the CLI process
# that started it, so its stderr cannot stay on a pipe nobody will read.
SCREEN_LOG_FILE = cloud_deploy.CLOUD_DIR / "screen.log"

# Apple Remote Desktop's agent, which is what actually serves VNC on macOS and
# what `kickstart` configures. Present on every macOS install -- enabling it
# installs nothing (the issue's explicit non-goal).
KICKSTART = (
    "/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart"
)

# The packet-filter anchor that makes the listener loopback-only. macOS's
# `com.apple.screensharing` launchd job binds 5900 on all interfaces and its
# plist is SIP-protected, so the bind address is not ours to change; pf --
# which macOS already ships and which this only *configures* -- is what turns
# that into a loopback-only listener on the host itself. Belt and braces with
# the security group, which stays TCP 22 only either way.
PF_ANCHOR_NAME = "nyxgpt-screen"
PF_ANCHOR_FILE = f"/etc/pf.anchors/{PF_ANCHOR_NAME}"

# Apple's legacy VNC password is DES-based and truncated to 8 bytes, so a
# longer secret would be silently cut and the operator would be handed a
# password that does not work. 8 characters out of this 58-character alphabet
# is ~47 bits, for a listener that is reachable only from the Mac's own
# loopback interface through an authenticated SSH tunnel.
VNC_PASSWORD_LENGTH = 8

# Look-alike characters removed (O/0, l/1/I) because this is a secret a human
# reads off a terminal and types into a VNC client.
_VNC_PASSWORD_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in "O0lI1"
)

# Belt-and-braces check on the rendered script: a password containing a
# newline or a quote would be a shell-injection vector into the script text
# this module delivers over stdin. Generated passwords cannot contain one --
# the alphabet above is alphanumeric -- so this only ever fires on a secrets
# file an operator hand-edited.
_VNC_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")

# What `pfctl -a <anchor> -s rules` must print back before Screen Sharing is
# allowed to start. Matched on the rule's shape rather than its port
# rendering: pf prints a port either numerically or by its /etc/services name
# (`= 5900` or `= vnc-server`) depending on the release, and asserting the
# wrong one would make this check pass vacuously.
PF_RULE_MARKER = "block drop in quick proto tcp"


def vnc_password_path() -> Path:
    """Where the generated VNC credential is stored.

    `~/.nyxGPT/secrets/`, the same place every other ops-managed secret on
    this machine lives (`ops._grafana_doctor_token_path` and friends), with
    the same 0700 directory and 0600 file. Never prompted for and never
    echoed unless the operator asks for it with `--show-password`.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "cloud-mac-vnc-password"


def read_vnc_password() -> str:
    """Return the stored VNC credential, or `""` when none has been generated."""
    path = vnc_password_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_vnc_password() -> tuple[str, bool]:
    """Return `(password, created)`, generating and storing one if needed.

    Generated, never prompted: an interactive prompt is a secret in the
    operator's shell history and in their scrollback, and a password they
    chose is one they will reuse. Stable across runs so a VNC client's saved
    connection keeps working -- `--rotate-password` is how it changes.
    """
    existing = read_vnc_password()
    if existing:
        return existing, False
    return rotate_vnc_password(), True


def rotate_vnc_password() -> str:
    """Generate, store and return a fresh VNC credential."""
    password = "".join(secrets.choice(_VNC_PASSWORD_ALPHABET) for _ in range(VNC_PASSWORD_LENGTH))
    path = vnc_password_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)
    return password


# --- The remote half: a loopback-only Screen Sharing listener ----------


def render_enable_script(login_user: str, password: str) -> str:
    """Render the script that enables Screen Sharing on the Mac, loopback-only.

    Three properties this ordering exists for, in the order they are enforced:

    1. **The firewall rule is loaded before anything listens.** macOS's
       Screen Sharing job binds 5900 on all interfaces, so activating the
       agent first and filtering second would leave a window -- however
       short -- in which the Mac has a non-loopback listener. That is the one
       thing `DECISION_PRIVATE_ACCESS_MECHANISM.md` forbids outright, so a pf
       failure aborts with nothing enabled rather than proceeding.
    2. **The rule is read back, not assumed.** `pfctl -f` exits 0 on a
       ruleset it merely warned about, so the script re-reads the anchor and
       refuses if its own block rule is not in it.
    3. **No account password is ever set.** `-setvnclegacy -vnclegacy yes
       -setvncpw` is what lets a third-party VNC client authenticate with the
       generated credential, which is what retires the `sudo passwd ec2-user`
       step from the hand-rolled flow this command replaces.

    `password` is interpolated into the script *text*, which travels on the
    SSH connection's stdin. It is therefore in no argv on the operator's
    machine, in no shell history, and in nothing sshd logs as a requested
    command. It does reach `kickstart`'s argv on the Mac itself, which has no
    stdin interface for it; that machine is single-tenant and the operator's
    own, and it is the narrowest exposure available.
    """
    if not _VNC_PASSWORD_RE.match(password):
        raise CloudCommandError(
            f"The VNC credential in {vnc_password_path()} is not usable: Apple's legacy VNC "
            f"password is alphanumeric and at most {VNC_PASSWORD_LENGTH} characters. Delete "
            "that file and re-run `nyxgpt cloud screen` to generate a valid one."
        )
    return f"""\
#!/bin/bash
# Rendered by nyxGPT (`nyxgpt cloud screen`, #4121). Delivered over the same
# wrapped SSH path every other remote step uses; never typed by an operator.
set -euo pipefail

KICKSTART={shlex.quote(KICKSTART)}
PF_ANCHOR_FILE={shlex.quote(PF_ANCHOR_FILE)}
PF_ANCHOR_NAME={shlex.quote(PF_ANCHOR_NAME)}
SCREEN_PORT={SCREEN_PORT}
TARGET_USER={shlex.quote(login_user)}
NYXGPT_VNC_PASSWORD={shlex.quote(password)}

if [ "$(id -u)" != "0" ]; then
    echo "error: this script must run as root (nyxGPT elevates with sudo -n)" >&2
    exit 1
fi
if [ ! -x "$KICKSTART" ]; then
    echo "error: $KICKSTART is not present, so this is not a macOS machine with Apple" >&2
    echo "       Remote Desktop's agent. Nothing was changed." >&2
    exit 1
fi

# --- 1. Loopback-only, BEFORE anything listens -------------------------
# macOS binds 5900 on all interfaces and its launchd job is SIP-protected, so
# the bind address is not ours to change. pf -- already on this machine -- is
# what makes the listener loopback-only on the host itself. `quick` so the
# first match wins; the pass rule is first so the SSH tunnel's own connection
# to 127.0.0.1 is never caught by the block below it.
cat > "$PF_ANCHOR_FILE" <<'NYXGPT_ANCHOR'
# Managed by nyxGPT -- `nyxgpt cloud screen` (#4121).
# Screen Sharing is reachable only from this machine's loopback interface,
# which means only through nyxGPT's authenticated SSH tunnel. Nothing is
# listening on a non-loopback address
# (product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md).
pass in quick on lo0 proto tcp from any to any port 5900
block drop in quick proto tcp from any to any port 5900
NYXGPT_ANCHOR
chmod 0644 "$PF_ANCHOR_FILE"

# Referenced from pf.conf once, and idempotently: an anchor nothing refers to
# is never evaluated, and appending the reference twice would load it twice.
# The filter section is last in pf.conf's grammar, so appending is correct.
if ! grep -q "$PF_ANCHOR_NAME" /etc/pf.conf; then
    cp /etc/pf.conf "/etc/pf.conf.nyxgpt.bak"
    printf '\\nanchor "%s"\\nload anchor "%s" from "%s"\\n' \\
        "$PF_ANCHOR_NAME" "$PF_ANCHOR_NAME" "$PF_ANCHOR_FILE" >> /etc/pf.conf
fi
pfctl -f /etc/pf.conf
# `pfctl -e` exits non-zero when pf is already on, so ask first.
if ! pfctl -s info 2>/dev/null | grep -q 'Status: Enabled'; then
    pfctl -e
fi

# Read the anchor back. `pfctl -f` exits 0 on a ruleset it only warned about,
# so "the command succeeded" is not evidence the rule is loaded.
echo "nyxgpt: packet-filter rules now in force for port $SCREEN_PORT:"
pfctl -a "$PF_ANCHOR_NAME" -s rules
if ! pfctl -a "$PF_ANCHOR_NAME" -s rules | grep -q {shlex.quote(PF_RULE_MARKER)}; then
    echo "error: the loopback-only rule for port $SCREEN_PORT did not load, so Screen" >&2
    echo "       Sharing was NOT enabled -- it would have been reachable from the" >&2
    echo "       network. Nothing is listening and nothing was changed on the agent." >&2
    exit 1
fi

# --- 2. Screen Sharing on, for the login user only ---------------------
"$KICKSTART" -activate -configure -access -on -users "$TARGET_USER" -privs -all -restart -agent

# --- 3. The VNC credential, so no ACCOUNT password is ever set ---------
# This is what retires the account-password step from the hand-rolled flow:
# a third-party VNC client authenticates against this instead of against the
# login account, so the Mac's own user keeps having no password. The word the
# unit test greps for is deliberately absent from this whole script.
"$KICKSTART" -configure -clientopts -setvnclegacy -vnclegacy yes \\
    -setvncpw -vncpw "$NYXGPT_VNC_PASSWORD" >/dev/null

echo "nyxgpt: Screen Sharing is enabled for $TARGET_USER on 127.0.0.1:$SCREEN_PORT only."
"""


def render_disable_script() -> str:
    """Render the script that turns Screen Sharing back off.

    The pf anchor is deliberately **left loaded**. It blocks a port that
    nothing is listening on, so it costs nothing, and removing it would mean
    a later `nyxgpt cloud screen` that failed between activating the agent
    and loading the rule had a window the rule would otherwise have closed.
    Defence in depth is the cheaper default here.
    """
    return f"""\
#!/bin/bash
# Rendered by nyxGPT (`nyxgpt cloud screen --disable`, #4121).
set -euo pipefail

KICKSTART={shlex.quote(KICKSTART)}

if [ "$(id -u)" != "0" ]; then
    echo "error: this script must run as root (nyxGPT elevates with sudo -n)" >&2
    exit 1
fi
if [ ! -x "$KICKSTART" ]; then
    echo "error: $KICKSTART is not present; nothing to disable." >&2
    exit 1
fi

"$KICKSTART" -deactivate -configure -access -off
echo "nyxgpt: Screen Sharing is off. The loopback-only packet-filter rule is left in place."
"""


def remote_command(login_user: str) -> str:
    """The shell command the rendered script is piped into on the Mac.

    Elevated the same way the macOS bootstrap is
    (`cloud_deploy.provision_remote_command`): `kickstart` and `pfctl` are
    root-only, and `sudo -n` means a Mac whose login user needs a password
    fails immediately with sudo's own message instead of hanging a wrapped
    command on a prompt nothing can answer.
    """
    return f"sudo -n NYXGPT_TARGET_USER={shlex.quote(login_user)} bash -s"


def _run_remote_script(
    target: cloud_deploy.DeployTarget, script: str, secret: str
) -> tuple[int, list[str]]:
    """Pipe `script` to the Mac over SSH, echoing its output with `secret` redacted.

    The redaction is not theatre: `kickstart` prints its own usage -- argv
    included -- when it dislikes an argument, and the generated VNC password
    is one of those arguments. Streaming that straight through would put the
    credential in the operator's scrollback by way of an error message, which
    is precisely what this command exists not to do.
    """
    argv = [*cloud_deploy.ssh_argv(target), remote_command(target.user)]
    output: list[str] = []
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None  # nosec B101 - pipes requested
        proc.stdin.write(script)
        proc.stdin.close()
        for line in proc.stdout:
            clean = line.replace(secret, "***") if secret else line
            print(clean, end="")
            output.append(clean.rstrip("\n"))
    return proc.returncode, output


def configure_remote_screen_sharing(
    target: cloud_deploy.DeployTarget, password: str
) -> dict[str, Any]:
    """Enable the loopback-only Screen Sharing listener on `target`."""
    script = render_enable_script(target.user, password)
    returncode, output = _run_remote_script(target, script, password)
    if returncode != 0:
        raise CloudCommandError(_remote_failure_detail(returncode, output, target))
    return {"configured": True, "login_user": target.user, "port": SCREEN_PORT}


def disable_remote_screen_sharing(target: cloud_deploy.DeployTarget) -> dict[str, Any]:
    """Turn the Screen Sharing listener on `target` back off."""
    returncode, output = _run_remote_script(target, render_disable_script(), "")
    if returncode != 0:
        raise CloudCommandError(_remote_failure_detail(returncode, output, target))
    return {"configured": False}


def _remote_failure_detail(
    returncode: int, output: list[str], target: cloud_deploy.DeployTarget
) -> str:
    """Say why the remote half failed, in terms the operator can act on."""
    tail = "\n".join(f"  {line}" for line in output[-15:] if line.strip())
    quoted = f"\n{tail}" if tail else ""
    if returncode == 255:
        return (
            f"Could not reach {target.user}@{target.host} over SSH. If your public IP has "
            f"changed, run `{cloud_deploy.LIFECYCLE_COMMANDS['allow_ip']}`.{quoted}"
        )
    return (
        f"Configuring Screen Sharing on {target.host} failed (exit {returncode}). Nothing "
        "is listening on that Mac unless its own output above says otherwise -- the script "
        f"loads the loopback-only rule before it enables the agent.{quoted}"
    )


# --- The local half: the SSH tunnel ------------------------------------


def screen_argv(target: cloud_deploy.DeployTarget, local_port: int = SCREEN_PORT) -> list[str]:
    """Build the `ssh -N -L <local>:127.0.0.1:5900` argv for the screen path."""
    options = ["-N", "-L", f"{local_port}:127.0.0.1:{SCREEN_PORT}"]
    return cloud_deploy.ssh_argv(target, options=options)


def screen_invocation(target: cloud_deploy.DeployTarget, local_port: int = SCREEN_PORT) -> str:
    """The raw `ssh` the wrapped command runs, for diagnostics only.

    Never printed as an instruction (CLAUDE.md's wrapper requirement);
    `nyxgpt cloud screen` is what an operator runs.
    """
    return " ".join(shlex.quote(part) for part in screen_argv(target, local_port))


def screen_state() -> dict[str, Any]:
    """Return the recorded screen path (or `{}`)."""
    return cloud_deploy._read_json(SCREEN_STATE_FILE)


def screen_status() -> dict[str, Any]:
    """Report whether the screen path is open, and what nyxGPT configured.

    Cheap by construction -- recorded state plus one `kill(pid, 0)`, no
    network -- because `nyxgpt cloud status` and the dashboard poll it.

    A dead pid rewrites the record with `pid: 0` rather than deleting it, which
    is the one way this differs from `cloud_deploy.tunnel_status`. "Screen
    Sharing is enabled on that Mac" outlives any one tunnel, and it is the
    half an operator needs after a reboot took the tunnel with it: deleting
    the record would make an enabled listener invisible.
    """
    recorded = screen_state()
    pid = int(recorded.get("pid") or 0)
    running = cloud_deploy._process_alive(pid)
    if recorded and pid and not running:
        cloud_deploy._write_json(SCREEN_STATE_FILE, {**recorded, "pid": 0})
    local_port = int(recorded.get("local_port") or SCREEN_PORT)
    return {
        "running": running,
        "pid": pid if running else 0,
        "host": str(recorded.get("host") or ""),
        "local_port": local_port,
        # The address to point a VNC client at. Always a `localhost` one: there
        # is no instance-facing URL for the screen any more than there is one
        # for the app ports.
        "url": f"vnc://localhost:{local_port}" if running else "",
        # What nyxGPT last enabled on that Mac, which outlives the tunnel.
        "configured": bool(recorded.get("configured")),
        "configured_at": str(recorded.get("configured_at") or ""),
        "password_file": str(vnc_password_path()),
        "command": cloud_deploy.LIFECYCLE_COMMANDS["screen"],
        "stop_command": cloud_deploy.LIFECYCLE_COMMANDS["screen_stop"],
    }


def start_screen_tunnel(
    target: cloud_deploy.DeployTarget, local_port: int = SCREEN_PORT, *, background: bool = True
) -> dict[str, Any]:
    """Forward `local_port` to the Mac's loopback 5900.

    Detached into its own process group in background mode, exactly like the
    app-port tunnel, so a Ctrl-C aimed at the CLI does not take the screen
    path with it and a later `--stop` can find it.
    """
    existing = screen_status()
    if existing["running"]:
        return {"action": "screen", "already_running": True, **existing}

    argv = screen_argv(target, local_port)
    recorded = screen_state()
    if not background:
        subprocess.run(argv)
        return {"action": "screen", "already_running": False, "running": False, "pid": 0}

    SCREEN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCREEN_LOG_FILE, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
            text=True,
        )
    # ssh exits within a moment when the local bind or the auth fails; a short
    # settle avoids reporting an open path for a process that is already gone.
    time.sleep(1.0)
    if process.poll() is not None:
        try:
            detail = SCREEN_LOG_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            detail = ""
        raise CloudCommandError(
            "Could not open the screen tunnel"
            + (f": {detail}" if detail else ".")
            + f"\nLocal port {local_port} may already be in use -- a Mac that is sharing its "
            "own screen holds 5900. `--local-port <port>` forwards to a different one."
        )
    cloud_deploy._write_json(
        SCREEN_STATE_FILE,
        {
            **recorded,
            "pid": process.pid,
            "host": target.host,
            "user": target.user,
            "local_port": local_port,
            "remote_port": SCREEN_PORT,
        },
    )
    return {"action": "screen", "already_running": False, **screen_status()}


def stop_screen_tunnel() -> dict[str, Any]:
    """Close the screen tunnel, if one is open.

    Leaves the `configured` half of the record alone: closing the tunnel does
    not turn Screen Sharing off on the Mac (`--disable` does), and reporting
    otherwise would hide an enabled listener.
    """
    status = screen_status()
    if not status["running"]:
        return {"action": "screen-stop", "stopped": False, "reason": "no screen tunnel is open"}
    pid = int(status["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:  # pragma: no cover - racing an external kill
        raise CloudCommandError(f"Could not close the screen tunnel (pid {pid}): {exc}") from exc
    cloud_deploy._write_json(SCREEN_STATE_FILE, {**screen_state(), "pid": 0})
    return {"action": "screen-stop", "stopped": True, "pid": pid}


def record_configured(target: cloud_deploy.DeployTarget) -> None:
    """Remember that nyxGPT enabled Screen Sharing on `target`."""
    cloud_deploy._write_json(
        SCREEN_STATE_FILE,
        {
            **screen_state(),
            "configured": True,
            "configured_at": cloud_mac.scheduler_timestamp(cloud_mac.utc_now()),
            "host": target.host,
            "user": target.user,
        },
    )


def clear_screen_record() -> None:
    """Forget the screen path entirely. Called when the Mac itself is gone."""
    SCREEN_STATE_FILE.unlink(missing_ok=True)


def record_disabled() -> None:
    """Remember that Screen Sharing is no longer enabled on the recorded Mac."""
    cloud_deploy._write_json(
        SCREEN_STATE_FILE, {**screen_state(), "configured": False, "configured_at": ""}
    )


# --- Scoping: which machines this command will touch -------------------


def resolve_screen_target(args: argparse.Namespace) -> cloud_deploy.DeployTarget:
    """Resolve the Mac to open a screen path to, or refuse and say why.

    Two refusals, both required by #4121's acceptance criteria and both
    scoped the way `nyxgpt cloud allow-ip` already scopes itself -- to the
    machines nyxGPT configured (docs/cloud.md, "EC2 Mac targets"):

    * **A non-macOS target.** Screen Sharing is a macOS capability; there is
      nothing to enable on Amazon Linux, and the recorded deployment says
      which one this is.
    * **A Mac nyxGPT did not configure** (one supplied with `--host`). That
      machine's security group is not nyxGPT's, so nyxGPT cannot know whether
      5900 is exposed on it -- and the one constraint this command may not
      trade away is that nothing listens on a non-loopback address. Enabling
      a listener behind a firewall nobody checked is how that gets traded
      away by accident.
    """
    record = cloud_deploy.load_deploy_state()
    commands = cloud_deploy.LIFECYCLE_COMMANDS
    if not record.get("host"):
        raise CloudCommandError(
            "No deploy is recorded on this machine, so there is no Mac to open a screen "
            f"path to. `{commands['status']}` reports what this machine can see; "
            f"`{commands['deploy']} --os macos` provisions an EC2 Mac."
        )

    os_family = str(record.get("os_family") or "")
    if os_family != cloud_deploy.OS_FAMILY_MACOS:
        described = os_family or "not recorded (the deploy predates `--os`)"
        raise CloudCommandError(
            "Screen sharing is a macOS capability and the recorded deployment's target OS "
            f"is {described}. There is no screen to share on a Linux instance -- "
            f"`{commands['ops_status']}` and `{commands['doctor']}` are how that box is "
            "inspected, and `nyxgpt cloud tunnel` reaches its UIs."
        )

    # Deliberately *not* `cloud_deploy.resolve_access_target`, which takes the
    # address from the Linux substrate's `state.json` `public_ip`. A macOS
    # deploy never applies that substrate, so that key is not written for a
    # Mac at all -- the Mac's address is `mac_public_ip`, recorded by
    # `cloud_mac.record_mac_host` at allocation. Reading it from there is also
    # what makes the check below exact rather than approximate: the host this
    # command acts on is, by construction, the one nyxGPT allocated.
    mac = cloud_mac.load_mac_record()
    managed_ip = str(mac.get("mac_public_ip") or "")
    requested = str(getattr(args, "host", None) or record.get("host") or managed_ip)
    if not managed_ip or managed_ip != requested:
        raise CloudCommandError(
            f"nyxGPT did not configure the Mac at {requested or 'the requested host'}, so it "
            "will not enable a screen-sharing listener on it. "
            + (
                f"The EC2 Mac nyxGPT manages is at {managed_ip}."
                if managed_ip
                else "No EC2 Mac Dedicated Host is recorded on this machine."
            )
            + "\nThe reason is the constraint this command exists to keep: Screen Sharing must "
            "be reachable only from the Mac's own loopback interface, and nyxGPT cannot know "
            "what a security group it does not manage exposes. The same scoping applies to "
            f"`{commands['allow_ip']}` on a `--host` Mac (see docs/cloud.md, 'EC2 Mac "
            f"targets').\nDeploy a Mac nyxGPT manages with `{commands['deploy']} --os macos`."
        )
    # Flags win, then what the deploy recorded -- the same precedence
    # `cloud_deploy.resolve_access_target` applies, so a Mac deployed with a
    # non-default key does not need `--identity-file` re-typed here either.
    identity = str(getattr(args, "identity_file", None) or record.get("identity_file") or "")
    return cloud_deploy.DeployTarget(
        host=managed_ip,
        user=str(
            getattr(args, "ssh_user", None)
            or record.get("ssh_user")
            or cloud_deploy.DEFAULT_SSH_USER
        ),
        identity_file=str(Path(identity).expanduser()) if identity else "",
        region=str(mac.get("mac_region") or record.get("region") or ""),
        instance_id=str(mac.get("mac_instance_id") or ""),
        security_group_id=str(mac.get("mac_security_group_id") or ""),
    )


# --- CLI ---------------------------------------------------------------


def _print_open_summary(status: dict[str, Any], *, show_password: bool) -> None:
    """Tell the operator how to connect, without putting the secret on screen."""
    print(f"\nScreen path open to {status['host']} (pid {status['pid']}).")
    cloud_deploy._print_row("Address", status["url"])
    if show_password:
        cloud_deploy._print_row("Password", read_vnc_password())
    else:
        cloud_deploy._print_row(
            "Password",
            f"not shown -- it is in {status['password_file']} "
            f"(`{status['command']} --show-password` prints it)",
        )
    print(
        "\nApple's VNC authentication uses the password alone, so there is no account "
        "password on that Mac to set or know."
    )
    print(f"Close the path again with `{status['stop_command']}`.")


def _print_status_summary(status: dict[str, Any], *, show_password: bool = False) -> None:
    """Print `nyxgpt cloud screen --status` for a human."""
    print("Screen path to the EC2 Mac")
    cloud_deploy._print_row(
        "Tunnel", f"open (pid {status['pid']})" if status["running"] else "closed"
    )
    cloud_deploy._print_row("Address", status["url"] or f"closed -- `{status['command']}` opens it")
    cloud_deploy._print_row(
        "On the Mac",
        (
            f"Screen Sharing enabled by nyxGPT at "
            f"{status['configured_at'] or 'an unrecorded time'}, reachable from 127.0.0.1 only"
            if status["configured"]
            else "Screen Sharing is not enabled by nyxGPT on this machine's record"
        ),
    )
    if show_password:
        cloud_deploy._print_row("Credential", read_vnc_password() or "(none generated yet)")
    else:
        cloud_deploy._print_row("Credential", status["password_file"])


def screen_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud screen` entry point."""
    try:
        show_password = bool(getattr(args, "show_password", False))
        if getattr(args, "status", False):
            status = screen_status()
            if getattr(args, "json", False):
                # The credential is never in the machine payload, for the same
                # reason `nyxgpt ops credentials` keeps secrets off the HTTP API
                # (#3458/#3466): a JSON blob gets piped, logged and pasted.
                print(json.dumps(status, indent=2))
            else:
                _print_status_summary(status, show_password=show_password)
            return 0

        if getattr(args, "stop", False):
            result = stop_screen_tunnel()
            print("Screen path closed." if result["stopped"] else "No screen path is open.")
            print(
                "Screen Sharing is still enabled on the Mac, and nothing can reach it: its "
                "packet filter drops every non-loopback connection to 5900 and no "
                "security-group port is open. "
                f"`{cloud_deploy.LIFECYCLE_COMMANDS['screen']} --disable` turns it off."
            )
            return 0

        if getattr(args, "disable", False):
            target = resolve_screen_target(args)
            stop_screen_tunnel()
            disable_remote_screen_sharing(target)
            record_disabled()
            print(f"Screen Sharing is off on {target.host} and the screen path is closed.")
            return 0

        target = resolve_screen_target(args)
        if getattr(args, "rotate_password", False):
            rotate_vnc_password()
        password, created = ensure_vnc_password()
        if created:
            print(f"Generated a VNC credential and stored it in {vnc_password_path()}.")
        configure_remote_screen_sharing(target, password)
        record_configured(target)
        local_port = int(getattr(args, "local_port", None) or SCREEN_PORT)
        result = start_screen_tunnel(target, local_port)
        if result.get("already_running"):
            print("\nA screen path is already open.")
        _print_open_summary(screen_status(), show_password=show_password)
        return 0
    except CloudCommandError as exc:
        print(f"nyxgpt cloud screen: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nnyxgpt cloud screen: interrupted.", file=sys.stderr)
        return cloud_deploy.EXIT_INTERRUPTED

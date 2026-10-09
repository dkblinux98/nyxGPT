"""The one resolver for *which AWS account* and *which SSH key* a cloud command uses (#4186).

Every `nyxgpt cloud` command needs two answers before it can do anything:
the AWS identity to authenticate as, and the SSH key that will be the only
way into the instance afterwards. Before this module there was no single
place that answered either one:

* **The account was chosen invisibly.** `cloud_infra.resolve_settings`,
  `cloud._resolve_profile` and `cloud_mac._record_profile` each walked their
  own version of the same chain and none of them ever *said* what they had
  picked. A command that ran against the wrong account reported the symptom
  of that choice (`InvalidGroup.NotFound`) and never the choice itself.
* **The SSH key was a wall, not a question.** With no `--ssh-key-name` /
  `--ssh-public-key` the deploy stopped dead -- while, in the owner's
  2026-10-09 acceptance round, the usable answer was already on the machine:
  an EC2 key pair in the account whose fingerprint matched a local
  `~/.ssh/*.pub`. Finding it took a hand-run fingerprint comparison.

So this module owns the resolution order -- **flag -> `infra.json` ->
`config.ini [cloud]` -> environment** -- for both answers, asks for anything
it still does not know (offering the resolved value as a default the operator
takes with Enter), and prints what it settled on. It is deliberately the only
copy: #4181 extends this resolver rather than adding a second one, and
`cloud._resolve_profile` / `cloud_mac._record_profile` now delegate here.

Three rules the surrounding system depends on:

* **Never hang.** Prompting requires a real TTY on both stdin and stdout and
  the absence of `--yes`/`NYXGPT_CLOUD_NONINTERACTIVE`. `nyxgpt cloud ops`
  runs over SSH and CI runs headless; both take the defaults and get one line
  naming them. With no usable default, the error names every missing input
  and the flag that supplies it -- and names no repository path, because the
  operator reading it has no checkout (#4182).
* **No network call the answer does not need.** The account id costs one STS
  call, so a recorded one is reused; the EC2 key-pair scan costs one
  `describe_key_pairs` and is made only when nothing is recorded -- which is
  exactly the case that used to fail. Both are best-effort: an expired
  credential degrades the prompt, it never replaces the command's own error.
* **The choice stays visible.** `resolve_account` records the account id it
  resolved, so `nyxgpt cloud status` and the dashboard can report the account
  a deployment was made in from a local read, with no credentials at all.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt.cloud import CloudCommandError
from nyxgpt.optional_imports import try_import, try_import_attr

# Set to any non-empty value to force the non-interactive path regardless of
# TTYs -- the escape hatch for a wrapper script that runs `nyxgpt cloud` with a
# terminal attached but no human in front of it.
NONINTERACTIVE_ENV = "NYXGPT_CLOUD_NONINTERACTIVE"

# Where local public keys are looked for, and the order a default is picked in.
# Modern key types first, then RSA -- which is both the oldest default and the
# one the owner's machine actually had.
SSH_DIR = Path.home() / ".ssh"
PREFERRED_KEY_FILES = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")

# Source tags on a resolved answer. Reported rather than inferred so a caller
# (and a test) can tell "the operator typed this" from "we guessed well".
SOURCE_FLAG = "flag"
SOURCE_RECORD = "record"
SOURCE_CONFIG = "config"
SOURCE_ENVIRONMENT = "environment"
SOURCE_PROMPT = "prompt"
SOURCE_DEFAULT = "default"
SOURCE_MATCHED_KEY_PAIR = "matched-key-pair"
SOURCE_LOCAL_PUBLIC_KEY = "local-public-key"

# STS/EC2 calls made purely to *describe* a default must not stall a command
# for a minute when there is no route to AWS. One attempt, short timeouts: the
# worst case is a prompt that cannot name the account id, which it says.
_LOOKUP_CONNECT_TIMEOUT = 3
_LOOKUP_READ_TIMEOUT = 5

# profile -> account id, for the life of the process. `resolve_account` is
# called by more than one layer of a single command (the settings resolver, then
# the announcement), and one STS round trip per command is the budget.
_ACCOUNT_ID_CACHE: dict[str, str] = {}


# --- Resolved answers ----------------------------------------------------


@dataclass(frozen=True)
class AccountChoice:
    """The AWS identity one cloud command will authenticate as."""

    profile: str = ""
    region: str = ""
    account_id: str = ""
    source: str = SOURCE_DEFAULT

    @property
    def profile_label(self) -> str:
        """The profile, or the honest description of having resolved none."""
        return self.profile or "(boto3's default credential chain)"

    @property
    def label(self) -> str:
        """`nyxgpt (066835328281)` -- the profile *and* the account it resolves to.

        Both halves, always: the profile name alone is what made a
        wrong-account run invisible, since two profiles with sensible names
        can point anywhere. When the id could not be looked up the label says
        so rather than implying the profile is the whole answer.
        """
        if self.account_id:
            return f"{self.profile_label} ({self.account_id})"
        return f"{self.profile_label} (account id not resolved)"


@dataclass(frozen=True)
class SshChoice:
    """The SSH identity one cloud command will give the instance, and log in with.

    `key_name` names an EC2 key pair that already exists; `public_key`
    carries OpenSSH material to register as a new one. Exactly one of the two
    is set -- that is the same either/or `--ssh-key-name` /
    `--ssh-public-key` express. `identity_file` is the private half to
    authenticate with, empty meaning "ssh's own `~/.ssh` defaults and agent".
    """

    key_name: str = ""
    public_key: str = ""
    public_key_path: str = ""
    identity_file: str = ""
    source: str = SOURCE_DEFAULT

    @property
    def label(self) -> str:
        """One line describing this choice, including where it came from."""
        if self.key_name and self.source == SOURCE_MATCHED_KEY_PAIR:
            return (
                f"EC2 key pair {self.key_name!r} (already in this account; "
                f"fingerprint matches {_tilde(self.public_key_path)})"
            )
        if self.key_name:
            return f"EC2 key pair {self.key_name!r}"
        if self.public_key_path:
            return f"register {_tilde(self.public_key_path)} as a new EC2 key pair"
        return "register the given public key as a new EC2 key pair"


def _tilde(path: str) -> str:
    """Render an absolute path under the home directory as `~/...`.

    Prompts and the one-line announcement are read by a human who thinks of
    the file as `~/.ssh/id_rsa.pub`, and the expanded form is both longer and
    identifies the operator's account name in anything they paste into an
    issue.
    """
    if not path:
        return ""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path


# --- Interaction ---------------------------------------------------------


def prompting_enabled(args: argparse.Namespace | None = None) -> bool:
    """Whether this run may ask the operator a question.

    Three things each independently forbid it, and every one of them is a
    situation where a prompt would hang a command that must finish on its
    own: `--yes` (the operator already said "use the defaults"),
    `NONINTERACTIVE_ENV`, and the absence of a terminal on stdin or stdout --
    which is CI, a `nyxgpt cloud ops` invocation over SSH, and the admin
    dashboard's API worker.
    """
    if args is not None and getattr(args, "yes", False):
        return False
    if os.environ.get(NONINTERACTIVE_ENV, "").strip():
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        # A closed or replaced stream is not a terminal; it is also not a
        # reason to crash before the command has started.
        return False


def ask(prompt: str, default: str = "") -> str:
    """Ask one question, returning `default` for an empty answer or a closed stdin.

    EOF is treated as "take the default" rather than as a cancellation: a
    caller only reaches here when `prompting_enabled` said there was a
    terminal, so an EOF at this point is a stream that went away mid-command,
    and the defaults are precisely what the non-interactive path would have
    used.
    """
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_choice(prompt: str, options: list[str]) -> int:
    """Present a numbered list and return the chosen index, defaulting to the first.

    Re-asks on an unparseable answer rather than falling back silently: a
    typo'd SSH key choice would otherwise hand the instance a key the operator
    did not pick, which they discover when they cannot log in. Three tries,
    then the default -- a loop with no bound is its own way to hang.
    """
    for number, option in enumerate(options, start=1):
        marker = "  (default)" if number == 1 else ""
        print(f"  {number}) {option}{marker}")
    for _ in range(3):
        answer = ask(prompt, "1")
        try:
            index = int(answer)
        except ValueError:
            print(f"  {answer!r} is not one of 1-{len(options)}.")
            continue
        if 1 <= index <= len(options):
            return index - 1
        print(f"  {index} is not one of 1-{len(options)}.")
    print("  Taking the default.")
    return 0


def missing_inputs_error(missing: list[tuple[str, str]]) -> CloudCommandError:
    """Build the non-interactive failure that names every missing input at once.

    One error listing all of them, not the first one found: a scripted run
    that is corrected one flag per failed invocation costs an AWS round trip
    (and, on a deploy, a provision) per round. Each line names the thing
    needed and the flag that supplies it, and nothing here cites a repository
    path -- the operator has no checkout to read it in (#4182).
    """
    lines = [f"  - {what}: pass {flag}" for what, flag in missing]
    return CloudCommandError(
        "This run cannot ask (no terminal, or --yes was given) and these inputs "
        "have no usable default:\n"
        + "\n".join(lines)
        + "\nRun the command from a terminal to be prompted for them instead, or set "
        "them once with `nyxgpt cloud credentials-setup`."
    )


# --- Recorded and configured sources -------------------------------------


def recorded_settings() -> dict[str, Any]:
    """What the last `cloud infra apply` wrote to `infra.json`, or `{}`.

    Imported late: `cloud_infra` imports this module for the resolver, so a
    module-scope import back would close the cycle.
    """
    try:
        from nyxgpt import cloud_infra

        return cloud_infra.load_settings()
    except Exception:
        # A corrupt or unreadable record is a missing default, never a reason
        # the command cannot run -- every value it would supply has a flag.
        return {}


def configured_reference() -> dict[str, str]:
    """config.ini's `[cloud]` profile/region reference, or empty strings on any failure."""
    try:
        from nyxgpt import aws_credentials_setup
        from nyxgpt import config as config_mod

        return aws_credentials_setup.cloud_reference_status(config_mod.load_config())
    except Exception:
        return {"profile": "", "region": ""}


def _session(profile: str) -> Any:
    """A boto3 session for `profile`, or None when boto3/the profile is unavailable."""
    boto3 = try_import("boto3")
    if boto3 is None:
        return None
    try:
        return boto3.Session(profile_name=profile) if profile else boto3.Session()
    except Exception:
        return None


def _lookup_config() -> Any:
    """A botocore `Config` with short timeouts for the describe-only lookups."""
    config_cls = try_import_attr("botocore.config", "Config")
    if config_cls is None:
        return None
    try:
        return config_cls(
            connect_timeout=_LOOKUP_CONNECT_TIMEOUT,
            read_timeout=_LOOKUP_READ_TIMEOUT,
            retries={"max_attempts": 1},
        )
    except Exception:
        return None


def _client(service: str, profile: str, region: str) -> Any:
    """A short-timeout client for one of this module's describe-only lookups, or None."""
    session = _session(profile)
    if session is None:
        return None
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    config = _lookup_config()
    if config is not None:
        kwargs["config"] = config
    try:
        return session.client(service, **kwargs)
    except Exception:
        return None


def account_id(profile: str, region: str = "") -> str:
    """Return the AWS account id `profile` resolves to, or `""` if it cannot be read.

    Best-effort and never fatal. The id is the half of the account answer
    that a profile name cannot give -- two differently-named profiles can
    point at the same account and one name can be re-pointed at another --
    so it is worth one STS call per command, and worth saying "not resolved"
    when the call fails rather than printing a profile name as if it were the
    whole truth.
    """
    cache_key = f"{profile}\x00{region}"
    cached = _ACCOUNT_ID_CACHE.get(cache_key)
    if cached is not None:
        return cached
    client = _client("sts", profile, region)
    resolved = ""
    if client is not None:
        try:
            resolved = str(client.get_caller_identity().get("Account", "") or "")
        except Exception:
            resolved = ""
    _ACCOUNT_ID_CACHE[cache_key] = resolved
    return resolved


# --- SSH key fingerprints ------------------------------------------------


def public_key_blob(material: str) -> bytes:
    """Decode the base64 body of an OpenSSH public key line.

    Raises `CloudCommandError` for anything that is not one -- the same
    refusal `cloud_infra._read_ssh_public_key` makes, kept here because the
    fingerprint helpers are also reached from the key-pair scan, which must
    not crash on a stray file in `~/.ssh`.
    """
    parts = material.strip().split()
    body = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
    try:
        return base64.b64decode(body, validate=True)
    except Exception as exc:
        raise CloudCommandError(f"{material.strip()[:32]!r} is not an OpenSSH public key") from exc


def _blob_fields(blob: bytes) -> list[bytes]:
    """Split an OpenSSH key blob into its length-prefixed fields."""
    fields: list[bytes] = []
    offset = 0
    while offset + 4 <= len(blob):
        (length,) = struct.unpack(">I", blob[offset : offset + 4])
        offset += 4
        if offset + length > len(blob):
            break
        fields.append(blob[offset : offset + length])
        offset += length
    return fields


def _der(tag: int, payload: bytes) -> bytes:
    """Encode one DER TLV."""
    length = len(payload)
    if length < 0x80:
        header = bytes([tag, length])
    else:
        encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
        header = bytes([tag, 0x80 | len(encoded)]) + encoded
    return header + payload


def _der_integer(raw: bytes) -> bytes:
    """Encode an unsigned big-endian integer as a DER INTEGER."""
    trimmed = raw.lstrip(b"\x00") or b"\x00"
    if trimmed[0] & 0x80:
        trimmed = b"\x00" + trimmed
    return _der(0x02, trimmed)


# 1.2.840.113549.1.1.1 (rsaEncryption), then the NULL parameters it takes.
_RSA_OID = bytes.fromhex("06092A864886F70D010101")
_DER_NULL = bytes.fromhex("0500")


def _rsa_spki_der(blob: bytes) -> bytes | None:
    """Re-encode an `ssh-rsa` blob as a DER SubjectPublicKeyInfo, or None if it isn't one.

    Hand-rolled rather than delegated to `cryptography`, which is not a
    declared dependency of this project: the structure is four nested TLVs
    around the two integers the blob already carries, and an optional import
    that is absent would silently drop RSA from the fingerprint match -- the
    one key type the owner's machine actually had.
    """
    fields = _blob_fields(blob)
    if len(fields) < 3 or fields[0] != b"ssh-rsa":
        return None
    exponent, modulus = fields[1], fields[2]
    algorithm = _der(0x30, _RSA_OID + _DER_NULL)
    public_key = _der(0x30, _der_integer(modulus) + _der_integer(exponent))
    return _der(0x30, algorithm + _der(0x03, b"\x00" + public_key))


def normalize_fingerprint(value: str) -> str:
    """Strip the decoration AWS and `ssh-keygen` disagree about.

    `ssh-keygen` prints `SHA256:<base64>` with the padding removed; EC2
    returns the same digest base64-encoded with padding. Case is preserved --
    base64 is case-significant, and lowercasing it to make the hex MD5 form
    comparable would make two different keys comparable too.
    """
    text = value.strip()
    if text.upper().startswith("SHA256:"):
        text = text[len("SHA256:") :]
    return text.rstrip("=")


def public_key_fingerprints(material: str) -> set[str]:
    """Every fingerprint EC2 might report for this public key.

    EC2 does not use one format. An *imported* RSA key pair is fingerprinted
    as the hex MD5 of its DER public key; an ED25519 pair (imported or
    created) as the base64 SHA-256 of the OpenSSH blob. Both are computed and
    the caller compares against whichever EC2 returned, so nothing here has to
    guess from `KeyType` -- which older key pairs do not carry.

    A key pair *created* by EC2 is fingerprinted from its private half (SHA-1
    of the PKCS#8 DER), which no public key can reproduce. Those simply do not
    match, which is why an unmatched pair is reported as "no local key
    matches" and never as "that is not your key".
    """
    blob = public_key_blob(material)
    fingerprints = {
        normalize_fingerprint(base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii"))
    }
    spki = _rsa_spki_der(blob)
    if spki is not None:
        # MD5 is EC2's chosen *format* for an imported RSA key pair's
        # fingerprint, not a security decision of this project's: the digest is
        # compared against a value AWS computed the same way, and nothing is
        # authenticated by it.
        digest = hashlib.md5(spki, usedforsecurity=False).hexdigest()
        fingerprints.add(":".join(digest[i : i + 2] for i in range(0, len(digest), 2)))
    return fingerprints


def local_public_keys(ssh_dir: Path | None = None) -> list[Path]:
    """Return the operator's local public keys, best candidate first.

    `PREFERRED_KEY_FILES` order first (modern types before RSA), then
    everything else alphabetically -- so the default offered at a prompt is
    stable across runs instead of depending on directory order.
    """
    directory = ssh_dir if ssh_dir is not None else SSH_DIR
    try:
        found = sorted(p for p in directory.glob("*.pub") if p.is_file())
    except OSError:
        return []
    preferred = [directory / name for name in PREFERRED_KEY_FILES]
    ordered = [path for path in preferred if path in found]
    ordered.extend(path for path in found if path not in ordered)
    return ordered


def private_key_for(public_key_path: Path) -> str:
    """The private half of `public_key_path`, or `""` when it is not beside it.

    Empty is a real answer -- "let ssh use its own `~/.ssh` defaults and
    agent" -- and is what a key held only in an agent, or on a machine that
    registered a public key it does not hold the private half of, correctly
    reports.
    """
    if public_key_path.suffix != ".pub":
        return ""
    private = public_key_path.with_suffix("")
    return str(private) if private.is_file() else ""


@dataclass(frozen=True)
class KeyPairMatch:
    """An EC2 key pair whose fingerprint matches a public key on this machine."""

    key_name: str
    public_key_path: Path
    identity_file: str


def ec2_key_pairs(profile: str, region: str) -> list[dict[str, str]]:
    """Return `[{"name", "fingerprint"}]` for the account's EC2 key pairs, or `[]`.

    Best-effort by construction: this only ever improves a default, so no
    credential, permission or network failure here may become the error the
    operator sees. One `describe_key_pairs` call, short-timeout.
    """
    client = _client("ec2", profile, region)
    if client is None:
        return []
    try:
        response = client.describe_key_pairs()
    except Exception:
        return []
    pairs: list[dict[str, str]] = []
    for pair in response.get("KeyPairs", []) or []:
        name = str(pair.get("KeyName", "") or "")
        if name:
            pairs.append({"name": name, "fingerprint": str(pair.get("KeyFingerprint", "") or "")})
    return pairs


def matching_key_pairs(
    profile: str, region: str, ssh_dir: Path | None = None
) -> list[KeyPairMatch]:
    """EC2 key pairs in `profile`/`region` whose fingerprint matches a local public key.

    This is the lookup the owner had to do by hand on 2026-10-09: the account
    held `nyxgpt-smoke-key`, whose fingerprint matched `~/.ssh/id_rsa.pub`,
    and nothing in nyxGPT would say so. Offering the pair *together with* the
    local file it matches is the whole point -- the name alone does not tell
    an operator they can log in with it.
    """
    pairs = ec2_key_pairs(profile, region)
    if not pairs:
        return []
    matches: list[KeyPairMatch] = []
    for path in local_public_keys(ssh_dir):
        try:
            fingerprints = public_key_fingerprints(path.read_text(encoding="utf-8"))
        except (OSError, CloudCommandError, UnicodeDecodeError):
            # `~/.ssh` holds plenty of files that are not public keys. Skipping
            # one is not an error the operator needs to hear about.
            continue
        for pair in pairs:
            if normalize_fingerprint(pair["fingerprint"]) in fingerprints:
                matches.append(
                    KeyPairMatch(
                        key_name=pair["name"],
                        public_key_path=path,
                        identity_file=private_key_for(path),
                    )
                )
    return matches


# --- Resolution ----------------------------------------------------------


def account_default(args: argparse.Namespace | None = None) -> AccountChoice:
    """Resolve the account/region by the documented order, making no AWS call.

    flag -> `infra.json` -> `config.ini [cloud]` -> environment. The account
    id is taken from what a previous run recorded, so this stays a local read;
    `resolve_account` is what will look one up when there is none.
    """
    recorded = recorded_settings()
    reference = configured_reference()

    flag_profile = str(getattr(args, "profile", None) or "") if args is not None else ""
    flag_region = str(getattr(args, "region", None) or "") if args is not None else ""

    profile = (
        flag_profile
        or str(recorded.get("aws_profile") or "")
        or str(reference.get("profile") or "")
        or os.environ.get("AWS_PROFILE", "")
    )
    region = (
        flag_region
        or str(recorded.get("aws_region") or "")
        or str(reference.get("region") or "")
        or os.environ.get("AWS_REGION", "")
        or os.environ.get("AWS_DEFAULT_REGION", "")
    )
    if flag_profile or flag_region:
        source = SOURCE_FLAG
    elif recorded.get("aws_profile") or recorded.get("aws_region"):
        source = SOURCE_RECORD
    elif reference.get("profile") or reference.get("region"):
        source = SOURCE_CONFIG
    elif profile or region:
        source = SOURCE_ENVIRONMENT
    else:
        source = SOURCE_DEFAULT
    # Only reused when the recorded profile is the one being resolved -- an
    # id recorded for a different profile describes a different account, which
    # is the exact confusion this field exists to prevent.
    recorded_id = (
        str(recorded.get("aws_account_id") or "")
        if profile == str(recorded.get("aws_profile") or "")
        else ""
    )
    return AccountChoice(profile=profile, region=region, account_id=recorded_id, source=source)


def resolve_account(
    args: argparse.Namespace | None = None,
    *,
    interactive: bool | None = None,
    lookup_account_id: bool = True,
) -> AccountChoice:
    """Resolve the AWS account, asking for it (with a default) when nothing named it.

    An explicit `--profile` is never questioned. Otherwise the resolved value
    is offered as a default the operator takes with Enter, and the prompt
    shows the profile *and* the account id it resolves to -- so the account a
    command is about to act in is never chosen without being seen.

    `lookup_account_id=False` suppresses the STS call for a caller that only
    wants the names (the non-interactive announcement on a path where the id
    is already recorded).
    """
    default = account_default(args)
    if interactive is None:
        interactive = prompting_enabled(args)

    account = default
    if lookup_account_id and not account.account_id:
        account = AccountChoice(
            profile=default.profile,
            region=default.region,
            account_id=account_id(default.profile, default.region),
            source=default.source,
        )

    if not interactive or default.source == SOURCE_FLAG:
        return account

    # The default shown in the brackets is the *label* -- profile plus the
    # account id it resolves to -- because the profile name alone is what made
    # a wrong-account run invisible (#4181): two sensibly-named profiles can
    # point at any two accounts. With no profile resolved there is no label to
    # show, so the prompt says in words what Enter will do instead of offering
    # `[(none)]` and leaving the operator to guess.
    label = account.label if account.profile else ""
    prompt = (
        "AWS profile"
        if account.profile
        else "AWS profile (Enter to use boto3's default credential chain)"
    )
    answer = ask(prompt, label)
    if answer in ("", label, account.profile):
        return account
    chosen = "" if answer == "-" else answer
    return AccountChoice(
        profile=chosen,
        region=account.region,
        account_id=account_id(chosen, account.region) if lookup_account_id else "",
        source=SOURCE_PROMPT,
    )


def _explicit_ssh_choice(args: argparse.Namespace | None) -> SshChoice | None:
    """The SSH choice the flags made, or None when they named neither key.

    `read_public_key` is `cloud_infra`'s reader, reached late: it owns the
    "that is a private key" refusal and this module must not grow a second
    copy of it.
    """
    key_name = str(getattr(args, "ssh_key_name", None) or "") if args is not None else ""
    public_key_arg = str(getattr(args, "ssh_public_key", None) or "") if args is not None else ""
    if key_name and public_key_arg:
        raise CloudCommandError(
            "Pass either --ssh-key-name (an EC2 key pair that already exists) or "
            "--ssh-public-key (a .pub file to register), not both."
        )
    if not key_name and not public_key_arg:
        return None

    from nyxgpt import cloud_infra

    identity = str(getattr(args, "identity_file", None) or "") if args is not None else ""
    if key_name:
        return SshChoice(key_name=key_name, identity_file=identity, source=SOURCE_FLAG)
    path = Path(public_key_arg).expanduser()
    return SshChoice(
        public_key=cloud_infra.read_ssh_public_key(public_key_arg),
        public_key_path=str(path) if path.is_file() else "",
        identity_file=identity or (private_key_for(path) if path.is_file() else ""),
        source=SOURCE_FLAG,
    )


def ssh_candidates(
    account: AccountChoice,
    *,
    scan_account: bool = True,
    ssh_dir: Path | None = None,
) -> list[SshChoice]:
    """Every SSH identity this machine could offer, best first.

    In the order #4186 specifies:

    1. the key the deployment already recorded in `infra.json`;
    2. an EC2 key pair in the chosen account and region whose fingerprint
       matches a local public key, offered by name *with* the file it matches;
    3. a local public key to register as a new pair.

    `scan_account=False` skips (2), which is the only step that touches AWS.
    The caller sets it when (1) already answered: a recorded key needs no
    account scan to be usable, and a `describe_key_pairs` on every deploy
    would be a round trip spent confirming what the record already said.
    """
    candidates: list[SshChoice] = []
    recorded = recorded_settings()
    recorded_name = str(recorded.get("ssh_key_name") or "")
    recorded_material = str(recorded.get("ssh_public_key") or "")
    recorded_identity = str(recorded.get("ssh_identity_file") or "")
    if recorded_name or recorded_material:
        candidates.append(
            SshChoice(
                key_name=recorded_name,
                public_key=recorded_material,
                public_key_path=str(recorded.get("ssh_public_key_path") or ""),
                identity_file=recorded_identity,
                source=SOURCE_RECORD,
            )
        )

    if scan_account:
        for match in matching_key_pairs(account.profile, account.region, ssh_dir):
            if any(c.key_name == match.key_name for c in candidates):
                continue
            candidates.append(
                SshChoice(
                    key_name=match.key_name,
                    public_key_path=str(match.public_key_path),
                    identity_file=match.identity_file,
                    source=SOURCE_MATCHED_KEY_PAIR,
                )
            )

    for path in local_public_keys(ssh_dir):
        if any(c.public_key_path == str(path) for c in candidates):
            continue
        try:
            material = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not material.startswith(("ssh-", "ecdsa-", "sk-")):
            continue
        candidates.append(
            SshChoice(
                public_key=material,
                public_key_path=str(path),
                identity_file=private_key_for(path),
                source=SOURCE_LOCAL_PUBLIC_KEY,
            )
        )
    return candidates


def resolve_ssh(
    args: argparse.Namespace | None = None,
    account: AccountChoice | None = None,
    *,
    interactive: bool | None = None,
    ssh_dir: Path | None = None,
) -> SshChoice:
    """Resolve the SSH identity, asking for it (with a default) when nothing named one.

    SSH is the only way into a nyxGPT instance, so a missing key used to stop
    the command outright. It is a *question* now: the candidates are listed,
    the first is the default, and the private half is offered alongside the
    pair it belongs to. Non-interactively the first candidate is taken and
    printed; with no candidate at all the error names both flags, and no
    repository path (#4182).
    """
    explicit = _explicit_ssh_choice(args)
    if explicit is not None:
        return explicit
    if account is None:
        account = account_default(args)
    if interactive is None:
        interactive = prompting_enabled(args)

    recorded_only = ssh_candidates(account, scan_account=False, ssh_dir=ssh_dir)
    answered_by_record = bool(recorded_only) and recorded_only[0].source == SOURCE_RECORD
    candidates = (
        recorded_only
        if answered_by_record
        else ssh_candidates(account, scan_account=True, ssh_dir=ssh_dir)
    )

    if not candidates:
        raise missing_inputs_error(
            [
                (
                    "an SSH key for the instance (SSH is the only way in; this account "
                    "has no key pair matching a local public key, and this machine has "
                    "no public key to register)",
                    "--ssh-key-name <existing-pair> or --ssh-public-key <file.pub>",
                )
            ]
        )

    if not interactive:
        return candidates[0]

    # Asked even when there is only one candidate. The key is the only way
    # into the instance and it is about to be registered against it, so
    # "there was nothing to choose between" is not a reason to install it
    # without the operator seeing it -- which is the invisibility this issue
    # is about. One Enter is the whole cost.
    print("\nSSH key for the instance (SSH is the only way in):")
    index = ask_choice("Choice", [c.label for c in candidates])
    chosen = candidates[index]

    identity_default = chosen.identity_file
    answer = ask("Private key to authenticate with", _tilde(identity_default) or "(ssh defaults)")
    identity = (
        identity_default
        if answer in ("", "(ssh defaults)", _tilde(identity_default))
        else str(Path(answer).expanduser())
    )
    return SshChoice(
        key_name=chosen.key_name,
        public_key=chosen.public_key,
        public_key_path=chosen.public_key_path,
        identity_file=identity,
        source=SOURCE_PROMPT if index or identity != identity_default else chosen.source,
    )


def announce(account: AccountChoice, ssh: SshChoice | None = None) -> str:
    """Print, and return, the one line naming the account and key this run chose.

    Printed on every path, interactive or not. The scriptable path has no
    prompt to have shown the operator what it picked, and "it printed the
    account it used" is the difference between #4181's wrong-account run being
    obvious and being invisible.
    """
    parts = [f"AWS account: {account.label}"]
    if account.region:
        parts.append(f"region: {account.region}")
    if ssh is not None:
        parts.append(f"SSH key: {ssh.label}")
        if ssh.identity_file:
            parts.append(f"identity: {_tilde(ssh.identity_file)}")
    line = " | ".join(parts)
    print(line)
    return line


__all__ = [
    "NONINTERACTIVE_ENV",
    "AccountChoice",
    "KeyPairMatch",
    "SshChoice",
    "account_default",
    "account_id",
    "announce",
    "ask",
    "ask_choice",
    "configured_reference",
    "ec2_key_pairs",
    "local_public_keys",
    "matching_key_pairs",
    "missing_inputs_error",
    "normalize_fingerprint",
    "private_key_for",
    "prompting_enabled",
    "public_key_blob",
    "public_key_fingerprints",
    "recorded_settings",
    "resolve_account",
    "resolve_ssh",
    "ssh_candidates",
]

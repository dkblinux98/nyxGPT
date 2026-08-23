"""One place that answers "what is this component's Homebrew service called?".

Homebrew names a service after its *formula*, and the candidate channel
publishes each service under a versioned formula name -- `3.0.0rc12` is
installed as `nyxgpt-api@3.0.0rc` (`scripts/build_homebrew_artifacts.py`'s
`formula_name`, and `ops._remote_tap_formula` on the install side). Both
`ops.py` and `self_heal.py` carried their own hard-coded
`{"api": "nyxgpt-api", ...}` map and looked the component up by that literal
name, so on a release-candidate install they asked `brew services list` about
a service nothing had registered:

* `nyxgpt up` waited its full 180s and exited 2 on a stack that was entirely
  running, printing `Still unhealthy: api, web`;
* `nyxgpt ops status` reported both components as `none`.

That is #3853, and the candidate channel is the *acceptance-testing* path
(`docs/homebrew.md#candidate-channel`), so the one install flow used to accept
a release was the one where `nyxgpt up` structurally could not succeed.

The rule this module encodes: **a component's service name is read from what
is actually registered, never asserted from a constant.** `resolve()` matches
both `nyxgpt-web` and `nyxgpt-web@<line>rc` and, when a machine carries
several (an older release's keg never uninstalled alongside a candidate --
the state the owner's Mac was in), prefers the one that is actually running.

It lives in its own module rather than in either caller for the reason
**D-022** gives for `k8s_pod_state.py`: two modules that must agree about what
is installed cannot each keep a copy of the answer. `ops.py` already imports
`self_heal.py`, so the shared vocabulary has to sit below both -- this module
imports neither, and nothing else in the package.

**The class sweep (#3853 AC5), and why it is confined to Homebrew.** The
defect is "an artifact is resolved by a hard-coded name that a versioned
install changes", so every other service/artifact name nyxGPT looks up was
checked for the same shape:

* **Linux systemd (`NATIVE_SYSTEMD_SERVICES`)** -- not affected. The unit name
  is not derived from a package: `ops/systemd/nyxgpt-api.service` is a
  template nyxGPT renders itself, identical in dev and artifact mode
  (`_install_and_activate_native_systemd_unit`), and the Linux artifact
  install is a tarball into `~/.nyxGPT/opt/<service>`, not a versioned
  package manager entry. There is no `nyxgpt-api@3.0.0rc.service` for
  anything to miss.
* **Docker (`NATIVE_CASSANDRA_CONTAINER`, `TERRAFORM_CONTAINERS`)** and
  **Kubernetes (`K8S_CORE_POD_APPS`)** -- not affected. Those names are
  chosen by nyxGPT at `docker run --name` / in `k8s/*.yaml`; the image tag
  carries the version, and the name does not.
* **Homebrew launchd labels** -- already correct, and for this exact reason:
  `ops._brew_service_launchd_labels` matches `homebrew.mxcl.nyxgpt*` by
  prefix rather than against a known formula list, so a teardown reaches a
  release line the running build has never heard of (#3859).
* **Log paths** -- unaffected by design: both channels' formulas declare
  `log_path var/"log/nyxgpt-api.log"`, so `nyxgpt ops logs api` needs no
  channel-specific path.

So Homebrew is the only place a *published artifact's* name varies with the
release channel, which is why the resolution lives here and not in a
service-manager-agnostic layer.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

__all__ = [
    "LIVE_STATES",
    "NATIVE_BREW_SERVICES",
    "VERSIONED_COMPONENTS",
    "format_variants",
    "is_variant_of",
    "parse_services_list",
    "resolve",
    "resolve_all",
    "strip_ansi",
    "superseded",
    "unique",
    "variants",
]

# Maps a core native component to the *stable* Homebrew formula its service is
# named after. This is the base name a candidate install suffixes, never the
# name to look up directly -- see `resolve`.
#
# Cassandra is absent on purpose: per product_management/PHASE_6_PLAN.md it
# stays the one ops-managed Docker container even under native-first, so it is
# tracked through `docker ps` rather than `brew services`.
NATIVE_BREW_SERVICES: dict[str, str] = {
    "api": "nyxgpt-api",
    "web": "nyxgpt-web",
    "ollama": "ollama",
}

# The components nyxGPT itself publishes formulas for, and therefore the only
# ones a versioned variant can exist for. `ollama` is upstream Homebrew's
# formula: nothing in this project ever registers an `ollama@...` service, and
# treating an unrelated versioned Ollama keg as nyxGPT's would be this defect
# in the other direction.
VERSIONED_COMPONENTS: frozenset[str] = frozenset({"api", "web"})

# `brew services list` spellings for "this service is up". `started` is what
# Homebrew prints; `running` is accepted because the same vocabulary is read
# back from Compose/Terraform probes that spell it that way.
LIVE_STATES: frozenset[str] = frozenset({"started", "running"})


# A CSI escape sequence: `ESC [` then parameter/intermediate bytes then a
# final byte in `@`-`~`. Broader than the `\x1b\[[0-9;]*m` colour case on
# purpose -- `strip_ansi` exists to make a comparison against brew's output
# safe, and a comparison that is safe only against the escapes we happened to
# think of is the defect it is fixing.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """`text` with ANSI escape sequences removed.

    **Every literal comparison against brew's output goes through here**, and
    that is the point rather than a nicety. `brew services list` colours its
    Status column on a real macOS runner, and the escapes survive a pipe.
    `macos-brew-smoke.yml` run 32233162053 printed brew's rows through
    `cat -v`, which settles it byte-for-byte (`ESC` standing for the 0x1b byte
    the log carries literally):

        nyxgpt-api         ESC[39mnoneESC[0m
        nyxgpt-api@3.0.0rc ESC[31merror  ESC[0m3 runner ~/Library/...plist

    Every state token is wrapped, `none` included, so a coloured token
    compares equal to nothing -- `state == "started"` is False for a running
    service, `state != "none"` is True for one brew is not running -- and
    every consumer of a parsed state (`LIVE_STATES` in `_rank` and
    `ops._restart_native_api_for`, `superseded`'s `registered_only` filter,
    `ops`'s ollama and `native_running` reads, and `self_heal`'s
    `healthy = state == "started"`, which `nyxgpt up`'s exit gate rides on)
    silently inverts. This is the mechanism behind the misreads in runs
    32222041921 and 32228088507, where a column-based read reported services
    launchd had already forgotten; the competing "the column goes stale"
    explanation is falsified by the same capture, in which the just-retired
    `nyxgpt-api` reads `none` with an empty File field within the second.

    Fixing it here rather than at each of those sites is deliberate: they read
    a state they did not fetch, so none of them can know whether it was
    coloured, and a per-site guard would have to be re-added every time a new
    reader appears. `parse_services_list` is the one chokepoint every state
    reaches them through (#3861).
    """
    return _ANSI_CSI.sub("", text)


def parse_services_list(stdout: str) -> dict[str, str]:
    """Return `{service_name: state}` parsed from `brew services list` output.

    Escapes are stripped first, so callers compare against `started`/`none`
    and not against a colour-wrapped `error` -- see `strip_ansi` for why that
    is not cosmetic.

    The header row (`Name Status User File`) parses to
    `{"Name": "Status"}` and is harmless: no component base name is `Name`,
    so it can never be matched by `resolve`. Filtering it by string would
    couple this parser to Homebrew's column headings, which change.
    """
    snapshot: dict[str, str] = {}
    for line in strip_ansi(stdout).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            snapshot[parts[0]] = parts[1]
    return snapshot


def is_variant_of(service: str, base: str) -> bool:
    """True if brew service `service` is `base` or a versioned formula of it.

    `nyxgpt-api` and `nyxgpt-api@3.0.0rc` are both variants of `nyxgpt-api`;
    `nyxgpt-api-canary` is not. The `@` is required, so the prefix match
    cannot swallow a differently-named formula that merely starts the same
    way.
    """
    return service == base or service.startswith(f"{base}@")


def variants(base: str, snapshot: Mapping[str, str]) -> list[str]:
    """Every service in `snapshot` that is `base` or a versioned formula of it.

    Ordered newest-name-first among versioned entries (`@3.1.0rc` before
    `@3.0.0rc`), which is the tie-break `resolve` inherits.
    """
    return sorted((name for name in snapshot if is_variant_of(name, base)), reverse=True)


def _rank(name: str, snapshot: Mapping[str, str]) -> tuple[int, int]:
    """Sort key for `variants`: running first, then versioned over unversioned.

    Running first is the load-bearing half and the acceptance criterion: on
    the owner's Mac the stale unversioned `nyxgpt-api` was registered in
    `error` while the candidate `nyxgpt-api@3.0.0rc` was `started`, and the
    probe has to report the one that is actually serving.

    Versioned-over-unversioned only breaks a tie where *neither* is running,
    and it is deliberate rather than arbitrary: an unversioned keg can be a
    prior release nobody uninstalled (Homebrew treats a differently-named
    formula as unrelated software, so no install removes it), while a
    versioned formula only ever exists because someone deliberately installed
    that candidate. Both report the same state either way -- the tie-break
    decides which *name* is shown, not whether the component is healthy.
    """
    return (
        0 if snapshot.get(name, "") in LIVE_STATES else 1,
        0 if "@" in name else 1,
    )


def resolve(component: str, base: str, snapshot: Mapping[str, str]) -> str:
    """The brew service name to read/act on for `component`, per `snapshot`.

    Falls back to `base` when nothing matching is registered, which preserves
    the "not in the snapshot means not installed, not down" contract every
    caller already relies on: the caller's own `snapshot.get(name)` still
    answers `None` and the component stays out of scope rather than being
    reported unhealthy.

    `component` selects whether versioned variants are considered at all
    (`VERSIONED_COMPONENTS`); `base` is that component's stable formula name.
    """
    if component not in VERSIONED_COMPONENTS:
        return base
    candidates = variants(base, snapshot)
    if not candidates:
        return base
    # A stable sort over the already newest-first `candidates` keeps that
    # ordering inside each rank.
    return sorted(candidates, key=lambda name: _rank(name, snapshot))[0]


def resolve_all(bases: Mapping[str, str], snapshot: Mapping[str, str]) -> dict[str, str]:
    """`{component: resolved service name}` for every component in `bases`."""
    return {component: resolve(component, base, snapshot) for component, base in bases.items()}


def superseded(
    base: str, snapshot: Mapping[str, str], *, keep: str, registered_only: bool = True
) -> list[str]:
    """Variants of `base` that are registered but are not `keep`.

    The competing-service half of #3853. A candidate install registers
    `nyxgpt-api@3.0.0rc` while a prior release's `nyxgpt-api` service stays
    registered with `keep_alive true`: launchd relaunches it, it loses the
    race for `127.0.0.1:8000`, exits, and is relaunched -- forever, filing an
    `[Errno 48] address already in use` into the error tracker the whole time.
    Nothing removed it because, to Homebrew, a differently-named formula is
    unrelated software.

    `registered_only` (the default) drops entries brew reports as `none` --
    a formula it knows about with no service registered, which is nothing to
    stop.
    """
    return [
        name
        for name in variants(base, snapshot)
        if name != keep and (not registered_only or snapshot.get(name, "none") != "none")
    ]


def format_variants(base: str, snapshot: Mapping[str, str]) -> str:
    """`"nyxgpt-api@3.0.0rc (started), nyxgpt-api (error)"`, for operator output."""
    return ", ".join(f"{name} ({snapshot.get(name, 'none')})" for name in variants(base, snapshot))


def unique(names: Iterable[str]) -> list[str]:
    """De-duplicate `names`, preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


# ---- Fully-qualified formula names (#3861, #4018 review) --------------------
#
# Lives here for the same D-022 reason the rest of this module does: `ops.py`
# and `self_heal.py` both name Homebrew formulas, `ops.py` imports
# `self_heal.py`, and two modules that must agree about which tap owns a keg
# cannot each keep a copy of the answer. The first version of the #3861 fix
# qualified only ops' seven sites and left self_heal's `brew services restart`
# bare -- so on the dual-tap machine the fix exists for, the *automated*
# recovery path still failed where the manual one had been repaired.

# A single path segment brew will accept. Kept identical to the inline barrier
# self_heal has carried since CodeQL #4 so that admitting `<tap>/<name>` widens
# nothing: the qualified form is validated segment by segment against this same
# character class, never by relaxing it to allow `/`.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def is_safe_formula_spec(spec: str) -> bool:
    """True if `spec` is a bare name or `<owner>/<tap>/<name>`, all segments safe."""
    parts = spec.split("/")
    if len(parts) not in (1, 3):
        return False
    return all(_SEGMENT.fullmatch(part) for part in parts)


def cellar(which: Callable[[str], str | None] | None = None) -> Path | None:
    """Homebrew's Cellar directory, or None when it cannot be located.

    `HOMEBREW_CELLAR` is consulted first because it is brew's own answer.
    Otherwise both layouts are tried: Apple Silicon resolves
    `/opt/homebrew/bin/brew` to a prefix whose sibling `Cellar` is correct,
    while Intel macOS and Linuxbrew resolve into `.../Homebrew`, whose parent
    holds the Cellar. The old `parents[1]` alone silently returned None there,
    degrading every caller back to the bare name (#4018 review, Minor).
    """
    import os

    env = os.environ.get("HOMEBREW_CELLAR")
    if env and Path(env).is_dir():
        return Path(env)
    # `which` is injected so each caller keeps its own lookup (and its tests'
    # seam) rather than this module reaching around them.
    brew = (which or shutil.which)("brew")
    if brew is None:
        return None
    resolved = Path(brew).resolve()
    for candidate in (resolved.parents[1] / "Cellar", resolved.parents[2] / "Cellar"):
        if candidate.is_dir():
            return candidate
    return None


def formula_spec(name: str, which: Callable[[str], str | None] | None = None) -> str:
    """`<tap>/<name>` for an installed keg, else `name` unchanged (#3861).

    A bare formula name stops being unambiguous the moment a machine carries
    two taps that both define it, and brew then refuses the command outright
    ("Formulae found in multiple taps ... Please use the fully-qualified
    name") rather than picking one. Which tap owns the keg is *recorded* by
    the install, in `INSTALL_RECEIPT.json` (`source.tap`), so this reads it
    rather than guessing a preferred tap -- a guess would be the same class of
    error as the bare name.

    Falls back to the bare name when nothing is installed under it (there is no
    keg to disambiguate, and brew's own error is then the right answer), when
    the receipt names no tap, or when the result would not be a safe spec.
    """
    root = cellar(which)
    if root is None:
        return name
    keg = root / name
    try:
        receipts = sorted(keg.glob("*/INSTALL_RECEIPT.json"))
    except OSError:  # pragma: no cover - unreadable Cellar
        return name
    for receipt in receipts:
        try:
            source = json.loads(receipt.read_text(encoding="utf-8")).get("source") or {}
        except (OSError, ValueError, AttributeError):
            continue
        tap = source.get("tap") if isinstance(source, dict) else None
        # `homebrew/core` formulas are never ambiguous and brew spells the
        # qualified form differently across versions; leave those bare.
        if isinstance(tap, str) and tap and tap != "homebrew/core":
            qualified = f"{tap}/{name}"
            return qualified if is_safe_formula_spec(qualified) else name
    return name

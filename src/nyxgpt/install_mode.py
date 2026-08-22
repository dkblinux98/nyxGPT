"""Which *build* a deployment's `api`/`web` were installed from (#3789, #3834,
#3835, #3861).

Two modes exist, and every layer that starts, stops, probes or reports on the
services needs to agree on which one is live:

- **artifact** (the default, and the only mode before #3789): api/web are
  built from a distributable artifact -- a Homebrew keg on macOS, a source
  tarball installed into a self-contained venv on Linux, and for both the
  Kubernetes and the Terraform deployments a container image built locally
  from the published `nyxgpt-api`/`nyxgpt-web` source tarballs (#3834, and
  #3985 which moved Terraform onto the same channel: an rc publishes those
  tarballs but no `ghcr.io` image, so pulling one could never install a
  candidate). This is what makes a machine with no checkout installable at
  all (the repo-less guarantee, #3504), so it stays the default.
- **dev**: an explicitly opted-into (`nyxgpt up --dev`) checkout-only mode --
  natively the api is an editable venv (`pip install -e <checkout>`) and the
  web UI runs Next's dev server out of `<checkout>/web`; in Kubernetes and
  under Terraform the two images are built from the working tree instead of
  from artifacts. Either way the deployment runs what the checkout holds,
  with no published artifact in between.

The mode is *recorded* here rather than re-derived, because the two modes use
different service managers on macOS (launchd agents for dev, `brew services`
for artifact) and guessing wrong is not a cosmetic error: it would start the
old keg's api on the port the dev process already holds.

**A mode is not an identity (#3861).** Until #3861 the record above *was* the
whole model -- `mode`, plus a checkout path for dev mode -- so two artifact
installs were indistinguishable from each other. Installing
`nyxgpt-api@3.0.0rc` over an existing `nyxgpt-api` 2.1.0 wrote `artifact`
where `artifact` already stood, and `ops._reconcile_install_mode`'s
`previous.mode != target` gate saw nothing to reconcile. That is not a lazy
check; it is the strongest check a two-value model can support. The owner's
Mac accumulated four concurrent install identities that way, two of them
`keep_alive` services registered on the same ports, producing a permanent
crash loop (`[Errno 48] address already in use ('127.0.0.1', 8000)`) that
nothing in the system could see -- the exact hazard the paragraph above says
the marker exists to prevent, in the one direction it did not model.

So what is recorded is an `InstallIdentity`: the service **manager**, the
concrete **service name per component** (`nyxgpt-api@3.0.0rc`, not
`nyxgpt-api`), the installed **version** and the **channel**, with `mode` as
one field of it. Reconciliation compares whole identities and acts on any
difference, rather than consulting a hand-maintained list of transition pairs
-- a pair list reproduces this defect the first time an unanticipated pair
appears, and every future artifact form (a Linux tarball venv, a container
image) is such a pair.

Identity *detection* deliberately lives in `nyxgpt.ops`, not here: it needs to
know which tap, formula and version an install is about to use, and this
module must stay import-free of `ops` (see the last paragraph). What lives
here is the dataclass, its vocabulary, its comparison and its serialisation.

**One marker per substrate (#3834, extended to Terraform by #3835).** The mode
is a property of a *deployment*, not of a machine: a host can have a native dev
install and a Kubernetes or Terraform artifact deployment at the same time, and
reporting one machine-wide answer is what made `ops status` label a pure-k8s
deployment `dev (editable checkout at ...)` -- leftover from an earlier `nyxgpt
up --dev` -- and stamp `[dev]` on `native api: none`. The Terraform path had the
same defect from the other end: it wrote no marker at all, so its deployment was
reported with whatever the native install last recorded. So each substrate
writes and reads its own marker, and a reader must say which substrate it is
asking about. Separate files also mean a Terraform or Kubernetes install can
never overwrite the native marker that decides whether `restart api` drives
launchd or `brew services`.

This module deliberately holds nothing but the markers and their vocabulary so
both `nyxgpt.ops` (which writes them) and `nyxgpt.self_heal` (which must not
import ops -- ops imports it) can read them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_MODE_ARTIFACT = "artifact"
INSTALL_MODE_DEV = "dev"

# Display-only third state (#3835): a deployment that is *running* but whose
# marker is absent. It is never a value of `mode` and is never written to a
# marker file -- it is precisely the absence of one. It exists because
# "nothing was recorded" and "artifact was recorded" are different facts, and
# for a Terraform deployment the artifact default is not merely unproven, it
# is the wrong way round: every deployment made before #3835 was built from
# the working tree.
INSTALL_MODE_UNRECORDED = "unrecorded"

# The substrates that record an install mode. `native` is the local-first
# api/web pair (brew services / systemd --user); `kubernetes` is the
# `--kubernetes --local` deployment's two images; `terraform` is the
# `--terraform --local` deployment's two containers.
SUBSTRATE_NATIVE = "native"
SUBSTRATE_KUBERNETES = "kubernetes"
SUBSTRATE_TERRAFORM = "terraform"

# Same ops-managed home `nyxgpt.ops.NYXGPT_HOME` points at, resolved
# independently to keep this module free of an ops import.
NYXGPT_HOME = Path.home() / ".nyxGPT"

# The native marker keeps its original, unqualified filename: it is the one
# every machine installed before #3834 already has, and renaming it would
# silently downgrade a live dev install to "artifact" on upgrade.
INSTALL_MODE_FILE = NYXGPT_HOME / "install-mode.json"

# The launchd labels dev mode uses for api/web on macOS, in place of the
# `brew services` the artifact path installs (there is no keg to attach a
# brew service to). Linux needs no equivalent map: both modes drive the same
# `nyxgpt-api`/`nyxgpt-web` systemd --user units, and only the wrapper script
# those units exec differs between them.
DEV_LAUNCHD_LABELS: dict[str, str] = {
    "api": "com.nyxgpt.api",
    "web": "com.nyxgpt.web",
}

# The service managers an install identity can name (#3861). `launchd` is dev
# mode's macOS manager, `brew` the artifact path's; `systemd` is both modes on
# Linux -- which is exactly why the manager alone cannot disambiguate an
# identity there and the version/channel fields carry the whole signal.
MANAGER_BREW = "brew"
MANAGER_LAUNCHD = "launchd"
MANAGER_SYSTEMD = "systemd"
MANAGER_UNKNOWN = "unknown"

# Which published channel an artifact identity came from. `candidate` is the
# `nyxgpt-api@<line>rc` formula line (docs/homebrew.md#candidate-channel);
# `dev` is not a published channel at all and marks a checkout build.
CHANNEL_STABLE = "stable"
CHANNEL_CANDIDATE = "candidate"
CHANNEL_DEV = "dev"
CHANNEL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstallIdentity:
    """*Which* build is installed, not merely how it was installed (#3861).

    Every field is part of the comparison, because the point of the type is
    that reconciliation is a comparison rather than a table of anticipated
    transitions. `services` maps a logical component (`api`, `web`) to the
    concrete name its manager registered it under -- `nyxgpt-api@3.0.0rc` for
    a candidate keg, `com.nyxgpt.api` for a dev LaunchAgent, `nyxgpt-api` for
    a systemd --user unit -- and that mapping is what a teardown or a
    reconcile needs in order to stop the *previous* install rather than
    whatever the stable names happen to be.

    `known=False` means "nothing recorded an identity here": a marker written
    before #3861 (mode + checkout only), a marker that could not be parsed, or
    no marker at all. An unknown identity **never compares equal to
    anything**, including another unknown one -- treating "I do not know" as
    "the same" is precisely the failure this type exists to remove.
    """

    mode: str = INSTALL_MODE_ARTIFACT
    manager: str = MANAGER_UNKNOWN
    # Sorted (component, service name) pairs rather than a dict, so the
    # dataclass stays hashable and comparison is order-independent.
    services: tuple[tuple[str, str], ...] = ()
    version: str = ""
    channel: str = CHANNEL_UNKNOWN
    checkout: str | None = None
    known: bool = False

    @classmethod
    def build(
        cls,
        *,
        mode: str,
        manager: str,
        services: Mapping[str, str],
        version: str,
        channel: str,
        checkout: Path | str | None = None,
    ) -> InstallIdentity:
        """A known identity, with `services` normalised into sorted pairs."""
        return cls(
            mode=mode,
            manager=manager,
            services=tuple(sorted((str(k), str(v)) for k, v in services.items())),
            version=str(version),
            channel=channel,
            checkout=str(checkout) if checkout else None,
            known=True,
        )

    @property
    def service_names(self) -> tuple[str, ...]:
        """The concrete service names this identity registered, deduplicated."""
        return tuple(sorted({name for _component, name in self.services}))

    @property
    def service_map(self) -> dict[str, str]:
        """`{component: service name}` -- the pairs as a plain mapping."""
        return dict(self.services)

    def differences(self, other: InstallIdentity) -> list[str]:
        """Human-readable list of every way `other` differs from `self`.

        Empty means "the same install"; anything else means the previous
        install must be reconciled before the new one starts. An unknown
        identity on either side yields a single "unknown" difference: unknown
        is a *possible* mismatch, and the caller reconciles defensively rather
        than assuming the machine is already in the state it wants.
        """
        if not self.known or not other.known:
            return ["previous install identity is unknown (no marker, or one written before #3861)"]
        fields = (
            ("mode", self.mode, other.mode),
            ("service manager", self.manager, other.manager),
            ("version", self.version, other.version),
            ("channel", self.channel, other.channel),
            ("checkout", self.checkout, other.checkout),
        )
        diffs = [
            f"{name}: {was or 'none'} -> {now or 'none'}" for name, was, now in fields if was != now
        ]
        if self.services != other.services:
            diffs.append(f"services: {self._service_text()} -> {other._service_text()}")
        return diffs

    def _service_text(self) -> str:
        """`api=nyxgpt-api, web=nyxgpt-web`, or `none` when nothing is recorded."""
        return ", ".join(f"{c}={n}" for c, n in self.services) or "none"

    def detail(self) -> str:
        """One-line description of which build this is, for status/doctor output."""
        if not self.known:
            return "no install identity recorded"
        parts = [f"{self.manager}: {self._service_text()}"]
        if self.version:
            parts.append(f"version {self.version}")
        parts.append(f"channel {self.channel}")
        if self.checkout:
            parts.append(f"checkout {self.checkout}")
        return "; ".join(parts)

    def to_payload(self) -> dict[str, object] | None:
        """JSON form for the marker file, or None when nothing is known."""
        if not self.known:
            return None
        return {
            "mode": self.mode,
            "manager": self.manager,
            "services": self.service_map,
            "version": self.version,
            "channel": self.channel,
            "checkout": self.checkout,
        }

    @classmethod
    def from_payload(cls, raw: object) -> InstallIdentity:
        """Parse a marker's `identity` block; anything malformed reads as unknown.

        Never raises, for the same reason `read_install_mode` never raises: a
        marker a future version wrote differently, or a half-written file,
        must not be able to break an install.
        """
        if not isinstance(raw, dict):
            return cls()
        services = raw.get("services")
        if raw.get("mode") not in (INSTALL_MODE_DEV, INSTALL_MODE_ARTIFACT) or not isinstance(
            services, dict
        ):
            # An identity is a mode *and* the service names that mode
            # registered. A block missing either -- an empty block, a
            # truncated write, or one a future version spells differently --
            # is not a partial identity to be filled in with defaults: a
            # `known` identity whose service map is empty would subtract to
            # "nothing to retire" and quietly under-reconcile, which is the
            # class of silence this whole change exists to remove. Unknown is
            # the honest answer and the defensive one.
            return cls()
        pairs = tuple(sorted((str(k), str(v)) for k, v in services.items()))
        checkout = raw.get("checkout")
        return cls(
            mode=str(raw["mode"]),
            manager=str(raw.get("manager") or MANAGER_UNKNOWN),
            services=pairs,
            version=str(raw.get("version") or ""),
            channel=str(raw.get("channel") or CHANNEL_UNKNOWN),
            checkout=str(checkout) if checkout else None,
            known=True,
        )


def install_mode_file(substrate: str = SUBSTRATE_NATIVE) -> Path:
    """The marker path recording `substrate`'s install mode."""
    if substrate == SUBSTRATE_NATIVE:
        return INSTALL_MODE_FILE
    return NYXGPT_HOME / f"install-mode-{substrate}.json"


@dataclass
class InstallModeState:
    """A substrate's recorded install mode, plus (for dev mode) its checkout."""

    mode: str = INSTALL_MODE_ARTIFACT
    checkout: str | None = None
    substrate: str = SUBSTRATE_NATIVE
    # Terraform only: the api/web image refs the deployment actually runs, so
    # `ops status`/`doctor` can name them instead of leaving the operator to
    # infer which build is serving (#3835).
    images: dict[str, str] = field(default_factory=dict)
    # Whether a marker was actually read. False means "nothing on this machine
    # recorded a mode for this substrate". For `native` that is the documented
    # artifact default -- it is what every machine installed before #3789 is
    # really running. For any other substrate there is no such history, so a
    # reader must say "unrecorded" rather than assert a mode nobody wrote.
    recorded: bool = False
    # Which build this substrate is running (#3861). Defaults to the unknown
    # identity, which is what a pre-#3861 marker and a missing marker both
    # read back as -- and which never compares equal to the identity an
    # install is about to write, so an install over an unknown previous
    # reconciles defensively instead of assuming there is nothing to do.
    identity: InstallIdentity = field(default_factory=InstallIdentity)

    @property
    def is_dev(self) -> bool:
        """True when this substrate's api/web were installed from a checkout."""
        return self.mode == INSTALL_MODE_DEV

    @property
    def is_terraform(self) -> bool:
        """True when this state describes the Terraform deployment (#3835)."""
        return self.substrate == SUBSTRATE_TERRAFORM

    def label(self, *, deployed: bool = False) -> str:
        """One-line human label for `ops status`/`doctor`.

        `deployed` says whether the deployment this state describes is
        actually running, and only changes the answer for an unrecorded
        Terraform deployment: a live stack with no marker predates #3835 or
        was brought up some other way, and calling that "artifact" asserts
        the opposite of the truth (every pre-#3835 deployment was built from
        the working tree). The native default needs no such care -- artifact
        really was the only mode before #3789 -- and the Kubernetes label
        reports its unrecorded state from `recorded` alone, because its
        callers only print it for a cluster they have already found.
        """
        if self.is_terraform:
            return self._terraform_label(deployed=deployed)
        if self.substrate == SUBSTRATE_KUBERNETES:
            if not self.recorded:
                return (
                    "unrecorded (no install-mode marker for this cluster -- it was deployed "
                    "before nyxGPT recorded one, or from another machine)"
                )
            if self.is_dev:
                return (
                    "dev (images built from the working tree at "
                    f"{self.checkout or 'unknown checkout'})"
                )
            return "artifact (images built from the published nyxgpt-api/nyxgpt-web artifacts)"
        if self.is_dev:
            return self._with_identity(
                f"dev (editable checkout at {self.checkout or 'unknown checkout'})"
            )
        return self._with_identity("artifact (published/vendored build -- the repo-less default)")

    def _with_identity(self, base: str) -> str:
        """`base`, plus which build it actually is when an identity is recorded.

        The reason this exists (#3861): the bare native label printed
        `artifact (published/vendored build -- the repo-less default)` for a
        2.1.0 keg and for a 3.0.0rc12 keg alike, so an operator reading
        `ops status` on a machine carrying both could not tell them apart --
        the same blindness that let the two accumulate.
        """
        return f"{base} [{self.identity.detail()}]" if self.identity.known else base

    def short_label(self, *, deployed: bool = False) -> str:
        """One word for a per-component tag: dev, artifact, or unrecorded.

        Same tri-state as `label`, so a status line's per-component tags can
        never disagree with the deployment line above them.
        """
        if self._is_unrecorded_deployment(deployed):
            return INSTALL_MODE_UNRECORDED
        return INSTALL_MODE_DEV if self.is_dev else INSTALL_MODE_ARTIFACT

    def _is_unrecorded_deployment(self, deployed: bool) -> bool:
        """True for a running Terraform deployment whose mode nothing recorded."""
        return self.is_terraform and deployed and not self.recorded

    def _terraform_label(self, *, deployed: bool = False) -> str:
        """Label for a Terraform deployment, naming the images it is running."""
        images = ", ".join(f"{component}={ref}" for component, ref in sorted(self.images.items()))
        suffix = f" [{images}]" if images else ""
        if self._is_unrecorded_deployment(deployed):
            return (
                "not recorded (a Terraform deployment is running that no `nyxgpt ops install "
                "--terraform` recorded -- it predates #3835 or was brought up another way, so "
                "whether its api/web images were built from a checkout or pulled is unknown). "
                "Re-run `nyxgpt up --terraform` (add `--dev` for a working-tree build) "
                f"to redeploy it and record the mode.{suffix}"
            )
        if self.is_dev:
            checkout = self.checkout or "unknown checkout"
            return f"dev (images built from the working tree at {checkout}){suffix}"
        # The refs in `suffix` carry the version (`nyxgpt-api:artifact-3.0.0rc13`),
        # which is what lets an operator read *which build* is serving off
        # `ops status` rather than infer it (#3985).
        return f"artifact (images built from the published nyxgpt-api/nyxgpt-web artifacts){suffix}"


def read_install_mode(
    path: Path | None = None, *, substrate: str = SUBSTRATE_NATIVE
) -> InstallModeState:
    """Read `substrate`'s recorded install mode, defaulting to artifact mode.

    Never raises: a missing, unreadable or malformed marker means "nothing
    recorded a dev install for this substrate", and artifact mode is both the
    default and the safe answer. `recorded` reports which of the two happened,
    so a caller that must not present the default as a fact (the Kubernetes
    report, #3834; a running Terraform stack, #3835) can tell them apart --
    for Terraform the default is not merely unproven but the wrong way round,
    since every deployment made before #3835 was built from a working tree.
    """
    marker = path or install_mode_file(substrate)
    default = InstallModeState(substrate=substrate)
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        logger.warning(
            "Could not read the install-mode marker %s, assuming artifact mode: %s",
            marker,
            e,
        )
        return default
    if not isinstance(raw, dict) or raw.get("mode") not in (
        INSTALL_MODE_DEV,
        INSTALL_MODE_ARTIFACT,
    ):
        return default
    raw_images = raw.get("images")
    images = {str(k): str(v) for k, v in raw_images.items()} if isinstance(raw_images, dict) else {}
    # A marker written before #3861 carries no `identity` block, so this is
    # the unknown identity -- deliberately, and not the same thing as "the
    # identity is whatever the mode field says". See `InstallIdentity`.
    identity = InstallIdentity.from_payload(raw.get("identity"))
    if raw.get("mode") != INSTALL_MODE_DEV:
        return InstallModeState(
            substrate=substrate, images=images, recorded=True, identity=identity
        )
    checkout = raw.get("checkout")
    return InstallModeState(
        mode=INSTALL_MODE_DEV,
        checkout=str(checkout) if checkout else None,
        substrate=substrate,
        images=images,
        recorded=True,
        identity=identity,
    )


def write_install_mode(
    mode: str,
    checkout: Path | str | None,
    path: Path | None = None,
    *,
    substrate: str = SUBSTRATE_NATIVE,
    images: dict[str, str] | None = None,
    identity: InstallIdentity | None = None,
) -> Path:
    """Record `mode` (and, for dev mode, its checkout) and return the marker path.

    `images` records which api/web image refs a Terraform deployment was
    brought up from; the other substrates leave it empty.

    `identity` records *which build* this is (#3861) -- manager, per-component
    service names, version, channel. Omitting it writes a marker with no
    identity block, which reads back as the unknown identity: a caller that
    cannot describe what it installed must not leave behind a record implying
    it could.
    """
    marker = path or install_mode_file(substrate)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "mode": mode,
        "checkout": str(checkout) if checkout else None,
        "substrate": substrate,
        "images": dict(images or {}),
    }
    identity_payload = identity.to_payload() if identity else None
    if identity_payload is not None:
        payload["identity"] = identity_payload
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return marker


def clear_install_mode(path: Path | None = None, *, substrate: str = SUBSTRATE_NATIVE) -> Path:
    """Remove `substrate`'s marker (the deployment it described is gone).

    Torn-down deployments must stop being reported: a marker left behind by
    `ops down --kubernetes` is exactly the stale record that made `ops status`
    describe a deployment that no longer existed (#3834). Returns the marker
    path whether or not it was there.
    """
    marker = path or install_mode_file(substrate)
    try:
        marker.unlink(missing_ok=True)
    except OSError as e:  # pragma: no cover - unwritable ~/.nyxGPT
        logger.warning("Could not remove the install-mode marker %s: %s", marker, e)
    return marker

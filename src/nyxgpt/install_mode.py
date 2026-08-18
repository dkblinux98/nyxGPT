"""Which mode a deployment's `api`/`web` were installed in (#3789, #3834, #3835).

Two modes exist, and every layer that starts, stops, probes or reports on the
services needs to agree on which one is live:

- **artifact** (the default, and the only mode before #3789): api/web are
  built from a distributable artifact -- a Homebrew keg on macOS, a source
  tarball installed into a self-contained venv on Linux, a container image
  built from the published `nyxgpt-api`/`nyxgpt-web` tarballs in Kubernetes,
  the published `ghcr.io` images for the Terraform deployment. This is what
  makes a machine with no checkout installable at all (the repo-less
  guarantee, #3504), so it stays the default.
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
            return f"dev (editable checkout at {self.checkout or 'unknown checkout'})"
        return "artifact (published/vendored build -- the repo-less default)"

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
                "Re-run `nyxgpt up --terraform --local` (add `--dev` for a working-tree build) "
                f"to redeploy it and record the mode.{suffix}"
            )
        if self.is_dev:
            checkout = self.checkout or "unknown checkout"
            return f"dev (images built from the working tree at {checkout}){suffix}"
        return f"artifact (published container images -- the repo-less default){suffix}"


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
    if raw.get("mode") != INSTALL_MODE_DEV:
        return InstallModeState(substrate=substrate, images=images, recorded=True)
    checkout = raw.get("checkout")
    return InstallModeState(
        mode=INSTALL_MODE_DEV,
        checkout=str(checkout) if checkout else None,
        substrate=substrate,
        images=images,
        recorded=True,
    )


def write_install_mode(
    mode: str,
    checkout: Path | str | None,
    path: Path | None = None,
    *,
    substrate: str = SUBSTRATE_NATIVE,
    images: dict[str, str] | None = None,
) -> Path:
    """Record `mode` (and, for dev mode, its checkout) and return the marker path.

    `images` records which api/web image refs a Terraform deployment was
    brought up from; the other substrates leave it empty.
    """
    marker = path or install_mode_file(substrate)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "checkout": str(checkout) if checkout else None,
        "substrate": substrate,
        "images": dict(images or {}),
    }
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

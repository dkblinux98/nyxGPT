"""Which mode a deployment's `api`/`web` were installed in (#3789, #3835).

Two modes exist, and every layer that starts, stops, probes or reports on the
services needs to agree on which one is live:

- **artifact** (the default, and the only mode before #3789): api/web are
  built from a distributable artifact -- a Homebrew keg on macOS, a source
  tarball installed into a self-contained venv on Linux, the published
  `ghcr.io` container images for the Terraform deployment. This is what makes
  a machine with no checkout installable at all (the repo-less guarantee,
  #3504), so it stays the default and is unaffected by anything here.
- **dev**: an explicitly opted-into (`nyxgpt up --dev`) checkout-only mode --
  natively the api is an editable venv (`pip install -e <checkout>`) and the
  web UI runs Next's dev server out of `<checkout>/web`; under Terraform the
  api/web images are built from that same working tree. Either way the
  services run whatever the checkout holds right now, with no keg, tarball or
  published image in between.

The mode is *recorded* here rather than re-derived, because the two modes use
different service managers on macOS (launchd agents for dev, `brew services`
for artifact) and guessing wrong is not a cosmetic error: it would start the
old keg's api on the port the dev process already holds.

Each *deployment* records its own marker (#3835). The native services and a
Terraform deployment can be in different modes on one machine -- and were
before this split, silently: the Terraform path never wrote a marker at all,
so `ops status` labelled a Terraform deployment with whatever the native
install last recorded. Two files, one per deployment, also means a Terraform
install can never overwrite the native marker that decides whether `restart
api` drives launchd or `brew services`.

This module deliberately holds nothing but the markers and their vocabulary
so both `nyxgpt.ops` (which writes them) and `nyxgpt.self_heal` (which must
not import ops -- ops imports it) can read them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_MODE_ARTIFACT = "artifact"
INSTALL_MODE_DEV = "dev"

# Which deployment a marker describes. The native services and a Terraform
# deployment each keep their own file (see the module docstring).
DEPLOYMENT_NATIVE = "native"
DEPLOYMENT_TERRAFORM = "terraform"

# Same ops-managed home `nyxgpt.ops.NYXGPT_HOME` points at, resolved
# independently to keep this module free of an ops import.
INSTALL_MODE_FILE = Path.home() / ".nyxGPT" / "install-mode.json"
TERRAFORM_INSTALL_MODE_FILE = Path.home() / ".nyxGPT" / "install-mode.terraform.json"

# The launchd labels dev mode uses for api/web on macOS, in place of the
# `brew services` the artifact path installs (there is no keg to attach a
# brew service to). Linux needs no equivalent map: both modes drive the same
# `nyxgpt-api`/`nyxgpt-web` systemd --user units, and only the wrapper script
# those units exec differs between them.
DEV_LAUNCHD_LABELS: dict[str, str] = {
    "api": "com.nyxgpt.api",
    "web": "com.nyxgpt.web",
}


@dataclass
class InstallModeState:
    """The recorded install mode plus, for dev mode, the checkout it points at."""

    mode: str = INSTALL_MODE_ARTIFACT
    checkout: str | None = None
    # Which deployment this state describes -- `DEPLOYMENT_NATIVE` or
    # `DEPLOYMENT_TERRAFORM`. Set by the reader, not stored in the file: the
    # file that was read is what identifies the deployment.
    deployment: str = DEPLOYMENT_NATIVE
    # Terraform only: the api/web image refs the deployment actually runs, so
    # `ops status`/`doctor` can name them instead of leaving the operator to
    # infer which build is serving (#3835).
    images: dict[str, str] = field(default_factory=dict)

    @property
    def is_dev(self) -> bool:
        """True when this deployment's api/web were installed from a checkout."""
        return self.mode == INSTALL_MODE_DEV

    @property
    def is_terraform(self) -> bool:
        """True when this state describes the Terraform deployment, not the native services."""
        return self.deployment == DEPLOYMENT_TERRAFORM

    def label(self) -> str:
        """One-line human label for `ops status`/`doctor`."""
        if self.is_terraform:
            return self._terraform_label()
        if self.is_dev:
            return f"dev (editable checkout at {self.checkout or 'unknown checkout'})"
        return "artifact (published/vendored build -- the repo-less default)"

    def _terraform_label(self) -> str:
        """Label for a Terraform deployment, naming the images it is running."""
        images = ", ".join(f"{component}={ref}" for component, ref in sorted(self.images.items()))
        suffix = f" [{images}]" if images else ""
        if self.is_dev:
            checkout = self.checkout or "unknown checkout"
            return f"dev (images built from the working tree at {checkout}){suffix}"
        return f"artifact (published container images -- the repo-less default){suffix}"


def _read_marker(marker: Path, deployment: str) -> InstallModeState:
    """Read one deployment's marker file, defaulting to artifact mode.

    Never raises: a missing, unreadable or malformed marker means "nothing
    recorded a dev install for this deployment", and artifact mode is both
    the default and the safe answer -- it is what every machine installed
    before #3789 (natively) / #3835 (Terraform) is actually running.
    """
    default = InstallModeState(deployment=deployment)
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
    if not isinstance(raw, dict):
        return default
    raw_images = raw.get("images")
    images = {str(k): str(v) for k, v in raw_images.items()} if isinstance(raw_images, dict) else {}
    if raw.get("mode") != INSTALL_MODE_DEV:
        return InstallModeState(deployment=deployment, images=images)
    checkout = raw.get("checkout")
    return InstallModeState(
        mode=INSTALL_MODE_DEV,
        checkout=str(checkout) if checkout else None,
        deployment=deployment,
        images=images,
    )


def read_install_mode(path: Path | None = None) -> InstallModeState:
    """Read the native services' recorded install mode, defaulting to artifact mode."""
    return _read_marker(path or INSTALL_MODE_FILE, DEPLOYMENT_NATIVE)


def read_terraform_install_mode(path: Path | None = None) -> InstallModeState:
    """Read the Terraform deployment's recorded install mode (#3835).

    Separate from `read_install_mode` on purpose: the Terraform deployment
    and the native services are installed independently and can be in
    different modes, so reporting one for the other is exactly the defect
    this marker was added to fix.
    """
    return _read_marker(path or TERRAFORM_INSTALL_MODE_FILE, DEPLOYMENT_TERRAFORM)


def write_install_mode(
    mode: str,
    checkout: Path | str | None,
    path: Path | None = None,
    images: dict[str, str] | None = None,
) -> Path:
    """Record `mode` (and, for dev mode, its checkout) and return the marker path.

    `images` records which api/web image refs a Terraform deployment was
    brought up from; the native path leaves it empty.
    """
    marker = path or INSTALL_MODE_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "checkout": str(checkout) if checkout else None,
        "images": dict(images or {}),
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return marker


def write_terraform_install_mode(
    mode: str, checkout: Path | str | None, images: dict[str, str] | None = None
) -> Path:
    """Record the Terraform deployment's install mode and the images it runs (#3835)."""
    return write_install_mode(mode, checkout, TERRAFORM_INSTALL_MODE_FILE, images)

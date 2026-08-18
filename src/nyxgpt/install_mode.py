"""Which mode a deployment's `api`/`web` were installed in (#3789, #3834).

Two modes exist, and every layer that starts, stops, probes or reports on the
services needs to agree on which one is live:

- **artifact** (the default, and the only mode before #3789): api/web are
  built from a distributable artifact -- a Homebrew keg on macOS, a source
  tarball installed into a self-contained venv on Linux, a container image
  built from the published `nyxgpt-api`/`nyxgpt-web` tarballs in Kubernetes.
  This is what makes a machine with no checkout installable at all (the
  repo-less guarantee, #3504), so it stays the default.
- **dev**: an explicitly opted-into (`nyxgpt up --dev`) checkout-only mode --
  natively the api is an editable venv (`pip install -e <checkout>`) and the
  web UI runs Next's dev server out of `<checkout>/web`; in Kubernetes the
  two images are built from the working tree instead of from artifacts. Either
  way the deployment runs what the checkout holds, with no published artifact
  in between.

The mode is *recorded* here rather than re-derived, because the two modes use
different service managers on macOS (launchd agents for dev, `brew services`
for artifact) and guessing wrong is not a cosmetic error: it would start the
old keg's api on the port the dev process already holds.

**One marker per substrate (#3834).** The mode is a property of a *deployment*,
not of a machine: a host can have a native dev install and a Kubernetes
artifact deployment at the same time, and reporting one machine-wide answer is
what made `ops status` label a pure-k8s deployment `dev (editable checkout at
...)` -- leftover from an earlier `nyxgpt up --dev` -- and stamp `[dev]` on
`native api: none`. So each substrate writes and reads its own marker, and a
reader must say which substrate it is asking about.

This module deliberately holds nothing but the markers and their vocabulary so
both `nyxgpt.ops` (which writes them) and `nyxgpt.self_heal` (which must not
import ops -- ops imports it) can read them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_MODE_ARTIFACT = "artifact"
INSTALL_MODE_DEV = "dev"

# The substrates that record an install mode. `native` is the local-first
# api/web pair (brew services / systemd --user); `kubernetes` is the
# `--kubernetes --local` deployment's two images.
SUBSTRATE_NATIVE = "native"
SUBSTRATE_KUBERNETES = "kubernetes"

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

    def label(self) -> str:
        """One-line human label for `ops status`/`doctor`."""
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


def read_install_mode(
    path: Path | None = None, *, substrate: str = SUBSTRATE_NATIVE
) -> InstallModeState:
    """Read `substrate`'s recorded install mode, defaulting to artifact mode.

    Never raises: a missing, unreadable or malformed marker means "nothing
    recorded a dev install for this substrate", and artifact mode is both the
    default and the safe answer. `recorded` reports which of the two happened,
    so a caller that must not present the default as a fact (the Kubernetes
    report, #3834) can tell them apart.
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
    if raw.get("mode") != INSTALL_MODE_DEV:
        return InstallModeState(substrate=substrate, recorded=True)
    checkout = raw.get("checkout")
    return InstallModeState(
        mode=INSTALL_MODE_DEV,
        checkout=str(checkout) if checkout else None,
        substrate=substrate,
        recorded=True,
    )


def write_install_mode(
    mode: str,
    checkout: Path | str | None,
    path: Path | None = None,
    *,
    substrate: str = SUBSTRATE_NATIVE,
) -> Path:
    """Record `mode` (and, for dev mode, its checkout) and return the marker path."""
    marker = path or install_mode_file(substrate)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "checkout": str(checkout) if checkout else None,
        "substrate": substrate,
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

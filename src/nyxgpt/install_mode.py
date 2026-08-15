"""Which mode the native `api`/`web` services were installed in (#3789).

Two modes exist, and every layer that starts, stops, probes or reports on the
native services needs to agree on which one is live:

- **artifact** (the default, and the only mode before #3789): api/web are
  built from a distributable artifact -- a Homebrew keg on macOS, a source
  tarball installed into a self-contained venv on Linux. This is what makes a
  machine with no checkout installable at all (the repo-less guarantee,
  #3504), so it stays the default and is unaffected by anything here.
- **dev**: an explicitly opted-into (`nyxgpt up --dev`) checkout-only mode --
  the api is an editable venv (`pip install -e <checkout>`) and the web UI
  runs Next's dev server out of `<checkout>/web`, so the services run
  whatever the working tree holds right now, with no keg/tarball build in
  between.

The mode is *recorded* here rather than re-derived, because the two modes use
different service managers on macOS (launchd agents for dev, `brew services`
for artifact) and guessing wrong is not a cosmetic error: it would start the
old keg's api on the port the dev process already holds.

This module deliberately holds nothing but the marker and its vocabulary so
both `nyxgpt.ops` (which writes it) and `nyxgpt.self_heal` (which must not
import ops -- ops imports it) can read it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_MODE_ARTIFACT = "artifact"
INSTALL_MODE_DEV = "dev"

# Same ops-managed home `nyxgpt.ops.NYXGPT_HOME` points at, resolved
# independently to keep this module free of an ops import.
INSTALL_MODE_FILE = Path.home() / ".nyxGPT" / "install-mode.json"

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

    @property
    def is_dev(self) -> bool:
        """True when the native api/web services were installed from a checkout."""
        return self.mode == INSTALL_MODE_DEV

    def label(self) -> str:
        """One-line human label for `ops status`/`doctor`."""
        if self.is_dev:
            return f"dev (editable checkout at {self.checkout or 'unknown checkout'})"
        return "artifact (published/vendored build -- the repo-less default)"


def read_install_mode(path: Path | None = None) -> InstallModeState:
    """Read the recorded install mode, defaulting to artifact mode.

    Never raises: a missing, unreadable or malformed marker means "nothing
    recorded a dev install on this machine", and artifact mode is both the
    default and the safe answer -- it is what every machine installed before
    #3789 is actually running.
    """
    marker = path or INSTALL_MODE_FILE
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InstallModeState()
    except (OSError, ValueError) as e:
        logger.warning(
            "Could not read the install-mode marker %s, assuming artifact mode: %s",
            marker,
            e,
        )
        return InstallModeState()
    if not isinstance(raw, dict) or raw.get("mode") != INSTALL_MODE_DEV:
        return InstallModeState()
    checkout = raw.get("checkout")
    return InstallModeState(mode=INSTALL_MODE_DEV, checkout=str(checkout) if checkout else None)


def write_install_mode(mode: str, checkout: Path | str | None, path: Path | None = None) -> Path:
    """Record `mode` (and, for dev mode, its checkout) and return the marker path."""
    marker = path or INSTALL_MODE_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "checkout": str(checkout) if checkout else None}
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return marker

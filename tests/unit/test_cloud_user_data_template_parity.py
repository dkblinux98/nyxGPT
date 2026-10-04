"""The two EC2 user-data bootstraps must not silently disagree about a core service.

`scripts/cloud/ec2-user-data-linux.sh.tmpl` and
`scripts/cloud/ec2-user-data-macos.sh.tmpl` are two renderings of one promise:
`nyxgpt cloud deploy` installs nyxGPT and brings it up. They are allowed to
differ in *mechanism* -- PyPI vs a Homebrew tap, systemd --user vs
`brew services`, `dnf` vs `brew` -- and they are allowed to differ in the
container tier, because an EC2 Mac supports no nested virtualization and so can
host no Docker daemon at all (docs/cloud.md, "EC2 Mac targets"). They are not
allowed to differ in a **core** component, and until #4150 one of them did.

The defect this guard exists to stop. The macOS template installed
`nyxgpt-api@` and `nyxgpt-web@`, started both, and never installed Ollama --
grep found 4 `ollama` references in the Linux template and 0 in the macOS one.
`nyxgpt cloud deploy --os macos` therefore exited 0 and reported the release
deployed onto a machine whose api and web were healthy and whose chat could not
work: `GET /api/v1/models` 502'd on `[Errno 61] Connection refused` to
127.0.0.1:11434. The omission had even been written down, as part of the
*observability* caveat -- and Ollama is not observability, it is the component
that answers chat. Skipping Grafana/Loki/Tempo/GlitchTip on a Mac is a
defensible platform constraint; skipping the model backend ships a car with no
engine and invites the owner to test the headlights.

So the divergence between these two files is declared here, explicitly, with a
reason per entry, and the build fails when reality stops matching the
declaration -- in *either* direction. Adding a core service to one template and
not the other fails. Removing one fails. Adding a permitted exclusion requires
naming which of the three permitted reasons applies, and a core service may not
claim any of them (`test_no_core_service_is_declared_macos_absent`).

Matching runs over the templates with full-line comments stripped: a component
*discussed* in a comment is not a component the bootstrap installs, and these
files are heavily commented precisely about what they do not do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "cloud"
LINUX_TEMPLATE = TEMPLATE_DIR / "ec2-user-data-linux.sh.tmpl"
MACOS_TEMPLATE = TEMPLATE_DIR / "ec2-user-data-macos.sh.tmpl"

#: The only reasons a component may be in one bootstrap and not the other.
#: Anything else is a divergence to fix, not to declare.
PERMITTED_EXCLUSION_REASONS: dict[str, str] = {
    "docker-substrate": (
        "The component is Docker-container-based, and every way of running Docker on "
        "macOS works by running a Linux VM. EC2 Mac instances do not support nested "
        "virtualization, so no Docker daemon can exist on that target -- this is a "
        "platform constraint, not a revisitable scoping choice (docs/cloud.md)."
    ),
    "observability": (
        "The observability stack (Grafana/Loki/Tempo/GlitchTip) and the Compose "
        "profiles that carry it. Container-based, so it inherits the constraint "
        "above; called out separately because it is the caveat the operator is told "
        "about, and the caveat that must never be read as covering the model backend."
    ),
    "os-mechanism": (
        "The same job done a different way on each OS: a package manager, a service "
        "manager, a user lookup, a PATH drop-in. Not a component the product has or "
        "lacks."
    ),
}


@dataclass(frozen=True)
class Component:
    """One thing a bootstrap may or may not do, and where it is expected."""

    probe: str
    """Regex looked for in each template's non-comment lines."""

    linux: bool
    macos: bool

    core: bool
    """True when nyxGPT is not a working product without it on that target."""

    why: str
    """Why it is core, or -- for a declared absence -- what it is."""

    exclusion: str | None = None
    """Key into `PERMITTED_EXCLUSION_REASONS`; required when linux != macos."""


COMPONENTS: dict[str, Component] = {
    # --- Core: present in both, and the build fails if that changes ---------
    "ollama (the model backend)": Component(
        probe=r"\bollama\b",
        linux=True,
        macos=True,
        core=True,
        why=(
            "The process that answers every chat message. nyxGPT with api and web up "
            "and no Ollama is not a degraded nyxGPT, it is a non-functional one "
            "(#4150)."
        ),
    ),
    "the configured-model pull": Component(
        # Linux reaches it through `nyxgpt ops install`; macOS runs the same
        # step on its own as `nyxgpt ops required-models`, because `ops install`
        # also reconciles a Docker engine that cannot exist on an EC2 Mac.
        probe=r"ops +(install|required-models)\b",
        linux=True,
        macos=True,
        core=True,
        why=(
            "Installing Ollama is not sufficient and this is the half that was missed "
            "on Linux's side of the fence: an Ollama with an empty store answers "
            "/api/tags with [], so /api/v1/models returns 200 and an empty list and "
            "chat fails differently. The model names come from configuration, never "
            "from a literal in a template."
        ),
    ),
    "config seeding": Component(
        probe=r"config\.ini",
        linux=True,
        macos=True,
        core=True,
        why="Nothing reads a default configuration the instance does not have.",
    ),
    "the session-storage backend choice": Component(
        probe=r"ops +session-backend\b",
        linux=True,
        macos=True,
        core=True,
        why=(
            "Applied before the services start so the API's first run reads the "
            "intended backend (#3865). The default differs per OS "
            "(cloud_provision.DEFAULT_SESSION_BACKEND_BY_OS) -- the *step* does not."
        ),
    ),
    "the requested version, verified on the machine": Component(
        probe=r"NYXGPT_VERSION",
        linux=True,
        macos=True,
        core=True,
        why=(
            "A deploy that silently installs a different release than the one under "
            "test makes every result of that test meaningless (#4122)."
        ),
    ),
    "a non-root target user": Component(
        probe=r"NYXGPT_TARGET_USER",
        linux=True,
        macos=True,
        core=True,
        why="Neither bootstrap runs the stack as root.",
    ),
    # --- Declared divergence: permitted, with the reason named --------------
    "the Docker engine": Component(
        probe=r"\bdocker\b",
        linux=True,
        macos=False,
        core=False,
        why="`ops install` shells out to it for the nyxgpt-cassandra container.",
        exclusion="docker-substrate",
    ),
    "the observability opt-out flag": Component(
        probe=r"--skip-observability",
        linux=True,
        macos=False,
        core=False,
        why=(
            "Linux passes it to `ops install`; the macOS bootstrap does not run that "
            "command at all, so it has nothing to pass the flag to."
        ),
        exclusion="observability",
    ),
    "node/npm for the web bundle build": Component(
        probe=r"\bnpm\b",
        linux=True,
        macos=False,
        core=False,
        why=(
            "`ops install` builds the web bundle from source on Linux. The macOS path "
            "installs the prebuilt `nyxgpt-web` keg instead -- same web UI, no Node "
            "toolchain needed on the instance."
        ),
        exclusion="os-mechanism",
    ),
    "the systemd --user service manager": Component(
        probe=r"systemctl|loginctl",
        linux=True,
        macos=False,
        core=False,
        why="macOS uses launchd, driven by `brew services`.",
        exclusion="os-mechanism",
    ),
    "the login-shell PATH drop-in": Component(
        probe=r"profile\.d/nyxgpt",
        linux=True,
        macos=False,
        core=False,
        why=(
            "On macOS the `nyxgpt-api` keg symlinks the CLI into Homebrew's own `bin`, "
            "which is already on the PATH -- so there is nothing for a drop-in to add "
            "(#3993, and the table in docs/cloud.md)."
        ),
        exclusion="os-mechanism",
    ),
    "Homebrew and its tap trust": Component(
        probe=r"tap-trust",
        linux=False,
        macos=True,
        core=False,
        why="The macOS artifact channel is the remote Homebrew tap (#3752, #3770).",
        exclusion="os-mechanism",
    ),
    "pip, for the PyPI artifact": Component(
        probe=r"\bpip\b",
        linux=True,
        macos=False,
        core=False,
        why="The Linux artifact channel is PyPI; macOS installs kegs.",
        exclusion="os-mechanism",
    ),
}


def _executable_lines(path: Path) -> str:
    """Return `path` with full-line comments and the shebang removed.

    A component named only in a comment is not one the bootstrap installs, and
    these templates comment at length about what they deliberately do not do --
    the macOS one discusses Docker, Cassandra and the observability stack in
    prose while installing none of them. Matching the raw text would read those
    paragraphs as parity.
    """
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(kept)


@pytest.fixture(scope="module")
def templates() -> dict[str, str]:
    return {"linux": _executable_lines(LINUX_TEMPLATE), "macos": _executable_lines(MACOS_TEMPLATE)}


@pytest.mark.parametrize("name", sorted(COMPONENTS))
@pytest.mark.parametrize("os_family", ["linux", "macos"])
def test_declared_component_presence_matches_reality(
    name: str, os_family: str, templates: dict[str, str]
) -> None:
    """Each template contains exactly the components declared for it.

    Fails in both directions on purpose. A core component dropped from one
    bootstrap is the #4150 defect; a core component that quietly appears in only
    one is the same defect waiting to be noticed on the other substrate.
    """
    component = COMPONENTS[name]
    expected = component.linux if os_family == "linux" else component.macos
    found = re.search(component.probe, templates[os_family]) is not None

    if expected and not found:
        pytest.fail(
            f"{os_family} bootstrap no longer provisions {name!r} "
            f"(no match for /{component.probe}/ outside comments).\n"
            f"Why it is declared here: {component.why}\n"
            "If this removal is intended, move the component to a declared exclusion "
            "with a reason from PERMITTED_EXCLUSION_REASONS -- and note that a core "
            "component may not claim one."
        )
    if found and not expected:
        pytest.fail(
            f"{os_family} bootstrap now provisions {name!r}, which is declared absent "
            f"there (reason: {component.exclusion}).\n"
            f"Declared because: {component.why}\n"
            "If the platform constraint has changed, update this declaration in the "
            "same change that updates the template -- and docs/cloud.md with it."
        )


@pytest.mark.parametrize("name", sorted(COMPONENTS))
def test_every_divergence_names_a_permitted_reason(name: str) -> None:
    """A component present in one bootstrap and not the other must say why."""
    component = COMPONENTS[name]
    if component.linux == component.macos:
        assert (
            component.exclusion is None
        ), f"{name!r} is present in both bootstraps, so it needs no exclusion reason"
        return
    assert component.exclusion in PERMITTED_EXCLUSION_REASONS, (
        f"{name!r} differs between the two bootstraps but names "
        f"{component.exclusion!r}, which is not a permitted reason. "
        f"Permitted: {sorted(PERMITTED_EXCLUSION_REASONS)}."
    )


@pytest.mark.parametrize("name", sorted(COMPONENTS))
def test_no_core_service_is_declared_macos_absent(name: str) -> None:
    """A core component may not be excluded from a target for any reason.

    This is the assertion that would have caught #4150 at review time rather
    than on a Dedicated Host with a non-refundable 24-hour minimum. The three
    permitted reasons are all about the *container tier* and about *mechanism*;
    none of them can justify shipping a target with no model backend, and the
    test says so rather than leaving it to a reviewer's judgement.
    """
    component = COMPONENTS[name]
    if not component.core:
        return
    assert component.linux and component.macos, (
        f"{name!r} is declared core but is missing from a bootstrap. Core means the "
        "product does not work on that target without it, so there is no permitted "
        f"exclusion for it. Why it is core: {component.why}"
    )


def test_ollama_is_declared_core_in_both_bootstraps() -> None:
    """Named explicitly, because this is the component the project got wrong.

    The generic tests above would pass if a future edit reclassified Ollama as
    non-core and excluded it under `observability` -- which is, almost exactly,
    the reasoning that produced #4150: the deploy's own closing output filed the
    missing model backend under the observability caveat. Pin it.
    """
    ollama = COMPONENTS["ollama (the model backend)"]
    assert ollama.core, "Ollama is the model backend; it is not an optional tier"
    assert ollama.linux and ollama.macos
    assert ollama.exclusion is None


def test_macos_bootstrap_starts_ollama_and_pulls_before_starting_the_api(
    templates: dict[str, str],
) -> None:
    """Order matters, not just presence (#4150).

    All three failure modes the owner listed are orderings or omissions, not
    missing mentions: installed but not started leaves :11434 refusing
    connections; started but not pulled leaves /api/v1/models answering with an
    empty list; and pulling after the API is up means the first chat message
    races a multi-hundred-megabyte download.
    """
    macos = templates["macos"]

    def position(fragment: str) -> int:
        at = macos.find(fragment)
        if at < 0:
            pytest.fail(
                f"the macOS bootstrap does not contain {fragment!r} outside its comments. "
                "Without it there is no ordering to check, and the step it names is the "
                "one #4150 was filed about."
            )
        return at

    install_at = position("install ollama")
    start_at = position("services start ollama")
    pull_at = position("ops required-models")
    api_start_at = position('services start "$NYXGPT_BREW_API_FORMULA"')

    assert install_at < start_at, "ollama must be installed before it is started"
    assert start_at < pull_at, (
        "the pull goes over HTTP to the running server, so the service must be started "
        "first -- that is also what keeps the pull and the serve from disagreeing about "
        "which model store they use"
    )
    assert pull_at < api_start_at, (
        "the models must be in place before the API starts serving, or chat 502s until "
        "a download nobody is watching finishes"
    )


def test_macos_bootstrap_never_hardcodes_a_model_name(templates: dict[str, str]) -> None:
    """The model to pull comes from configuration, not from this script.

    `[nyxgpt] default_model` currently resolves to `qwen3.5:0.8b`, and a literal
    here is how that silently stops matching the product the next time the
    shipped default changes -- the failure mode `shipped_default_model()`'s own
    docstring was written about (one literal in nine files, three of them
    updated).
    """
    from nyxgpt.config import shipped_default_model

    macos = templates["macos"]
    model = shipped_default_model()
    assert model not in macos, (
        f"the macOS bootstrap names the model {model!r} literally. Let "
        "`nyxgpt ops required-models` read it from configuration instead."
    )
    # The embedding model too -- `required_models` pulls both, and RAG is a
    # per-session toggle, so "RAG is off" is not a reason to skip it.
    assert "nomic-embed-text" not in macos

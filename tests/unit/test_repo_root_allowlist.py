"""Guard test for #3621: REPO_ROOT-relative runtime lookups.

`nyxgpt ops install`/`up` reconcile the local stack from runtime data
(Compose file, config/provisioning templates, launchd/systemd unit
templates, helper scripts) shipped inside the installed package under
`nyxgpt.resources` and synced to a fixed, ops-managed location under
`~/.nyxGPT` by `nyxgpt.ops._sync_packaged_resources` -- not resolved
relative to `REPO_ROOT` (`Path(__file__).resolve().parents[2]`), since an
installed, non-editable build has no repo checkout alongside it for a
REPO_ROOT-relative lookup to find.

`REPO_ROOT` itself still exists in a few modules, deliberately scoped to
operations that remain inherently repo-checkout-dependent regardless of
Python packaging: building distributable artifacts FROM source (Homebrew
tap tarball vendoring, the self-contained Linux venv build, Docker image
builds for Terraform/Kubernetes local deploy), Terraform/Kubernetes local
deploy itself (terraform/*.tf and k8s/*.yaml are files on disk, not
importable package data), the web/ npm project (its own build/packaging
concern, not shipped as Python package data), and dev-checkout-only
doctor/version diagnostics that no-op cleanly when REPO_ROOT doesn't exist.

This test parses each module with `ast` (so comments/docstrings that
merely mention "REPO_ROOT" in prose never count) and asserts every actual
code reference to the `REPO_ROOT` name is one of the specific,
already-reviewed lines below. A new REPO_ROOT-relative lookup added
anywhere else fails this test -- it needs the same importlib.resources
treatment #3621 gave the rest of the install path (see
`nyxgpt.ops._sync_packaged_resources`), not a silent bypass. Entries here
that stop matching (e.g. the line was deleted) also fail the test, so the
allowlist can't silently rot out of sync with the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# relpath -> set of exact (leading/trailing-whitespace-stripped) source
# lines that may reference REPO_ROOT. Reformatting one of these lines (e.g.
# a black line-wrap change) requires updating its entry here -- that's the
# point: every REPO_ROOT reference is a reviewed, deliberate exception, not
# an accident this test should quietly tolerate drifting.
_ALLOWLIST: dict[str, set[str]] = {
    "src/nyxgpt/ops.py": {
        # _read_project_version / _stale_venv_doctor_issues: reading
        # pyproject.toml directly (not via importlib.metadata) so a version
        # bump is picked up immediately in a dev checkout without a
        # reinstall, and so the stale-venv doctor check compares against
        # the checkout's actual declared deps -- both dev-checkout-only
        # diagnostics/tooling that no-op cleanly (or are simply irrelevant)
        # for an installed package.
        'pyproject = REPO_ROOT / "pyproject.toml"',
        'pyproject_path = REPO_ROOT / "pyproject.toml"',
        # _has_vendorable_source / _native_service_version: the *detection*
        # of whether a checkout is present at all (#3759). These are the
        # inverse of a REPO_ROOT-relative lookup -- they exist so the
        # native api/web installers stop assuming one and fall back to the
        # published release artifact (`_service_source_tarball`) when
        # REPO_ROOT resolves inside an installed venv instead of a checkout.
        'return (REPO_ROOT / "web" / "package.json").is_file()',
        'return (REPO_ROOT / "src" / "nyxgpt").is_dir() and (REPO_ROOT / "pyproject.toml").is_file()',
        'if (REPO_ROOT / "pyproject.toml").is_file():',
        # `ops._create_dist_tarball`: the builder itself moved to the
        # stdlib-only `release_tarball` module (#3741), but every local
        # install path here calls it expecting "vendor from the checkout
        # ops.REPO_ROOT points at", so this wrapper resolves that default.
        "tap_dir, name, version, REPO_ROOT if source_root is None else source_root",
        # Docker image build fingerprinting for the nyxgpt-api image --
        # `docker build`'s context and source-change fingerprint are
        # necessarily a repo checkout's files.
        'REPO_ROOT / "src" / "nyxgpt",',
        'REPO_ROOT / "pyproject.toml",',
        'REPO_ROOT / "example.config.ini",',
        'REPO_ROOT / "docker" / "entrypoint.sh",',
        # Homebrew formula templates -- also part of the tap-vendoring
        # build-from-source flow above.
        'template = REPO_ROOT / "homebrew" / "nyxgpt-api.rb"',
        'template = REPO_ROOT / "homebrew" / "nyxgpt-web.rb"',
        # Self-contained Linux venv build (`ops package --linux`): builds
        # an installable artifact from the checkout's own example.config.ini.
        'example_config = REPO_ROOT / "example.config.ini"',
        # _ensure_web_deps / doctor()'s node_modules resolution check: the
        # web/ npm project is its own build/packaging concern (Homebrew
        # formula), not shipped as Python package data -- both no-op when
        # web/ doesn't exist (installed-package case). Both functions'
        # lookups happen to normalize to this identical stripped line.
        'web_dir = REPO_ROOT / "web"',
        # _ensure_mcp_deps: root node_modules for Claude Code MCP servers
        # -- repo-local dev tooling, not a runtime dependency of the
        # installed product.
        "root_dir = REPO_ROOT",
        # Terraform local deploy: terraform/*.tf files are on disk, not
        # importable package data -- `terraform apply` inherently needs a
        # repo checkout regardless of how nyxGPT's Python package ships.
        'TERRAFORM_DIR = REPO_ROOT / "terraform"',
        "text = re.sub(r'repo_path\\s*=\\s*\".*\"', lambda _m: f'repo_path    = \"{REPO_ROOT}\"', text)",
        "REPO_ROOT,",
        'REPO_ROOT / "web",',
        'fingerprint_paths=[REPO_ROOT / "web"],',
        # _dev_checkout_root and the two messages that explain its answer
        # (#3789): dev mode is checkout-only by definition -- it installs
        # the api editable from the working tree -- so this is the same
        # "is a checkout present at all" detection as
        # `_has_vendorable_source` above, plus the errors that name the
        # path that turned out not to be one. The artifact path (the
        # repo-less default) is unaffected by all three.
        "return REPO_ROOT",
        'f"nyxgpt is running from an installed package ({REPO_ROOT} has no "',
        'f"installed package ({REPO_ROOT} has no pyproject.toml/src/nyxgpt/web).\\n"',
        # Kubernetes local deploy: k8s/*.yaml manifests, same reasoning.
        'K8S_DIR = REPO_ROOT / "k8s"',
        "context: Path = REPO_ROOT,",
        'context=REPO_ROOT / "web",',
    },
    "src/nyxgpt/release_tarball.py": {
        # Homebrew tap tarball vendoring (`ops package`) -- building a
        # distributable artifact FROM a repo checkout; sibling issue B
        # ("publish install artifacts") retires this REPO_ROOT dependency
        # by publishing pre-built artifacts instead. Since #3737 the tree it
        # vendors is a parameter (`source_root`) and REPO_ROOT is only its
        # default, so the four vendoring call sites this replaced are no
        # longer REPO_ROOT-relative at all. Split out of ops.py by #3741 so
        # release tooling can import the builder without ops.py's
        # third-party dependencies -- the REPO_ROOT rationale is unchanged.
        "src_root = REPO_ROOT if source_root is None else Path(source_root)",
    },
    "src/nyxgpt/verify.py": {
        # `nyxgpt ops verify`'s Playwright dashboard screenshot harness --
        # part of the optional `verify` extra (CI acceptance verification),
        # not the install/up reconciliation path.
        'DASHBOARDS_DIR = REPO_ROOT / "docker" / "grafana" / "dashboards"',
    },
    "src/nyxgpt/canary.py": {
        # Kubernetes canary image build context -- same reasoning as the
        # Terraform/Kubernetes docker-build entries in ops.py above.
        'build_context=ops_module.REPO_ROOT / "web",',
        'build_fingerprint_paths=[ops_module.REPO_ROOT / "web"],',
    },
}


def _repo_root_usage_lines(path: Path) -> dict[int, str]:
    """Return {lineno: stripped_source_line} for every AST-level reference
    to the name/attribute `REPO_ROOT` in `path`, excluding the line(s) that
    define it (`REPO_ROOT = Path(__file__)...`)."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    lines = src.splitlines()

    definition_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "REPO_ROOT":
                    definition_lines.add(node.lineno)

    usage: dict[int, str] = {}
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or lineno in definition_lines:
            continue
        is_repo_root_name = isinstance(node, ast.Name) and node.id == "REPO_ROOT"
        is_repo_root_attr = isinstance(node, ast.Attribute) and node.attr == "REPO_ROOT"
        if is_repo_root_name or is_repo_root_attr:
            usage[lineno] = lines[lineno - 1].strip()
    return usage


@pytest.mark.unit
@pytest.mark.parametrize("relpath", sorted(_ALLOWLIST))
def test_repo_root_usage_matches_allowlist(relpath: str) -> None:
    path = _REPO_ROOT / relpath
    usage = _repo_root_usage_lines(path)
    allowed = _ALLOWLIST[relpath]

    unrecognized = [
        f"{relpath}:{lineno}: {text!r}" for lineno, text in usage.items() if text not in allowed
    ]
    assert not unrecognized, (
        "New REPO_ROOT-relative code found outside the documented allowlist "
        "(#3621). If this is a lookup `nyxgpt ops install`/`up` needs, give it "
        "the same importlib.resources treatment as "
        "nyxgpt.ops._sync_packaged_resources; if it's a genuinely "
        "repo-checkout-dependent operation (building an artifact from source, "
        "Terraform/Kubernetes local deploy, the web/ npm project, a "
        "dev-checkout-only diagnostic), add it to this test's allowlist with a "
        "one-line rationale:\n" + "\n".join(unrecognized)
    )

    found_texts = set(usage.values())
    stale = sorted(allowed - found_texts)
    assert not stale, (
        "Allowlist entries no longer found in the source (stale -- update or "
        f"remove them from {__file__}):\n" + "\n".join(stale)
    )


@pytest.mark.unit
def test_self_heal_has_no_repo_root_usage() -> None:
    """#3621 retired self_heal.py's REPO_ROOT-based compose-file resolution
    entirely (module-path check + config.ini `[paths] compose_file`
    fallback) in favor of a fixed `~/.nyxGPT/docker-compose.yml` location --
    it should never come back."""
    path = _REPO_ROOT / "src/nyxgpt/self_heal.py"
    assert _repo_root_usage_lines(path) == {}

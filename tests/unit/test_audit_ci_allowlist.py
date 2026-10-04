"""Guard test for #4150: the shape of `web/audit-ci.jsonc`'s allowlist.

`security-scan` is a [required] check, so `web/audit-ci.jsonc` is the one
file in this tree that can turn a high-severity npm advisory into a green
build. That makes its *shape* load-bearing in a way its contents are not,
and this test guards the shape only -- it never asserts which advisories
are allowlisted, because that set legitimately changes every time npm
publishes one.

WHY THIS EXISTS. #4150's round hit GHSA-vfj7-8cjw-p6xm (`braces`,
stack-exhaustion DoS via deeply nested glob patterns). Unusually, the
advisory covers `<= 3.0.3` while `3.0.3` is the newest version `braces`
has ever published, and `first_patched_version` is null: there is no
upgrade, `npm audit fix` is a no-op, and `npm audit fix --force` proposes
semver-major *downgrades* of `eslint-config-next` and
`@ducanh2912/next-pwa`. The allowlist was the only action available.

Accepting an advisory on that reasoning is sound, but only for the paths
the reasoning was checked against -- here, two build/lint-time paths whose
glob patterns are authored in this repository and never come from a
request. The dangerous move is the bare `"GHSA-vfj7-8cjw-p6xm"` entry,
which reads identically in a diff and *also* accepts the advisory on paths
nobody triaged, including a future runtime dependency where
"build-time only" would simply be false. Path-scoping is what keeps the
gate alive for the one part of the risk that is not yet understood: a
third path appearing. Verified in both directions when the entries were
added -- the full allowlist exits 0 ("Passed npm security audit"), and the
same config with one path removed exits 1 naming exactly the removed path,
so the scoping is live rather than decorative.

Hence the three assertions below: the gate's threshold cannot be quietly
lowered, an entry cannot be blanket-scoped, and an entry cannot be added
without its reasoning recorded in the file beside it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

AUDIT_CI_CONFIG = Path(__file__).resolve().parents[2] / "web" / "audit-ci.jsonc"

# audit-ci's path-scoped entry form, exactly as the tool prints it for
# copy-paste: `<advisory-id>|<pkg>[>...]>...`. Anything without the `|` is
# accepting the finding by advisory ID or by module name, i.e. everywhere.
_PATH_SCOPED = re.compile(r"^(?P<advisory>[A-Za-z0-9-]+)\|(?P<path>\S+>\S+)$")


def _load_config() -> dict:
    """Parse the JSONC config, dropping whole-line `//` comments.

    Only full-line comments are stripped, deliberately: a naive strip of
    everything after `//` would also eat the `https://` URLs this file
    carries in its justifications. `test_the_config_uses_only_full_line_comments`
    asserts that assumption instead of trusting it.
    """
    raw = AUDIT_CI_CONFIG.read_text()
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))


def test_the_config_uses_only_full_line_comments() -> None:
    """`_load_config`'s comment strip is valid for this file.

    If someone adds a trailing `// ...` comment after a value, the parse
    above stops being correct and this test says so, rather than the
    allowlist tests failing with a confusing JSON error.
    """
    offenders = [
        (number, line)
        for number, line in enumerate(AUDIT_CI_CONFIG.read_text().splitlines(), start=1)
        if "//" in line and not line.lstrip().startswith("//")
    ]
    assert not offenders, (
        "web/audit-ci.jsonc has a trailing `//` comment, which this test's "
        f"full-line comment strip cannot parse: {offenders}. Move it to its "
        "own line, or teach _load_config a real JSONC parser."
    )


def test_the_gate_still_fails_the_build_on_high_severity() -> None:
    """`"high": true` is what makes `security-scan` a gate at all.

    Flipping it to false would turn every current and future high/critical
    advisory green at once -- a far larger change than any single allowlist
    entry, and one that looks smaller in a diff.
    """
    assert _load_config().get("high") is True, (
        "web/audit-ci.jsonc no longer fails on high/critical findings. The "
        '`security-scan` required check is inert without `"high": true`.'
    )


def test_every_allowlist_entry_is_scoped_to_a_dependency_path() -> None:
    """No bare advisory IDs or module names -- see this module's docstring.

    A bare entry accepts the advisory on paths that were never triaged,
    including ones that do not exist yet. The per-path form keeps the gate
    red for a third path while staying green for the two that were
    reasoned about.
    """
    unscoped = [entry for entry in _load_config()["allowlist"] if not _PATH_SCOPED.match(entry)]
    assert not unscoped, (
        f"web/audit-ci.jsonc allowlists {unscoped} without a dependency path. "
        "Use audit-ci's `<advisory-id>|<a>b>c>` form (the tool prints it ready "
        "to paste) so the advisory stays blocking on paths nobody has "
        "triaged -- a runtime dependency included."
    )


def test_every_allowlisted_advisory_has_its_reasoning_in_the_file() -> None:
    """An entry is only as good as the reason recorded next to it.

    This file is JSONC specifically so each acceptance can carry its
    justification (`security/README.md` documents that). An entry whose
    advisory ID appears nowhere in the comments is an acceptance with no
    recorded reasoning, which the next reader cannot re-triage or retire.
    """
    config = _load_config()
    comments = "\n".join(
        line for line in AUDIT_CI_CONFIG.read_text().splitlines() if line.lstrip().startswith("//")
    )
    undocumented = sorted(
        {
            match["advisory"]
            for entry in config["allowlist"]
            if (match := _PATH_SCOPED.match(entry)) and match["advisory"] not in comments
        }
    )
    assert not undocumented, (
        f"web/audit-ci.jsonc allowlists {undocumented} with no justification "
        "comment mentioning it. Record why the finding is accepted and what "
        "would retire the entry, beside the entry."
    )


@pytest.mark.parametrize(
    "entry",
    [
        "GHSA-vfj7-8cjw-p6xm",
        "braces",
        "GHSA-vfj7-8cjw-p6xm|braces",
    ],
)
def test_the_scoping_guard_rejects_the_entries_it_was_written_for(entry: str) -> None:
    """The pattern matches the forms it exists to catch (#3753).

    A guard that has never been observed to reject anything is not
    evidence. These are the three shapes an under-scoped acceptance
    actually takes: the bare advisory ID, a bare module name, and an
    advisory scoped to a single package rather than a dependency path.
    """
    assert not _PATH_SCOPED.match(entry)


def test_the_scoping_guard_accepts_a_real_path_scoped_entry() -> None:
    """...and does not reject the correct form, which would be the inverse bug."""
    match = _PATH_SCOPED.match(
        "GHSA-vfj7-8cjw-p6xm|eslint-config-next>@next/eslint-plugin-next>fast-glob>micromatch>braces"
    )
    assert match is not None
    assert match["advisory"] == "GHSA-vfj7-8cjw-p6xm"
    assert match["path"].endswith(">braces")

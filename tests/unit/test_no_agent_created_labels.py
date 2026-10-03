"""Labels are the owner's: nothing here creates, edits or deletes one (#4134).

CLAUDE.md §Tooling has said so for months ("Do NOT create labels (use
existing labels only) ... If a label/milestone/field option is needed, ASK the
user first"), and automation created three sets anyway:

* `usage-limit-retry` — `gh label create` in `developer_auto_implement.yml`
  and `claude-code-review.yml`. It broke the one-label invariant on every
  issue it touched, which had to be worked around by excluding it from
  `real_label_names` (`WORKFLOW_CONTROL_LABELS_JSON`), and it still deadlocked
  PR submission once (#3360). Retired: the retry queue is now marker comments
  on the release tracking issue (`scripts/agents/lib/usage_limit_retry.py`).
* `dependencies` / `javascript` — GitHub's Dependabot defaults, applied and
  RECREATED because the repository had no `.github/dependabot.yml`
  (PR #4124, 2026-10-01). Turned off with `labels: []`.
* `Support` — owner-sanctioned, so the label stays. What did not stay is
  `gh label create --force` in `support_intake_guard.yml` and
  `admin_ensure_support_label.yml`: `--force` OVERWRITES the colour and
  description of an existing label, so a daily cron was rewriting the owner's
  own label every day.

A fourth went with them: `admin_label_rename.yml` was an owner-triggered
utility that PATCHed `repos/{repo}/labels/{name}` to rename a label. Nothing
about it was abusive -- but it is a workflow that rewrites the label registry
under an agent token, the rule is categorical, and renaming a label takes two
clicks in the GitHub UI. It is deleted rather than exempted; this test is what
would have failed had it stayed.

The rule was written down and drifted anyway, three times over. What protects
it is a check that runs. This is that check, and it runs in `ci-tests.yml`
with the rest of pytest -- unlike `check_no_hardcoded_release_branch.sh`,
whose equivalent rule is enforced by a script that no workflow invokes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / "scripts"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

#: Every spelling of "write the label registry itself".
#:
#: `gh label create|edit|delete` is the CLI form. The REST forms are the
#: repository-level `/labels` collection (POST creates, PATCH edits, DELETE
#: removes) and Octokit's `issues.createLabel` / `updateLabel` / `deleteLabel`.
_LABEL_WRITE = re.compile(
    r"gh\s+label\s+(?:create|edit|delete)\b"
    r"|(?:-X|--method)\s+(?:POST|PATCH|PUT|DELETE)[^\n]*?/labels\b"
    r"|gh\s+api[^\n]*?repos/[^\n]*?/labels\b[^\n]*?-[fF]\s"
    r"|issues\.(?:create|update|delete)Label\s*\("
)

#: The REST endpoint that *silently creates* a label it was asked to add to an
#: issue. `gh issue edit --add-label` refuses instead, which is why every
#: label application in this repository goes through `gh`.
_REST_ADD_LABELS = re.compile(
    r"(?:-X|--method)\s+(?:POST|PUT)[^\n]*?issues/[^\n]*?/labels\b"
    r"|gh\s+api[^\n]*?issues/[^\n]*?/labels\b[^\n]*?-[fF]\s"
    r"|issues\.(?:add|set)Labels\s*\("
)

#: Labels the OWNER created. A label applied by automation must be one of
#: these: anything else is a label this repository would be inventing.
#:
#: `Escalation` and `Agent` are the two the owner added on 2026-10-03 (#4134);
#: `Support` on #3745. The rest are the long-standing issue-type labels.
OWNER_LABELS = frozenset(
    {
        "Acceptance Failure",
        "Agent",
        "Documentation",
        "Escalation",
        "Feature",
        "Improvement",
        "Production Defect",
        "Release Management",
        "Support",
    }
)

#: `--add-label "<literal>"` / `--remove-label "<literal>"`, capturing the
#: name only when it is a literal. A shell variable (`"$label"`,
#: `"$PREFIX_LABEL"`) is a name read off an issue that already carries it, so
#: it cannot be an invented one and is not checked here.
_LITERAL_LABEL_ARG = re.compile(r"--(?:add|remove)-label\s+[\"']?([A-Za-z][A-Za-z0-9 _-]*)[\"']?")

#: Prose, not a call: an error message naming the manual fix, a comment, a
#: line that is the continuation of a quoted string above it.
_NOT_A_CALL = re.compile(r"^\s*(echo|printf)\b|^\s*[\"']|::error::|::warning::|Manual fix:")


def _sources() -> list[Path]:
    files = [p for p in SCRIPTS.rglob("*.sh") if "__pycache__" not in str(p)]
    files += [p for p in SCRIPTS.rglob("*.py") if "__pycache__" not in str(p)]
    files += sorted(WORKFLOWS.glob("*.yml"))
    return files


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines that are code: comments and Python docstrings are dropped.

    History in a comment is not a call -- several of the files below
    deliberately *describe* the `gh label create` they no longer make, and a
    guard that cannot tell the two apart would forbid explaining itself.
    Python docstrings are prose for the same reason, and this project's are
    long: `usage_limit_retry.py`'s own module docstring recounts the label it
    replaced.
    """
    out = []
    in_docstring = False
    is_python = path.suffix == ".py"
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if is_python:
            # Counting the fences rather than matching a start/end pair: a
            # one-line `"""..."""` docstring opens and closes on the same
            # line and must not leave the state machine inverted.
            fences = stripped.count('"""') + stripped.count("'''")
            if in_docstring:
                if fences % 2 == 1:
                    in_docstring = False
                continue
            if fences % 2 == 1:
                in_docstring = True
                continue
            if fences and stripped.startswith(('"""', "'''", 'r"""', "r'''")):
                continue
        if stripped.startswith(("#", "//", "*")):
            continue
        out.append((number, line))
    return out


class TestNothingWritesTheLabelRegistry:
    def test_no_label_create_edit_or_delete(self):
        offenders = []
        for path in _sources():
            for number, line in _code_lines(path):
                if _NOT_A_CALL.search(line):
                    continue
                if _LABEL_WRITE.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
        assert not offenders, (
            "these create, edit or delete a label, which only the owner may do "
            f"(CLAUDE.md §Tooling, #4134): {offenders}. If a label is needed, ask "
            "the owner for it and assume it exists -- fail loudly if it does not, "
            "as admin_ensure_support_label.yml does."
        )

    def test_no_rest_label_add_that_would_create_one(self):
        """GitHub's REST "add labels to an issue" endpoint silently CREATES a
        missing label; `gh issue edit --add-label` refuses. Using REST would
        therefore reintroduce agent-created labels through a side door, with no
        `gh label create` anywhere to find."""
        offenders = []
        for path in _sources():
            for number, line in _code_lines(path):
                if _NOT_A_CALL.search(line):
                    continue
                if _REST_ADD_LABELS.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
        assert not offenders, (
            "these add labels over REST/Octokit, which silently creates any label "
            f"that does not exist: {offenders}. Use `gh issue edit --add-label` / "
            "`gh pr edit --add-label`, which refuse instead."
        )

    def test_every_literal_label_applied_is_an_owner_label(self):
        offenders = []
        for path in _sources():
            for number, line in _code_lines(path):
                if _NOT_A_CALL.search(line):
                    continue
                for name in _LITERAL_LABEL_ARG.findall(line):
                    if name not in OWNER_LABELS:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{number}: label '{name}'"
                        )
        assert not offenders, (
            f"these apply a label that is not one of the owner's: {offenders}. "
            f"Known owner labels: {sorted(OWNER_LABELS)}. Adding to that set means "
            "the owner created the label first."
        )

    def test_the_retired_control_label_is_gone_everywhere(self):
        """`usage-limit-retry` as a LABEL, and the exemption list it needed.

        The exemption is the part that matters: while `real_label_names` knew
        about a second class of label, the next piece of automation to invent
        one had somewhere to hide it.

        Matched narrowly -- on the label verbs and the label-filtered REST
        query, not on the string. The marker comments that replaced the label
        share its name on purpose (`<!-- usage-limit-retry: … -->`), so a
        substring match would forbid the fix along with the defect.
        """
        label_use = re.compile(
            r"--(?:add|remove)-label[^\n]*usage-limit-retry"
            r"|labels=usage-limit-retry"
            r"|gh\s+label\s+\w+\s+[\"']?usage-limit-retry"
            r"|WORKFLOW_CONTROL_LABELS_JSON\s*=|\$\{?WORKFLOW_CONTROL_LABELS_JSON"
        )
        offenders = []
        for path in _sources():
            for number, line in _code_lines(path):
                if label_use.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
        assert not offenders, f"the retired usage-limit-retry label is still live here: {offenders}"


class TestDependabotAppliesNoLabels:
    def test_the_config_exists(self):
        assert DEPENDABOT.is_file(), (
            "without .github/dependabot.yml GitHub applies its own default labels "
            "(`dependencies`, `javascript`) to Dependabot PRs and recreates them "
            "whenever they are deleted (PR #4124)"
        )

    def test_every_update_config_sets_labels_to_empty(self):
        config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
        updates = config.get("updates") or []
        assert updates, "dependabot.yml declares no update configs"
        for entry in updates:
            ecosystem = entry.get("package-ecosystem")
            assert entry.get("labels") == [], (
                f"the '{ecosystem}' update config must set `labels: []` -- omitting it "
                "restores GitHub's default labels, which is the whole defect"
            )


class TestHygieneTreatsEveryLabelAsReal:
    def test_the_one_definition_of_a_real_label_excludes_nothing(self):
        """`Escalation` needs no special case in hygiene, and that is the point.

        Hygiene adds `Feature` only to an issue with NO label, counting labels
        rather than recognising names (#3390/#3413/#3415). A name-blind count
        is what guarantees it will never add `Feature` next to `Escalation`,
        and a name-blind count is only possible while nothing is exempt.
        """
        lib = (SCRIPTS / "agents" / "lib" / "gh_project.sh").read_text(encoding="utf-8")
        body = lib[lib.index("real_label_names() {") :]
        body = body[: body.index("\n}\n")]
        assert "jq -r '.[].name'" in body, (
            "real_label_names must report every label name, with no exemption list"
        )
        hygiene = (SCRIPTS / "agents" / "ensure_issue_hygiene.sh").read_text(encoding="utf-8")
        assert "real_label_names" in hygiene

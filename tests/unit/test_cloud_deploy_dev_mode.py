"""Unit tests for `nyxgpt cloud deploy --dev` -- the working-tree cloud path (#3950).

The question this file answers is the one the issue was filed about: *when an
operator asks to deploy their working tree to a cloud target, do they get it?*
Three things have to hold, and each of them is a way the feature could pass
inspection while being useless:

1. The build source actually changes. A `--dev` that rendered the same
   `pip install nyxgpt==<version>` would deploy a published release under a
   flag that says otherwise -- a silent wrong answer, which is worse than no
   flag at all.
2. It refuses without a checkout, loudly and before AWS is touched. This is
   the acceptance criterion the issue is most explicit about: an operator who
   believes they are testing their tree must never be handed an artifact
   install instead.
3. The default path is unchanged. `--dev` is opt-in and checkout-only (D-009),
   so the repo-less guarantee (#3504) has to hold for every deploy that does
   not ask for it -- including from the admin API, which has no tree to ship.

Nothing here opens an SSH connection or talks to AWS; `subprocess` is a
recorder and the "working tree" is a real git repository in `tmp_path`, which
is what makes the file-selection assertions mean something.
"""

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from nyxgpt import cloud_deploy, cloud_infra, ops
from nyxgpt.cloud import CloudCommandError


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the module reads or writes at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_deploy, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", cloud_dir / "deploy.json")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_HISTORY_FILE", cloud_dir / "history.jsonl")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_STATE_FILE", cloud_dir / "tunnel.json")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    return cloud_dir


@pytest.fixture
def checkout(tmp_path):
    """A real git working tree shaped like this repository's.

    A real `git init` rather than a stub: `working_tree_files` asks git what
    the working tree contains, so a fake would test the mock and not the
    selection rules that matter (tracked + new-but-not-ignored, minus
    ignored, minus deleted).
    """
    root = tmp_path / "checkout"
    (root / "src" / "nyxgpt").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='nyxgpt'\n", encoding="utf-8")
    (root / "src" / "nyxgpt" / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (root / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text("web/node_modules/\n*.pyc\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"],
    ):
        subprocess.run([*argv[:1], "-C", str(root), *argv[1:]], check=True, capture_output=True)
    return root


@pytest.fixture
def dev_checkout(checkout, monkeypatch):
    """Make `--dev` resolve to `checkout`, as an editable install would."""
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: checkout)
    return checkout


def _args(**overrides) -> argparse.Namespace:
    base = {
        "host": None,
        "ssh_user": None,
        "identity_file": None,
        "version": "3.0.0",
        "dev": False,
        "os_family": "auto",
        "skip_observability": False,
        "no_tunnel": False,
        "health_timeout": None,
        "ssh_timeout": None,
        "status": False,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- The refusal (acceptance criterion 3) --------------------------------


def test_dev_without_a_checkout_is_refused_before_anything_is_provisioned(monkeypatch):
    """No tree, no deploy -- and the message says which three paths were missing.

    The failure mode this exists to prevent is not an exception: it is a
    *successful* deploy of a published release to an operator who is trying
    to test an unreleased change, and who has no way to tell from the outside.
    """
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: None)
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_deploy.resolve_plan(_args(dev=True))
    message = str(excinfo.value)
    assert "--dev" in message
    assert "pyproject.toml" in message and "src/nyxgpt/" in message and "web/" in message
    # It names both ways forward, the same way `ops.install`'s refusal does.
    assert "from a clone" in message
    assert "drop --dev" in message


def test_the_refusal_uses_the_same_definition_of_a_checkout_as_the_local_install(monkeypatch):
    """`cloud deploy --dev` and `up --dev` must never disagree about a tree.

    Two independent notions of "is this a checkout" is how one path refuses a
    tree the other accepts, so the cloud path asks `ops` rather than testing
    for files itself. Pinned by monkeypatching the single definition and
    watching the cloud answer move with it.
    """
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: Path("/somewhere/else"))
    assert cloud_deploy.dev_source_root() == Path("/somewhere/else")
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: None)
    assert cloud_deploy.dev_source_root() is None


def test_dev_on_a_macos_target_is_refused_rather_than_ignored(dev_checkout):
    """Two flags that are each fine alone must not compose into a silent lie.

    The EC2 Mac bootstrap (#3867) installs published Homebrew formulas and has
    no working-tree source, so a `--dev` it merely ignored would hand the
    operator a published release while they believed they were testing their
    tree -- the exact defect #3950 exists to prevent. Refused in `resolve_plan`,
    so it costs nothing: the substrate is not applied yet.
    """
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_deploy.resolve_plan(_args(dev=True, os_family="macos", host="203.0.113.9"))
    message = str(excinfo.value)
    assert "--dev is not available for a macOS target" in message
    assert "Homebrew" in message
    # Both ways forward, the way every other refusal in this module names them.
    assert "Linux target" in message and "drop --dev" in message


def test_a_macos_deploy_without_dev_is_unaffected(dev_checkout):
    """The refusal is about the combination, not about Macs."""
    plan = cloud_deploy.resolve_plan(_args(os_family="macos", host="203.0.113.9"))
    assert plan.os_family == cloud_deploy.OS_FAMILY_MACOS
    assert plan.dev is False


def test_a_plain_deploy_needs_no_checkout_at_all(monkeypatch):
    """The repo-less default (#3504) is untouched by any of this."""
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: None)
    plan = cloud_deploy.resolve_plan(_args())
    assert plan.dev is False
    assert plan.source_dir == ""


# --- What gets shipped ---------------------------------------------------


def test_the_working_tree_is_shipped_including_uncommitted_edits(dev_checkout):
    """Dev mode's whole point: the code in front of you, not the last commit."""
    (dev_checkout / "src" / "nyxgpt" / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")
    (dev_checkout / "src" / "nyxgpt" / "brand_new.py").write_text("x = 1\n", encoding="utf-8")
    names = cloud_deploy.working_tree_files(dev_checkout)
    assert "src/nyxgpt/brand_new.py" in names
    archive = dev_checkout.parent / "tree.tar.gz"
    cloud_deploy.build_working_tree_archive(dev_checkout, archive)
    listed = subprocess.run(
        ["tar", "-xzOf", str(archive), "src/nyxgpt/__init__.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert listed.stdout == "VERSION = 2\n"


def test_ignored_paths_are_never_shipped(dev_checkout):
    """`node_modules` alone would dwarf the tree, and is rebuilt on the box anyway."""
    (dev_checkout / "web" / "node_modules").mkdir()
    (dev_checkout / "web" / "node_modules" / "huge.js").write_text("x", encoding="utf-8")
    (dev_checkout / "src" / "nyxgpt" / "stale.pyc").write_text("x", encoding="utf-8")
    names = cloud_deploy.working_tree_files(dev_checkout)
    assert not [name for name in names if "node_modules" in name or name.endswith(".pyc")]


def test_the_repository_itself_is_never_shipped(dev_checkout):
    """A source tree, not a repository: nothing on the instance to pull or push.

    CLAUDE.md's repo-less requirement is about the *product* never needing a
    checkout; `--dev` is the sanctioned development exception (D-009) and
    stays as close to it as it can -- the instance gets files, never git
    history and never a remote it could clone from.
    """
    names = cloud_deploy.working_tree_files(dev_checkout)
    assert not [name for name in names if name.startswith(".git/") or name == ".git"]


def test_symlinked_resource_directories_are_shipped(dev_checkout):
    """`src/nyxgpt/resources/` is symlinks, and dropping them breaks the install.

    Since #3621 the ops layer's runtime data (Compose files, unit templates,
    k8s manifests, the Terraform configs, `scripts/cloud/`) reaches the
    package through symlinks in `src/nyxgpt/resources/` pointing back at the
    top-level trees. git tracks each as one entry, and a plain `is_file()`
    filter answers False for every symlink-to-a-directory among them -- which
    would ship a checkout whose `ops install` cannot find its own resources,
    and would do it silently.
    """
    (dev_checkout / "src" / "nyxgpt" / "resources").mkdir()
    (dev_checkout / "shared").mkdir()
    (dev_checkout / "shared" / "compose.yml").write_text("x\n", encoding="utf-8")
    (dev_checkout / "src" / "nyxgpt" / "resources" / "shared").symlink_to("../../../shared")
    subprocess.run(["git", "-C", str(dev_checkout), "add", "-A"], check=True, capture_output=True)
    assert "src/nyxgpt/resources/shared" in cloud_deploy.working_tree_files(dev_checkout)

    archive = dev_checkout.parent / "tree.tar.gz"
    cloud_deploy.build_working_tree_archive(dev_checkout, archive)
    extracted = dev_checkout.parent / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(archive), "-C", str(extracted)], check=True, capture_output=True
    )
    # Shipped as a symlink, and one that still resolves on the far side --
    # its target travelled with it.
    landed = extracted / "src" / "nyxgpt" / "resources" / "shared"
    assert landed.is_symlink()
    assert (landed / "compose.yml").read_text(encoding="utf-8") == "x\n"


def test_a_tracked_file_deleted_in_the_working_tree_does_not_break_the_archive(dev_checkout):
    """git still lists it; tar would abort on the first one."""
    (dev_checkout / "web" / "package.json").unlink()
    names = cloud_deploy.working_tree_files(dev_checkout)
    assert "web/package.json" not in names
    cloud_deploy.build_working_tree_archive(dev_checkout, dev_checkout.parent / "tree.tar.gz")


def _fake_archive(source: Path, dest: Path) -> int:
    """Stand in for `build_working_tree_archive`, writing a real (tiny) file.

    Real enough that `ship_working_tree`'s `stat()` and `open()` work on it,
    so the tests below can stub the transfer without also stubbing pathlib.
    """
    dest.write_bytes(b"tarball")
    return 3


def test_shipping_replaces_the_previous_tree_rather_than_merging_into_it(dev_checkout, monkeypatch):
    """A file deleted since the last `--dev` deploy has to leave the instance too.

    Merging would leave a module the operator deleted still importable on the
    box -- an install that half-matches the tree, which is the failure that is
    hardest to diagnose from the outside.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cloud_deploy.subprocess, "run", fake_run)
    target = cloud_deploy.DeployTarget(host="198.51.100.10")
    # The archive build shells out too, so it is stubbed along with ssh --
    # this test is about the remote command, which the last call carries.
    monkeypatch.setattr(cloud_deploy, "build_working_tree_archive", _fake_archive)
    record = cloud_deploy.ship_working_tree(target, dev_checkout)
    remote_command = calls[-1][-1]
    assert f'rm -rf "{cloud_deploy.REMOTE_SOURCE_DIR}"' in remote_command
    assert f'tar -xzf - -C "{cloud_deploy.REMOTE_SOURCE_DIR}"' in remote_command
    assert record["step"] == "source"
    assert record["files"] == 3


def test_a_failed_transfer_is_reported_with_ssh_s_own_error(dev_checkout, monkeypatch):
    monkeypatch.setattr(cloud_deploy, "build_working_tree_archive", _fake_archive)
    monkeypatch.setattr(
        cloud_deploy.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "Permission denied"),
    )
    with pytest.raises(CloudCommandError, match="Permission denied"):
        cloud_deploy.ship_working_tree(cloud_deploy.DeployTarget(host="h"), dev_checkout)


def _timeout(*_a, **_k):
    raise subprocess.TimeoutExpired(cmd="stub", timeout=1)


def test_a_transfer_that_times_out_says_what_the_instance_is_left_holding(
    dev_checkout, monkeypatch
):
    """A raw `TimeoutExpired` traceback hides the part that matters.

    The remote command removes the old tree before extracting, so a transfer
    stopped part-way leaves an incomplete one on the box. An operator who
    reconnects meanwhile must not trust what is there.
    """
    monkeypatch.setattr(cloud_deploy, "build_working_tree_archive", _fake_archive)
    monkeypatch.setattr(cloud_deploy.subprocess, "run", _timeout)
    with pytest.raises(CloudCommandError, match="incomplete tree"):
        cloud_deploy.ship_working_tree(cloud_deploy.DeployTarget(host="h"), dev_checkout)


def test_listing_and_archiving_timeouts_are_named_rather_than_raised_raw(dev_checkout, monkeypatch):
    monkeypatch.setattr(cloud_deploy.subprocess, "run", _timeout)
    with pytest.raises(CloudCommandError, match="Listing the working tree"):
        cloud_deploy.working_tree_files(dev_checkout)

    monkeypatch.setattr(cloud_deploy, "working_tree_files", lambda _s: ["pyproject.toml"])
    with pytest.raises(CloudCommandError, match="Archiving the working tree"):
        cloud_deploy.build_working_tree_archive(dev_checkout, dev_checkout / "out.tar.gz")


# --- The provisioning script (acceptance criterion 2) --------------------


def test_the_dev_script_installs_the_shipped_tree_and_not_a_release(dev_checkout):
    """The build source actually changes -- the point of the whole issue."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(dev=True)))
    assert 'pip" install --quiet -e "$HOME/.nyxGPT/src"' in script
    assert "nyxgpt==" not in script
    assert "run_nyxgpt ops install --dev" in script


def test_the_default_script_still_installs_a_published_release(dev_checkout):
    """Same rendering function, and the artifact path is byte-for-byte unaffected."""
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    assert 'pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"' in script
    assert "--dev" not in script
    assert cloud_deploy.REMOTE_SOURCE_DIR not in script


def test_the_dev_script_refuses_on_the_instance_if_the_tree_did_not_arrive(dev_checkout):
    """Defence in depth for the one failure the local paths cannot have.

    Locally, "is there a checkout" is answered on the machine that installs.
    Here the two are different machines, so a transfer that silently did
    nothing would leave `ops install --dev` to fail somewhere less legible --
    or, worse, an earlier deploy's stale tree to be installed as if it were
    this one's.
    """
    script = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(dev=True)))
    assert 'if [ ! -f "$HOME/.nyxGPT/src/pyproject.toml" ]; then' in script
    assert "no working tree at $HOME/.nyxGPT/src" in script


def test_the_dev_script_clones_nothing_either(dev_checkout):
    """The tree crosses the deploy's own SSH connection; the box fetches no source."""
    lowered = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(dev=True))
    ).lower()
    assert "git clone" not in lowered
    assert "git://" not in lowered
    assert ".git" not in lowered


def test_both_scripts_share_one_bootstrap(dev_checkout):
    """Only the nyxGPT-install block differs -- everything else is one template.

    A second copy of a 200-line bootstrap is a copy that drifts, and this
    script has collected five runtime defects (#3759, #3760, #3761, #3762,
    #3782) that would each have had to be found and fixed twice.
    """
    artifact = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args()))
    dev = cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(dev=True)))
    for shared in (
        "no Python >= 3.11 is installed or installable",
        "sudo loginctl enable-linger",
        "ops session-backend",
        "self-heal enable",
    ):
        assert shared in artifact and shared in dev


# --- Orchestration and what is recorded ----------------------------------


@pytest.fixture
def stubbed_deploy(monkeypatch, _isolated_cloud_home):
    """Stub every external effect and record the order steps run in."""
    order: list[str] = []

    def fake_apply(args):
        order.append("apply")
        (_isolated_cloud_home / "state.json").write_text(
            json.dumps(
                {
                    "region": "us-east-1",
                    "instance_id": "i-0abc",
                    "public_ip": "198.51.100.10",
                    "security_group_id": "sg-0abc",
                }
            ),
            encoding="utf-8",
        )
        return {
            "action": "apply",
            "settings": {"owner_ip_cidr": "203.0.113.5/32", "aws_region": "us-east-1"},
            "outputs": {},
        }

    monkeypatch.setattr(cloud_infra, "apply_infra", fake_apply)
    monkeypatch.setattr(cloud_infra, "load_settings", lambda: {})
    monkeypatch.setattr(
        cloud_deploy, "wait_for_ssh", lambda target, timeout, **k: order.append("ssh") or 1.0
    )
    monkeypatch.setattr(
        cloud_deploy,
        "ship_working_tree",
        lambda target, source: (order.append("source") or {"step": "source", "files": 9}),
    )
    monkeypatch.setattr(
        cloud_deploy,
        "provision_instance",
        lambda target, plan: (
            order.append("provision") or {"version": plan.version, "dev": plan.dev}
        ),
    )
    monkeypatch.setattr(
        cloud_deploy,
        "start_tunnel",
        lambda target, profiles=None, **k: (
            order.append("tunnel") or {"running": True, "pid": 7, "urls": {}}
        ),
    )
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda timeout, **k: (
            order.append("health") or {"healthy": True, "url": "u", "status": 200, "waited": 1.0}
        ),
    )
    return order


def test_the_tree_is_shipped_before_the_instance_is_provisioned(stubbed_deploy, dev_checkout):
    """Order matters: the script installs from the tree, so the tree goes first.

    Shipping first also means a failed transfer fails with nothing installed,
    rather than half-way through a bootstrap that then has to be reasoned
    about.
    """
    result = cloud_deploy.deploy(_args(dev=True))
    assert stubbed_deploy == ["apply", "ssh", "source", "provision", "tunnel", "health"]
    assert [step["step"] for step in result["steps"]] == [
        "infra",
        "access",
        "ssh",
        "source",
        "provision",
        "tunnel",
        "health",
    ]


def test_a_plain_deploy_ships_nothing(stubbed_deploy, dev_checkout):
    """Even on a machine that *has* a checkout, the default copies nothing."""
    cloud_deploy.deploy(_args())
    assert "source" not in stubbed_deploy


def test_the_deploy_record_says_the_box_is_running_a_tree(
    stubbed_deploy, dev_checkout, _isolated_cloud_home
):
    """Otherwise a dev deploy and an artifact deploy are indistinguishable."""
    cloud_deploy.deploy(_args(dev=True))
    recorded = json.loads((_isolated_cloud_home / "deploy.json").read_text())
    assert recorded["dev"] is True
    assert recorded["source_dir"] == str(dev_checkout)


def test_dev_is_not_inherited_by_the_next_deploy(stubbed_deploy, dev_checkout):
    """A plain `nyxgpt cloud deploy` always means "install a published release".

    Every other recorded choice describes the instance's configuration and is
    rightly carried forward; `--dev` describes where *this run* gets its code.
    Inheriting it would silently re-ship whatever tree happened to be checked
    out, under a command the operator read as the artifact path.
    """
    cloud_deploy.deploy(_args(dev=True))
    plan = cloud_deploy.resolve_plan(_args())
    assert plan.dev is False
    assert plan.source_dir == ""


def test_the_history_entry_distinguishes_a_tree_deploy(
    stubbed_deploy, dev_checkout, _isolated_cloud_home
):
    """ "What happened to this deployment" has to include "it was not a release"."""
    cloud_deploy.deploy(_args(dev=True))
    events = [
        json.loads(line)
        for line in (_isolated_cloud_home / "history.jsonl").read_text().splitlines()
    ]
    assert events[-1]["dev"] is True


def test_status_reports_a_dev_deployment(stubbed_deploy, dev_checkout, monkeypatch):
    """Observable from the dashboard, per the Definition of Done."""
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"on_ec2": False})
    cloud_deploy.deploy(_args(dev=True))
    status = cloud_deploy.deploy_status()
    assert status["dev"] is True
    assert status["source_dir"] == str(dev_checkout)


def test_status_of_an_artifact_deployment_does_not_claim_dev(stubbed_deploy, monkeypatch):
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"on_ec2": False})
    cloud_deploy.deploy(_args())
    assert cloud_deploy.deploy_status()["dev"] is False


def test_the_human_status_names_the_build_source_of_a_dev_deployment(
    stubbed_deploy, dev_checkout, monkeypatch, capsys
):
    """`nyxgpt cloud status` without `--json` is what an operator actually runs.

    The machine form carrying `dev`/`source_dir` is not enough on its own: the
    default output prints `Version <the tree's declared version>`, which reads
    exactly like a published build. Same misleading line `_print_deploy_summary`
    already qualifies, one command later.
    """
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"on_ec2": False})
    cloud_deploy.deploy(_args(dev=True))
    capsys.readouterr()
    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
    printed = capsys.readouterr().out
    assert "Build source" in printed
    assert "--dev" in printed
    assert str(dev_checkout) in printed


def test_the_human_status_calls_an_artifact_deployment_a_published_release(
    stubbed_deploy, monkeypatch, capsys
):
    """Stated on every deployment: a row that appears only in the dev case is
    one an operator does not know to look for, so its absence says nothing."""
    monkeypatch.setattr(cloud_infra, "infra_status", lambda: {"on_ec2": False})
    cloud_deploy.deploy(_args())
    capsys.readouterr()
    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())
    printed = capsys.readouterr().out
    assert "Build source    published release" in printed
    assert "--dev" not in printed


def test_the_human_status_on_the_instance_does_not_guess_the_build_source(monkeypatch, capsys):
    """Asked on the box, there is no deploy record to answer from (D-018).

    `dev` is False there because nothing recorded it, not because a release was
    installed -- so claiming "published release" would be inventing the one
    fact this row exists to report.
    """
    monkeypatch.setattr(
        cloud_infra, "infra_status", lambda: {"on_ec2": True, "public_ip": "1.2.3.4"}
    )
    status = cloud_deploy.deploy_status()
    assert status["source"] == cloud_deploy.SOURCE_LOCAL_INSTANCE
    cloud_deploy._print_status_summary(status)
    printed = capsys.readouterr().out
    assert "Build source    not recorded here" in printed
    assert "published release" not in printed


def test_the_summary_says_the_version_is_not_a_published_release(
    stubbed_deploy, dev_checkout, capsys
):
    """The version line alone would read as a published build.

    A working tree's declared version usually names a release that does not
    exist yet, so printing it unqualified is the one place this feature could
    mislead an operator who is otherwise doing everything right.
    """
    result = cloud_deploy.deploy(_args(dev=True))
    cloud_deploy._print_deploy_summary(result)
    printed = capsys.readouterr().out
    assert "--dev" in printed
    assert str(dev_checkout) in printed
    assert "not from a published" in printed


# --- The admin API (D-017) -----------------------------------------------


def test_the_admin_api_cannot_ask_for_a_dev_deploy(dev_checkout):
    """The dashboard has no working tree to ship, and must not pretend otherwise.

    `POST /cloud/deploy` builds its namespace from an explicit allowlist of
    payload keys (`app._cloud_deploy_args`); `dev` is deliberately not one of
    them, so a request carrying it resolves to the artifact path rather than
    deploying whatever tree happens to sit beside the API process. Pinned
    because the omission is invisible -- `getattr(args, "dev", False)` makes
    it a silent default, and a later hand that adds the key would change the
    behaviour of a lifecycle action with no test to stop it (D-017).
    """
    from nyxgpt import app as app_module

    namespace = app_module._cloud_deploy_args({"dev": True, "version": "3.0.0"})
    assert cloud_deploy.resolve_plan(namespace).dev is False

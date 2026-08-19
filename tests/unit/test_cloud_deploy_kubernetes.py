"""`nyxgpt cloud deploy --kubernetes`: #3506's decision, implemented (#3956).

`product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md`, approved by the owner
2026-08-04, decided **EC2 single-box with the existing `k8s/*.yaml` manifests
optionally layered on a single-node k3s cluster for canary**. What it rejected
was a managed EKS control plane, not Kubernetes -- and the requirement never
reached #3513's acceptance criteria, so it shipped without a `--kubernetes`
option and canary rollout was unavailable on the cloud target entirely.

These tests hold the four properties that decision rests on, each of which is
silent when it breaks:

* the manifests are applied by the **existing** install mode, on the instance
  -- "no new deployment code path" (#3506's own wording);
* nothing listens on the public interface, and no ingress controller or
  `Service: LoadBalancer` implementation is installed (#3503);
* the instance gets everything from published artifacts, never a clone
  (CLAUDE.md, 2026-08-01);
* the substrate choice is recorded, so a bare re-deploy cannot install a
  native stack beside a running cluster.

Nothing here reaches AWS, k3s or SSH. What *executes* the k3s bootstrap is
`.github/workflows/k3s-cloud-smoke.yml`, which runs the very text
`render_k3s_bootstrap()` returns on a real Linux machine (D-006): the shape of
the script is unit-testable, but whether k3s comes up is not.
"""

import argparse
import json
import subprocess

import pytest

from nyxgpt import cloud_deploy, cloud_infra
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


def _args(**overrides) -> argparse.Namespace:
    base = {
        "host": None,
        "ssh_user": None,
        "identity_file": None,
        "version": "3.0.0",
        "skip_observability": False,
        "no_tunnel": False,
        "health_timeout": None,
        "ssh_timeout": None,
        "status": False,
        "yes": False,
        "kubernetes": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _script(**overrides) -> str:
    return cloud_deploy.render_provision_script(cloud_deploy.resolve_plan(_args(**overrides)))


def _executed(script: str) -> str:
    """The script with its comment lines removed.

    Several assertions below are of the form "this word does not appear", and
    the block comments explaining *why* it does not appear would fail them.
    Stripping comments asserts against what the instance actually runs, which
    is the claim being made anyway.
    """
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


# --- The flag, and what carries over -------------------------------------


def test_the_default_deploy_is_unchanged():
    """`--kubernetes` is opt-in: a bare deploy is the native stack it always was."""
    plan = cloud_deploy.resolve_plan(_args())
    assert plan.kubernetes is False
    assert plan.substrate == cloud_deploy.SUBSTRATE_NATIVE


def test_the_flag_selects_the_kubernetes_substrate():
    plan = cloud_deploy.resolve_plan(_args(kubernetes=True))
    assert plan.kubernetes is True
    assert plan.substrate == cloud_deploy.SUBSTRATE_KUBERNETES
    assert plan.to_dict()["substrate"] == "kubernetes"


def test_a_bare_redeploy_does_not_install_a_native_stack_beside_the_cluster(_isolated_cloud_home):
    """The substrate carries over, like --version and --session-backend do.

    Without this a re-deploy that omits the flag installs the native
    services -- Ollama, the api and web systemd units -- onto a box already
    running the whole stack in k3s, and the two then fight over ports
    8000/3000 on the instance's loopback. Nothing would report that; the
    health check passes either way, because *something* answers.
    """
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "kubernetes": True}), encoding="utf-8"
    )
    assert cloud_deploy.resolve_plan(_args(version=None)).kubernetes is True


def test_the_carry_forward_is_reversible(_isolated_cloud_home):
    """`--no-kubernetes` exists so the sticky choice is not a one-way door.

    A `store_true` flag that is also carried forward can only ever be turned
    on, which is the trap this shape avoids: `resolve_plan` distinguishes
    "not asked" (None) from "asked for off" (False).
    """
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "kubernetes": True}), encoding="utf-8"
    )
    assert cloud_deploy.resolve_plan(_args(version=None, kubernetes=False)).kubernetes is False


def test_a_deploy_predating_the_flag_reads_as_native(_isolated_cloud_home):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0"}), encoding="utf-8"
    )
    assert cloud_deploy.resolve_plan(_args(version=None)).kubernetes is False


def test_a_session_backend_the_cluster_cannot_honour_is_refused():
    """Refused, not accepted-and-ignored.

    In Kubernetes mode the Pods read `k8s/configmap.yaml`, which asserts
    `session_backend = cassandra`. `ops session-backend` writes the *host's*
    config.ini, which nothing in the cluster reads -- so `--session-backend
    file --kubernetes` is a flag that cannot do anything, and the deploy
    summary reporting it would have described a setting no Pod was using.
    """
    with pytest.raises(CloudCommandError, match="configmap"):
        cloud_deploy.resolve_plan(_args(kubernetes=True, session_backend="file"))


def test_a_carried_forward_backend_says_where_it_came_from(_isolated_cloud_home):
    """The nastier half: the operator passed no `--session-backend` at all.

    Adding `--kubernetes` to a box last deployed natively with `file` sessions
    would otherwise produce a refusal naming a flag they did not type.
    """
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "session_backend": "file"}), encoding="utf-8"
    )
    with pytest.raises(CloudCommandError, match="carries it forward"):
        cloud_deploy.resolve_plan(_args(version=None, kubernetes=True))


def test_the_default_backend_is_fine_in_kubernetes_mode():
    assert cloud_deploy.resolve_plan(_args(kubernetes=True)).session_backend == "cassandra"


# --- Where the substrate does not reach: macOS targets (#3867) -----------


def test_kubernetes_on_a_macos_target_is_refused_not_ignored():
    """The trap the #3867 merge created, closed explicitly.

    `render_provision_script`'s macOS branch returns the Homebrew bootstrap
    and never reaches the substrate sections, so `--os macos --kubernetes`
    would otherwise be accepted, dropped on the floor, and then contradicted
    by a deploy record claiming a cluster that was never installed. There is
    no macOS k3s path to offer instead: the installer builds a systemd unit.
    """
    with pytest.raises(CloudCommandError, match="k3s is Linux-only"):
        cloud_deploy.resolve_plan(_args(os_family="macos", host="mac.example", kubernetes=True))


def test_the_macos_refusal_beats_the_session_backend_refusal():
    """Order matters, because macOS defaults to the `file` backend (#3867).

    Checked after the backend instead, the combination would fail with a
    message about `k8s/configmap.yaml` and session lists -- a true statement
    about a setting the operator never chose, and the wrong diagnosis of
    what is really "that flag does not apply to this OS".
    """
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_deploy.resolve_plan(_args(os_family="macos", host="mac.example", kubernetes=True))
    assert "configmap" not in str(excinfo.value).lower()


def test_a_linux_substrate_does_not_follow_the_operator_onto_a_mac(_isolated_cloud_home):
    """The carry-forward is scoped to one target OS, like the backend is.

    Otherwise a box last deployed as Linux/k3s makes the *next* `--os macos`
    deploy fail on a `kubernetes: true` the operator did not ask for on this
    run and cannot see in their own command line.
    """
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "kubernetes": True, "os_family": "linux"}),
        encoding="utf-8",
    )
    plan = cloud_deploy.resolve_plan(_args(version=None, os_family="macos", host="mac.example"))
    assert plan.kubernetes is False
    assert plan.substrate == cloud_deploy.SUBSTRATE_NATIVE


def test_a_macos_script_carries_no_substrate_text(_isolated_cloud_home):
    """The Mac bootstrap is the #3511 one, with nothing k3s spliced into it."""
    script = cloud_deploy.render_provision_script(
        cloud_deploy.resolve_plan(_args(os_family="macos", host="mac.example"))
    )
    assert "k3s" not in _executed(script)


# --- Reuse: the existing install mode, on the instance -------------------


def test_the_manifests_are_applied_by_the_existing_install_mode():
    """#3506: "no new deployment code path, just the existing `--kubernetes`
    install mode pointed at the remote box over the P6-4 SSH path"."""
    script = _script(kubernetes=True)
    assert "run_nyxgpt ops install --kubernetes --local" in script
    # ...and never a hand-rolled apply that would bypass every step that
    # command performs (secret bootstrap, capacity preflight, image build,
    # the readiness waits).
    assert "kubectl apply" not in script
    assert "kustomize" not in script


def test_skip_observability_reaches_the_kubernetes_install_too():
    assert "ops install --kubernetes --local --skip-observability" in _script(
        kubernetes=True, skip_observability=True
    )


@pytest.mark.parametrize("profiles", ["monitoring,logging,tracing,errors", ""])
def test_exactly_one_kubernetes_install_pass(profiles):
    """Both branches counted as they actually run, not string-matched.

    Same rationale as the native path's single-pass test (#3762): a text scan
    sees the taken and the untaken branch alike.
    """
    script = _script(kubernetes=True)
    start = script.index('if [ -n "$NYXGPT_PROFILES" ]; then')
    block = script[start : script.index("\nfi\n", start) + len("\nfi\n")]

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f'run_nyxgpt() {{ echo "PASS: $*"; }}\nNYXGPT_PROFILES="{profiles}"\n{block}',
        ],
        capture_output=True,
        text=True,
    )
    expected = (
        "PASS: ops install --kubernetes --local"
        if profiles
        else "PASS: ops install --kubernetes --local --skip-observability"
    )
    assert [line for line in completed.stdout.splitlines() if line.startswith("PASS:")] == [
        expected
    ], completed.stdout


# --- #3503: nothing but TCP 22 -------------------------------------------


def test_k3s_binds_the_private_address_not_every_interface():
    """k3s's default is 0.0.0.0 -- the apiserver would listen on the public NIC.

    Refused by the security group, but listening, and #3503's access model is
    that nothing but TCP 22 is reachable. The private address rather than
    127.0.0.1 because the in-cluster `kubernetes` Service is built from the
    advertise address: pinned to loopback, every Pod that talks to the API
    server dials its own loopback instead.
    """
    flags = cloud_deploy.K3S_SERVER_FLAGS
    assert "--bind-address=$NYXGPT_NODE_IP" in flags
    assert "--advertise-address=$NYXGPT_NODE_IP" in flags
    assert "--tls-san=$NYXGPT_NODE_IP" in flags
    assert not any("0.0.0.0" in flag for flag in flags)


def test_no_ingress_controller_and_no_loadbalancer_implementation():
    """Both are on by default in k3s, and both are refused by #3506's premise.

    Traefik binds host ports 80/443; servicelb is what would make a
    `Service: LoadBalancer` provision anything. `k8s/*.yaml` declares neither,
    so removing the controllers costs nothing and makes the absence
    structural rather than a convention a later manifest could break silently.
    """
    assert "--disable=traefik" in cloud_deploy.K3S_SERVER_FLAGS
    assert "--disable=servicelb" in cloud_deploy.K3S_SERVER_FLAGS


def test_local_storage_is_left_enabled():
    """The Cassandra and Ollama StatefulSets bind through the default StorageClass.

    Neither declares a `storageClassName`, and on k3s the default is
    `local-path`, which the `local-storage` component provides. Disabling it
    to "reduce surface" leaves both Pods Pending on unbound PVCs -- a failure
    that looks like a capacity problem and is not one.
    """
    assert not any("local-storage" in flag for flag in cloud_deploy.K3S_SERVER_FLAGS)


def test_the_deploy_opens_no_new_port():
    """The access step is unchanged: one port-22 rule, tunnel to loopback."""
    script = _executed(_script(kubernetes=True))
    for exposed in ("NodePort", "LoadBalancer", "--node-external-ip", "authorize-security-group"):
        assert exposed not in script


def test_the_kubeconfig_is_owned_by_the_login_user_not_world_readable():
    """`--write-kubeconfig-mode 0644` is the usual shortcut and the wrong trade.

    /etc/rancher/k3s/k3s.yaml holds cluster-admin credentials, on a box whose
    entire access model is one port and one user.
    """
    script = _script(kubernetes=True)
    assert "--write-kubeconfig-mode" not in _executed(script)
    assert "--write-kubeconfig-mode" not in cloud_deploy.K3S_SERVER_FLAGS
    assert 'chmod 600 "$HOME/.kube/config"' in script


def test_the_kubeconfig_points_at_the_address_k3s_actually_listens_on():
    """k3s writes `server: https://127.0.0.1:6443` regardless of --bind-address.

    Left as written, every kubectl call on the instance -- the install, the
    access bridge, `nyxgpt canary` -- dials an address nothing answers on.
    """
    script = _script(kubernetes=True)
    assert 'sed -i "s#https://127.0.0.1:6443#https://${NYXGPT_NODE_IP}:6443#"' in script


# --- Repo-less (CLAUDE.md, 2026-08-01) -----------------------------------


def test_the_kubernetes_provision_script_never_clones_the_repository():
    script = _script(kubernetes=True).lower()
    assert "git clone" not in script
    assert "git://" not in script
    assert ".git" not in script


def test_the_instance_still_installs_the_pinned_published_release():
    script = _script(kubernetes=True, version="3.1.2")
    assert 'NYXGPT_VERSION="3.1.2"' in script
    assert 'pip" install --quiet "nyxgpt==${NYXGPT_VERSION}"' in script


# --- What the Kubernetes mode deliberately does NOT install --------------


def test_no_host_ollama_in_kubernetes_mode():
    """k8s/statefulset-ollama.yaml runs it in the cluster.

    A host Ollama would be a second model server on the same box, holding a
    second copy of every pulled model in RAM, that nothing points at.
    """
    script = _script(kubernetes=True)
    assert "ollama.com/install.sh" not in script
    # ...and the native path still does install it.
    assert "ollama.com/install.sh" in _script()


def test_no_host_node_toolchain_in_kubernetes_mode():
    """`nyxgpt-web:local` is a container built by docker from the published
    `nyxgpt-web` artifact, so the host's npm is never used. Installing it
    would add a NodeSource repo and several minutes to every deploy."""
    script = _script(kubernetes=True)
    assert "nodesource.com" not in script
    assert "nodesource.com" in _script()


def test_docker_is_still_installed_in_kubernetes_mode():
    """The install path builds both images with docker before importing them
    into k3s's containerd -- dropping docker would break the build, not just
    the load."""
    assert "sudo systemctl enable --now docker" in _script(kubernetes=True)


# --- The access bridge ---------------------------------------------------


def test_the_access_bridge_makes_the_cluster_reachable_through_the_tunnel():
    """Without it a `--kubernetes` deploy fails its own health check.

    `k8s/`'s Services are ClusterIP-only, so nothing binds 127.0.0.1:8000 on
    the instance the way the native services do -- and the SSH tunnel forwards
    to the instance's loopback. The stack would be perfectly healthy and
    completely unreachable.
    """
    script = _script(kubernetes=True)
    assert "nyxgpt-k8s-bridge@.service" in script
    assert "systemctl --user enable --now nyxgpt-k8s-bridge@api.service" in script
    assert "systemctl --user enable --now nyxgpt-k8s-bridge@web.service" in script


def test_the_access_bridge_runs_the_wrapped_command_not_raw_kubectl():
    """CLAUDE.md's wrapper requirement. `nyxgpt ops port-forward` already
    exists for exactly this and binds the same ports the tunnel forwards."""
    script = _script(kubernetes=True)
    assert "nyxgpt ops port-forward --target %i" in script
    assert "ExecStart=%h/.nyxGPT/venv/bin/nyxgpt ops port-forward" in script
    assert "kubectl port-forward" not in script


def test_the_access_bridge_survives_a_pod_replacement():
    """A canary rollout replaces Pods by design, which ends a port-forward.

    A bridge that does not come back turns every rollout into an outage of
    the operator's own access path.
    """
    script = _script(kubernetes=True)
    unit = script[script.index("nyxgpt-k8s-bridge@.service") : script.index("\nUNIT\n")]
    assert "Restart=always" in unit


def test_the_access_bridge_carries_its_own_kubeconfig_and_path():
    """A systemd --user unit inherits neither the shell's environment nor the
    login PATH, so both have to be stated or the unit fails on `kubectl not
    found` at every boot."""
    script = _script(kubernetes=True)
    unit = script[script.index("nyxgpt-k8s-bridge@.service") : script.index("\nUNIT\n")]
    assert "Environment=KUBECONFIG=%h/.kube/config" in unit
    assert "/usr/local/bin" in unit


def test_the_observability_bridge_is_skipped_when_observability_is():
    assert "nyxgpt-k8s-bridge@observability.service" in _script(kubernetes=True)
    script = _script(kubernetes=True, skip_observability=True)
    # The unit is enabled behind the same profile guard the install uses, so
    # a --skip-observability deploy does not leave a unit restart-looping
    # against four Services that were never created.
    guarded = script[script.index("nyxgpt-k8s-bridge@web.service") :]
    assert 'if [ -n "$NYXGPT_PROFILES" ]; then' in guarded
    assert 'NYXGPT_PROFILES=""' in script


def test_the_bridge_is_installed_after_the_stack_exists():
    script = _script(kubernetes=True)
    assert script.index("run_nyxgpt ops install --kubernetes") < script.index("nyxgpt-k8s-bridge")


def test_the_native_deploy_grows_no_bridge():
    """A native deploy installs no bridge. It does *retire* one -- the
    `--no-kubernetes` transition below -- so the claim is about the unit file
    and the `enable`, not about the string appearing anywhere."""
    script = _script()
    assert "nyxgpt-k8s-bridge@.service\" <<'UNIT'" not in script
    assert "systemctl --user enable --now nyxgpt-k8s-bridge" not in script


# --- Switching substrates on an instance that already has one ------------
#
# The failure these pin is silent in every direction that is not covered.
# k3s and the `Restart=always` bridge keep answering 127.0.0.1:8000/3000
# after a `--no-kubernetes` re-deploy, so the native services fail to bind
# and every health probe -- the install's, the deploy's, the tunnel's -- is
# satisfied by the substrate the operator just asked to leave, while
# `deploy.json` and the dashboard's Substrate row both say "native".


def test_the_native_deploy_retires_a_cluster_the_instance_was_running():
    script = _script()

    assert "systemctl --user disable --now" in script
    assert "nyxgpt-k8s-bridge@${bridge}.service" in script
    assert "/usr/local/bin/k3s-uninstall.sh" in script


def test_the_native_teardown_runs_before_the_native_install():
    """Order is the whole point: uninstalling k3s after `ops install` would
    leave the install racing the cluster for the ports it needs."""
    script = _script()

    assert script.index("k3s-uninstall.sh") < script.index("run_nyxgpt ops install")


def test_the_native_teardown_is_guarded_and_survives_a_box_that_never_had_k3s():
    """A first deploy and an ordinary native re-deploy must pass straight
    through: `disable --now` on an absent unit and an absent uninstaller are
    both errors, and `set -euo pipefail` would abort the deploy on either."""
    script = _script()

    assert "if [ -x /usr/local/bin/k3s-uninstall.sh ]; then" in script
    bridge_teardown = script[
        script.index("for bridge in api web observability") : script.index(
            'rm -f "$HOME/.config/systemd/user/nyxgpt-k8s-bridge@.service"'
        )
    ]
    assert ">/dev/null 2>&1 || true" in bridge_teardown


def test_the_kubernetes_deploy_retires_a_native_stack_the_instance_was_running():
    """The mirror image, and the direction that used to fail loudly with a
    refusal prescribing `nyxgpt ops down` -- a command no wrapped `nyxgpt
    cloud` surface can run on the instance."""
    script = _script(kubernetes=True)

    assert "run_nyxgpt ops down" in script
    assert 'if [ -f "$HOME/.config/systemd/user/nyxgpt-api.service" ]; then' in script
    assert script.index("run_nyxgpt ops down") < script.index("run_nyxgpt ops install --kubernetes")


def test_neither_teardown_appears_in_the_other_substrates_script():
    """Each script retires the substrate it is replacing, and only that one:
    a native deploy must not tear down the native stack it is about to
    install, nor a Kubernetes deploy the cluster it is about to use."""
    assert "k3s-uninstall.sh" not in _script(kubernetes=True)
    assert "run_nyxgpt ops down" not in _script()


# --- The rendered script is a script -------------------------------------


@pytest.mark.parametrize("kubernetes", [False, True])
def test_the_rendered_script_parses_as_bash(kubernetes, tmp_path):
    """Substituting whole blocks makes an unbalanced `fi` a rendering bug, not
    a syntax error someone reads -- so the rendering is parsed, every time."""
    path = tmp_path / "provision.sh"
    path.write_text(_script(kubernetes=kubernetes), encoding="utf-8")
    completed = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_no_placeholder_survives_rendering():
    for kubernetes in (False, True):
        script = _script(kubernetes=kubernetes)
        assert "__" not in script.replace("__VERSION__", ""), script


def test_render_k3s_bootstrap_is_the_text_the_deploy_sends():
    """The CI proxy executes `render_k3s_bootstrap()`; if that drifted from
    what a deploy actually sends, the executed evidence would be evidence
    about something else (D-006)."""
    assert cloud_deploy.render_k3s_bootstrap() in _script(kubernetes=True)
    assert "__K3S_SERVER_FLAGS__" not in cloud_deploy.render_k3s_bootstrap()


class _ReachedSSH(Exception):
    """Stops `deploy` at the first step that needs a real instance."""


@pytest.mark.parametrize(("kubernetes", "expected"), [(True, True), (False, False)])
def test_the_node_sizing_pointer_comes_before_the_provisioning(
    _isolated_cloud_home, monkeypatch, capsys, kubernetes, expected
):
    """In Kubernetes mode the instance carries the whole stack as Pods, and the
    default instance type was chosen for the native layout.

    The install's own capacity preflight refuses an undersized node before it
    builds anything (#3825) -- but that is on the instance, after ~20 minutes
    of provisioning, which is a slow and billed way to learn that
    `--instance-type` exists. Said before that, and only in Kubernetes mode:
    on a native deploy the default is the right size and the advice would be
    noise.
    """
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps({"public_ip": "198.51.100.10", "region": "us-east-1"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cloud_infra,
        "apply_infra",
        lambda args: {"outputs": {}, "settings": {"instance_type": "m5.large"}},
    )

    def stop(*_a, **_k):
        raise _ReachedSSH

    monkeypatch.setattr(cloud_deploy, "wait_for_ssh", stop)

    with pytest.raises(_ReachedSSH):
        cloud_deploy.deploy(_args(kubernetes=kubernetes))

    err = capsys.readouterr().err
    assert ("--instance-type" in err) is expected
    if expected:
        assert "m5.large" in err
        assert "docs/kubernetes.md" in err


# --- The CLI surface -----------------------------------------------------


def _dispatched(monkeypatch, argv: list[str]) -> argparse.Namespace:
    """Run `argv` through the real parser AND the real dispatcher.

    Both halves matter and both fail silently on their own: a `store_true`
    would parse and then break the carry-forward, and a `cloud_cmd` the
    dispatcher does not list parses fine and falls through to an unrelated
    branch at runtime.
    """
    from nyxgpt import cli as cli_mod

    seen: dict[str, argparse.Namespace] = {}

    def capture(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli_mod.cloud_deploy_mod, "deploy_command", capture)
    assert cli_mod.cli(argv) == 0
    return seen["args"]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], None), (["--kubernetes"], True), (["--no-kubernetes"], False)],
)
def test_the_flag_is_tri_state_through_the_real_parser(monkeypatch, flags, expected):
    """`resolve_plan` distinguishes three cases and argparse has to preserve
    all three. A `store_true` here would collapse "not asked" into "asked for
    off" -- which reads fine and quietly makes the carry-forward impossible to
    turn off."""
    args = _dispatched(monkeypatch, ["cloud", "deploy", *flags])
    assert args.kubernetes is expected


def test_cloud_canary_reaches_the_cloud_deploy_module(monkeypatch):
    args = _dispatched(monkeypatch, ["cloud", "canary", "promote", "--step", "20", "--force"])
    assert args.cloud_cmd == "canary"
    assert args.canary_cmd == "promote"
    assert cloud_deploy.canary_argv(args) == ["canary", "promote", "--step", "20", "--force"]


# --- `nyxgpt cloud canary` -----------------------------------------------


@pytest.mark.parametrize("subcommand", cloud_deploy.REMOTE_CANARY_COMMANDS)
def test_every_canary_subcommand_is_reachable_on_the_cloud_target(subcommand):
    """The capability #3506 was choosing a substrate FOR."""
    argv = cloud_deploy.canary_argv(argparse.Namespace(canary_cmd=subcommand))
    assert argv[:2] == ["canary", subcommand]


def test_canary_deploy_is_deliberately_absent():
    """`nyxgpt canary deploy` builds a versioned image from the current
    checkout, and the instance has none by construction. Rolling a new release
    out to the cloud is `nyxgpt cloud deploy --version <release>`."""
    with pytest.raises(CloudCommandError, match="checkout"):
        cloud_deploy.canary_argv(argparse.Namespace(canary_cmd="deploy"))


def test_canary_flags_are_passed_through():
    argv = cloud_deploy.canary_argv(
        argparse.Namespace(canary_cmd="promote", component="web", step=25, force=True)
    )
    assert argv == ["canary", "promote", "--component", "web", "--step", "25", "--force"]


def test_canary_flags_are_scoped_to_the_subcommand_that_has_them():
    """`--force` on `rollback` would be silently accepted and silently
    meaningless; an allowlist per subcommand is what stops a flag being
    forwarded to a command that does not take it."""
    argv = cloud_deploy.canary_argv(
        argparse.Namespace(canary_cmd="rollback", component="api", force=True, step=90)
    )
    assert argv == ["canary", "rollback", "--component", "api"]


def test_canary_numeric_flags_cannot_smuggle_shell_text():
    """Everything here is spliced into a remote shell command."""
    with pytest.raises(ValueError):
        cloud_deploy.canary_argv(
            argparse.Namespace(canary_cmd="start", component=None, weight="10; rm -rf /")
        )


def test_canary_refuses_a_deployment_that_has_no_cluster(_isolated_cloud_home, capsys):
    """A native deployment has nothing to weight traffic in, and the failure
    an operator would otherwise get is `kubectl: not found` from a box they
    never asked to run Kubernetes on."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": False}),
        encoding="utf-8",
    )
    args = argparse.Namespace(cloud_cmd="canary", canary_cmd="status", component=None)
    assert cloud_deploy.deploy_command(args) == 1
    assert "--kubernetes" in capsys.readouterr().err


def test_canary_runs_the_instances_own_nyxgpt_over_the_wrapped_ssh_path(
    _isolated_cloud_home, monkeypatch
):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": True}),
        encoding="utf-8",
    )
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps({"public_ip": "198.51.100.10", "region": "us-east-1"}), encoding="utf-8"
    )
    seen = {}

    def fake_run_remote(target, command, **kwargs):
        seen["target"] = f"{target.user}@{target.host}"
        seen["command"] = command
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(cloud_deploy, "run_remote", fake_run_remote)
    args = argparse.Namespace(
        cloud_cmd="canary",
        canary_cmd="status",
        component="api",
        host=None,
        ssh_user=None,
        identity_file=None,
    )
    assert cloud_deploy.deploy_command(args) == 0
    assert seen["target"] == "ec2-user@198.51.100.10"
    assert seen["command"] == ('"$HOME/.nyxGPT/venv/bin/nyxgpt" canary status --component api')


def test_a_failing_canary_evaluation_is_an_answer_not_a_transport_error(
    _isolated_cloud_home, monkeypatch
):
    """`nyxgpt canary evaluate` exits 2 on a canary it rolled back. That is the
    result the operator asked for, and it has to survive the SSH hop as
    itself -- collapsing it to 1 would make a regressing canary
    indistinguishable from an unreachable instance."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": True}),
        encoding="utf-8",
    )
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps({"public_ip": "198.51.100.10"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess([], 2, "", ""),
    )
    args = argparse.Namespace(
        cloud_cmd="canary",
        canary_cmd="evaluate",
        component=None,
        host=None,
        ssh_user=None,
        identity_file=None,
    )
    assert cloud_deploy.deploy_command(args) == 2


def test_an_unreachable_instance_names_the_access_remedy(_isolated_cloud_home, monkeypatch):
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": True}),
        encoding="utf-8",
    )
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps({"public_ip": "198.51.100.10"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess([], 255, "", "timed out"),
    )
    target = cloud_deploy.recorded_target()
    assert target is not None
    with pytest.raises(CloudCommandError, match="allow-ip"):
        cloud_deploy.remote_canary(target, ["canary", "status"])


# --- What status and teardown report -------------------------------------


def test_the_substrate_is_observable_in_the_status_payload(_isolated_cloud_home):
    """Observable, never operable (D-017): the dashboard reports which
    substrate is running and names the command that changes it."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps(
            {"version": "3.0.0", "host": "198.51.100.10", "substrate": "kubernetes"},
        ),
        encoding="utf-8",
    )
    status = cloud_deploy.deploy_status()
    assert status["substrate"] == "kubernetes"
    assert status["commands"]["deploy_kubernetes"] == "nyxgpt cloud deploy --kubernetes"
    assert status["commands"]["canary"] == "nyxgpt cloud canary status"


@pytest.mark.parametrize(
    ("substrate", "expected"),
    [
        ("kubernetes", "k3s"),
        ("native", "--kubernetes"),
        ("", "unknown"),
    ],
)
def test_cloud_status_prints_which_substrate_is_running(
    _isolated_cloud_home, capsys, substrate, expected
):
    """The status summary is where an operator looks after the deploy's
    scrollback is gone (#3813), so a substrate that is only in the JSON is a
    substrate they will not find. The native line names the flag, because
    "how do I get canary here" is the question this answers."""
    record = {"version": "3.0.0", "host": "198.51.100.10"}
    if substrate:
        record["substrate"] = substrate
    (_isolated_cloud_home / "deploy.json").write_text(json.dumps(record), encoding="utf-8")

    cloud_deploy._print_status_summary(cloud_deploy.deploy_status())

    out = capsys.readouterr().out
    assert "Substrate" in out
    assert expected in out


def test_an_unknown_substrate_is_reported_as_unknown_not_as_native(_isolated_cloud_home):
    """A deploy from before this flag existed is not a claim that the box runs
    the native stack -- same three-way honesty D-018 requires of every other
    cloud status field."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10"}), encoding="utf-8"
    )
    assert cloud_deploy.deploy_status()["substrate"] == ""


def test_destroy_removes_the_record_that_would_pin_the_next_deploy(
    _isolated_cloud_home, monkeypatch
):
    """The cluster lives entirely on the instance -- control plane, containerd
    image store, `local-path` volumes -- so terminating it IS the teardown.
    What must not survive is the substrate marker `resolve_plan` reads back.
    """
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(cloud_infra, "destroy_infra", lambda args: {"settings": {}})

    cloud_deploy.destroy(_args(yes=True))

    assert not (_isolated_cloud_home / "deploy.json").exists()
    assert cloud_deploy.resolve_plan(_args(version="3.0.0")).kubernetes is False


def test_destroy_does_not_drain_a_cluster_it_is_about_to_terminate(
    _isolated_cloud_home, monkeypatch
):
    """Adding an `ops down --kubernetes` pass would spend minutes gracefully
    evicting workloads off a machine being deleted seconds later, and would
    add a way for the teardown to hang on a cluster that is already
    unreachable."""
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "host": "198.51.100.10", "kubernetes": True}),
        encoding="utf-8",
    )
    reached = []
    monkeypatch.setattr(cloud_deploy, "stop_tunnel", lambda: {"stopped": True})
    monkeypatch.setattr(cloud_infra, "destroy_infra", lambda args: {"settings": {}})
    monkeypatch.setattr(cloud_deploy, "run_remote", lambda *a, **k: reached.append(a))

    cloud_deploy.destroy(_args(yes=True))

    assert reached == []


def test_the_deploy_history_records_which_substrate_was_deployed(_isolated_cloud_home):
    cloud_deploy.record_history("deploy", "succeeded", version="3.0.0", substrate="kubernetes")
    assert cloud_deploy.deploy_history()[-1]["substrate"] == "kubernetes"

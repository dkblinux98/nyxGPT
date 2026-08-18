"""Unit-test configuration and fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_cassandra_pool():
    """Reset the module-level Cassandra connection pool before and after each test.

    Without this, a pool created in one test would be reused in the next test,
    causing stale mocks and unexpected connection attempts.
    """
    import contextlib

    from nyxgpt.rag.vectorstore_cassandra import reset_connection_pool

    with contextlib.suppress(Exception):
        reset_connection_pool()

    yield

    with contextlib.suppress(Exception):
        reset_connection_pool()


@pytest.fixture(autouse=True)
def _reset_query_result_cache():
    """Reset the module-level RAG query result cache before and after each test.

    Without this, a cache backend (or disabled NoOpCache) created in one test
    would be reused in later tests, causing stale hits/misses depending on
    test execution order.
    """
    import nyxgpt.rag.rag as rag_module

    rag_module._query_result_cache = None
    yield
    rag_module._query_result_cache = None


@pytest.fixture(autouse=True)
def _reset_config_fallback_warnings():
    """Reset the config module's once-per-key fallback warning dedup set.

    `config._log_fallback_once` only logs a given key's fallback once per
    process, so without this reset a test asserting a fallback WARNING would
    pass or fail depending on whether an earlier test already triggered the
    same key's fallback.
    """
    from nyxgpt.config import reset_fallback_warnings

    reset_fallback_warnings()
    yield
    reset_fallback_warnings()


@pytest.fixture(autouse=True)
def _isolate_install_mode_marker(monkeypatch, tmp_path):
    """Redirect the install-mode markers into `tmp_path` for every unit test (#3789).

    `install_mode.INSTALL_MODE_FILE` defaults to the developer's real
    `~/.nyxGPT/install-mode.json`, and it is not an inert file: it is what
    `ops._reconcile_install_mode()` compares the requested mode against, so a
    test that reaches that function on a machine recording `dev` (exactly the
    machine `nyxgpt up --dev` produces) makes it decide the mode is *changing*
    -- which deletes the real `~/.nyxGPT/opt/nyxgpt-api/venv`, boots out the
    live dev LaunchAgents on macOS, and rewrites the real marker to artifact.
    The suite would then be destroying the state of the very machine it runs
    on, and passing or failing according to what that machine happens to have
    installed.

    Isolating the marker here, once, closes that for every present and future
    test rather than per call site. Tests that need to exercise the marker
    itself still write to it -- they just write into `tmp_path`.

    `NYXGPT_HOME` is redirected alongside it because the per-substrate markers
    (#3834 -- `install-mode-kubernetes.json`; #3835 --
    `install-mode-terraform.json`) are resolved from it rather than from a
    module-level constant of their own.
    """
    from nyxgpt import install_mode

    home = tmp_path / "install-mode-home"
    monkeypatch.setattr(install_mode, "NYXGPT_HOME", home)
    monkeypatch.setattr(install_mode, "INSTALL_MODE_FILE", home / "install-mode.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_ops_terraform_dir(monkeypatch, tmp_path):
    """Redirect the ops-managed Terraform working directory into `tmp_path` (#3835).

    `ops.TERRAFORM_DIR` defaults to the developer's real
    `~/.nyxGPT/terraform`, which `_sync_local_terraform_config` writes into
    (and `terraform destroy` runs against). A unit test reaching either must
    not touch the state of a real deployment on the machine running the
    suite -- the same reasoning as the install-mode marker above.
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "TERRAFORM_DIR", tmp_path / "ops-terraform")
    yield


@pytest.fixture(autouse=True)
def _isolate_image_build_state(monkeypatch, tmp_path):
    """Redirect the Docker build staging and image markers into `tmp_path` (#3834).

    Same hazard as the install-mode marker above, one directory over.
    `ops.K8S_BUILD_DIR` is where the Kubernetes artifact path unpacks the
    published `nyxgpt-api`/`nyxgpt-web` tarballs to build from, and
    `DOCKER_IMAGE_MARKER_DIR` records which source each `:local` image was
    last built from. A test that reaches either -- by stubbing one layer of
    the build and not the one below it, which is exactly how this was found
    -- otherwise copies the whole working tree into the developer's real
    `~/.nyxGPT`, and can make their next real install skip a rebuild it
    needed (or run one it didn't).
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "K8S_BUILD_DIR", tmp_path / "k8s-build-home")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path / "docker-image-markers")
    yield


@pytest.fixture(autouse=True)
def _refuse_real_docker_builds(monkeypatch):
    """Fail any unit test that reaches a real `docker build` (#3834).

    A unit test that shells out to the machine's Docker daemon is not a unit
    test: it takes minutes on a cold cache, it depends on a daemon being
    there at all, and it leaves `nyxgpt-api:local` / `nyxgpt-web:local`
    behind on the developer's machine -- where the next real `nyxgpt ops
    install` reads them, and skips or forces a rebuild accordingly.

    It is easy to do by accident, because the install paths build images
    through several layers: stubbing one and not the one below it is enough.
    Two suites were doing it when this guard was added (the Terraform
    lifecycle-ledger test, and the Kubernetes install-step tests that stubbed
    the low-level builder while the step called the new per-component
    wrapper). The guard names the command, so the fix is obvious: stub the
    build step this test isn't about.
    """
    from nyxgpt import ops

    real_run = ops._run

    def guarded(cmd, *args, **kwargs):
        if list(cmd)[:2] == ["docker", "build"]:
            raise AssertionError(
                "a unit test reached a real `docker build` -- stub the build step it is "
                f"not testing (command: {list(cmd)})"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ops, "_run", guarded)
    yield

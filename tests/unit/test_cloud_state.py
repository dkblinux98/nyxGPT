"""Unit tests for `nyxgpt cloud state` -- Terraform remote state (P6-9, #3510).

Neither Terraform nor AWS is reachable here: `_run_terraform` is replaced with
a recorder so the tests assert on the command lines the wrapper builds, and
boto3 is replaced with fakes that record API calls and can be told to raise
the specific botocore error codes the module branches on. What's under test is
therefore the wrapper's contract -- which flags init gets when the backend
changes, which bucket settings are non-negotiable, and what `backend.json`
records when a migration fails part-way.
"""

import argparse
import json
import re
import subprocess

import pytest

from nyxgpt import cloud_infra, cloud_state
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit


class _ClientError(Exception):
    """Stand-in for botocore's ClientError, which carries its code in `response`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """Records S3 calls; `fail_create` makes create_bucket raise a given code."""

    def __init__(self, fail_create: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.fail_create = fail_create
        self.versioning_status = "Enabled"
        self.versions: list[dict] = []

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))
        if self.fail_create:
            raise _ClientError(self.fail_create)

    def put_bucket_versioning(self, **kwargs):
        self.calls.append(("put_bucket_versioning", kwargs))

    def put_bucket_encryption(self, **kwargs):
        self.calls.append(("put_bucket_encryption", kwargs))

    def put_public_access_block(self, **kwargs):
        self.calls.append(("put_public_access_block", kwargs))

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        return {"Status": self.versioning_status}

    def list_object_versions(self, **kwargs):
        self.calls.append(("list_object_versions", kwargs))
        return {"Versions": self.versions}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))

        class _Body:
            @staticmethod
            def read():
                return b'{"version": 4, "serial": 7}'

        return {"Body": _Body()}


class _FakeDynamo:
    """Records DynamoDB calls; `fail_create` makes create_table raise a code."""

    def __init__(self, fail_create: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.fail_create = fail_create
        self.waited: list[str] = []

    def create_table(self, **kwargs):
        self.calls.append(("create_table", kwargs))
        if self.fail_create:
            raise _ClientError(self.fail_create)

    def get_waiter(self, name):
        waited = self.waited

        class _Waiter:
            @staticmethod
            def wait(**kwargs):
                waited.append(kwargs["TableName"])

        return _Waiter()

    def describe_table(self, **kwargs):
        self.calls.append(("describe_table", kwargs))
        return {"Table": {"TableStatus": "ACTIVE"}}


class _FakeSts:
    @staticmethod
    def get_caller_identity():
        return {"Account": "123456789012"}


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path both modules write to at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    monkeypatch.setattr(cloud_infra, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_infra, "TERRAFORM_DIR", cloud_dir / "terraform")
    monkeypatch.setattr(cloud_infra, "TFVARS_FILE", cloud_dir / "terraform.tfvars")
    monkeypatch.setattr(cloud_infra, "TFSTATE_FILE", cloud_dir / "terraform.tfstate")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    # Computed from CLOUD_DIR at import time, so it needs repointing too.
    monkeypatch.setattr(cloud_state, "BACKEND_FILE", cloud_dir / "backend.json")
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return cloud_dir


@pytest.fixture
def terraform_calls(monkeypatch):
    """Record `_run_terraform` invocations instead of shelling out to Terraform."""
    calls: list[list[str]] = []

    def fake_run(arguments, *, capture=False, extra_env=None, chdir=None):
        calls.append(list(arguments))
        stdout = '{"version": 4, "serial": 3}' if arguments[:2] == ["state", "pull"] else ""
        return subprocess.CompletedProcess(["terraform", *arguments], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(cloud_infra, "_run_terraform", fake_run)
    return calls


@pytest.fixture
def aws(monkeypatch):
    """Replace `_client` with fakes, returned as a dict keyed by service name."""
    clients = {"s3": _FakeS3(), "dynamodb": _FakeDynamo(), "sts": _FakeSts()}
    monkeypatch.setattr(
        cloud_state, "_client", lambda service, region, profile="": clients[service]
    )
    return clients


def _args(**overrides) -> argparse.Namespace:
    base = {"bucket": None, "table": None, "key": None, "region": "us-west-2", "profile": None}
    base.update(overrides)
    return argparse.Namespace(**base)


# --- Backend rendering ---------------------------------------------------


def test_render_backend_tf_carries_the_bucket_key_region_and_lock_table():
    settings = cloud_state.BackendSettings(
        bucket="nyxgpt-tfstate-123", table="locks", region="us-west-2", key="a/b.tfstate"
    )
    rendered = cloud_state.render_backend_tf(settings)

    assert 'backend "s3"' in rendered
    assert 'bucket         = "nyxgpt-tfstate-123"' in rendered
    assert 'key            = "a/b.tfstate"' in rendered
    assert 'region         = "us-west-2"' in rendered
    assert 'dynamodb_table = "locks"' in rendered
    # Encryption at rest is not optional -- state carries every resource id.
    assert "encrypt        = true" in rendered
    # No profile configured means the standard AWS credential chain applies;
    # emitting `profile = ""` would pin it to a profile literally named "".
    assert "profile" not in rendered


def test_render_backend_tf_includes_the_profile_when_one_is_configured():
    settings = cloud_state.BackendSettings(
        bucket="b", table="t", region="us-east-1", profile="nyxgpt"
    )
    assert 'profile        = "nyxgpt"' in cloud_state.render_backend_tf(settings)


def test_apply_configured_backend_only_uses_s3_once_the_migration_succeeded():
    """`bootstrap` creates the AWS resources but must not switch the backend."""
    cloud_infra.sync_terraform_config()
    backend_tf = cloud_infra.TERRAFORM_DIR / cloud_state.BACKEND_TF_NAME

    cloud_state.save_backend_settings(
        cloud_state.BackendSettings(bucket="b", table="t", region="us-east-1", enabled=False)
    )
    cloud_state.apply_configured_backend()
    assert 'backend "local"' in backend_tf.read_text(encoding="utf-8")

    cloud_state.save_backend_settings(
        cloud_state.BackendSettings(bucket="b", table="t", region="us-east-1", enabled=True)
    )
    cloud_state.apply_configured_backend()
    assert 'backend "s3"' in backend_tf.read_text(encoding="utf-8")


def test_sync_reapplies_the_configured_backend_over_the_packaged_default():
    """A re-sync must not silently demote a migrated install back to local state."""
    cloud_state.save_backend_settings(
        cloud_state.BackendSettings(bucket="b", table="t", region="us-east-1", enabled=True)
    )
    cloud_infra.sync_terraform_config()

    backend_tf = cloud_infra.TERRAFORM_DIR / cloud_state.BACKEND_TF_NAME
    assert 'backend "s3"' in backend_tf.read_text(encoding="utf-8")


def test_packaged_configuration_ships_a_separate_backend_file():
    """The local<->S3 switch depends on the backend living in its own file."""
    packaged = cloud_infra.packaged_terraform_dir()
    backend_tf = (packaged / cloud_state.BACKEND_TF_NAME).read_text(encoding="utf-8")
    assert 'backend "local"' in backend_tf
    # versions.tf must not also declare one -- Terraform allows exactly one.
    # Anchored to the start of a line so the file's prose about the split
    # (which necessarily says "backend") doesn't read as a declaration.
    versions_tf = (packaged / "versions.tf").read_text(encoding="utf-8")
    assert re.search(r'^\s*backend\s+"', versions_tf, re.MULTILINE) is None


# --- Bucket and lock table provisioning ----------------------------------


def test_bucket_creation_pins_versioning_encryption_and_public_access_block(aws):
    cloud_state.ensure_state_bucket("nyxgpt-tfstate-x", "us-west-2")

    calls = dict(aws["s3"].calls)
    assert calls["create_bucket"]["CreateBucketConfiguration"] == {
        "LocationConstraint": "us-west-2"
    }
    assert calls["put_bucket_versioning"]["VersioningConfiguration"] == {"Status": "Enabled"}
    rule = calls["put_bucket_encryption"]["ServerSideEncryptionConfiguration"]["Rules"][0]
    assert rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert all(calls["put_public_access_block"]["PublicAccessBlockConfiguration"].values())


def test_us_east_1_bucket_is_created_without_a_location_constraint(aws):
    """The API rejects an explicit LocationConstraint naming its own default region."""
    cloud_state.ensure_state_bucket("nyxgpt-tfstate-x", "us-east-1")

    create = dict(aws["s3"].calls)["create_bucket"]
    assert "CreateBucketConfiguration" not in create
    assert create["Bucket"] == "nyxgpt-tfstate-x"


def test_existing_bucket_is_adopted_and_still_has_its_settings_enforced(monkeypatch):
    """A bucket created by hand is exactly the one likely to be missing versioning."""
    s3 = _FakeS3(fail_create="BucketAlreadyOwnedByYou")
    monkeypatch.setattr(cloud_state, "_client", lambda service, region, profile="": s3)

    actions = cloud_state.ensure_state_bucket("existing", "us-west-2")

    assert any("already exists" in action for action in actions)
    assert "put_bucket_versioning" in dict(s3.calls)


def test_bucket_name_taken_by_another_account_is_a_clear_error(monkeypatch):
    s3 = _FakeS3(fail_create="BucketAlreadyExists")
    monkeypatch.setattr(cloud_state, "_client", lambda service, region, profile="": s3)

    with pytest.raises(CloudCommandError, match="already taken by another AWS account"):
        cloud_state.ensure_state_bucket("taken", "us-west-2")


def test_lock_table_has_the_shape_terraform_requires_and_is_waited_for(aws):
    cloud_state.ensure_lock_table("nyxgpt-tfstate-locks", "us-west-2")

    create = dict(aws["dynamodb"].calls)["create_table"]
    assert create["KeySchema"] == [{"AttributeName": "LockID", "KeyType": "HASH"}]
    assert create["AttributeDefinitions"] == [{"AttributeName": "LockID", "AttributeType": "S"}]
    assert create["BillingMode"] == "PAY_PER_REQUEST"
    # A CREATING table would fail the first apply's attempt to take the lock.
    assert aws["dynamodb"].waited == ["nyxgpt-tfstate-locks"]


def test_existing_lock_table_is_adopted(monkeypatch):
    dynamo = _FakeDynamo(fail_create="ResourceInUseException")
    monkeypatch.setattr(cloud_state, "_client", lambda service, region, profile="": dynamo)

    actions = cloud_state.ensure_lock_table("locks", "us-west-2")

    assert any("already exists" in action for action in actions)
    assert dynamo.waited == ["locks"]


def test_default_bucket_name_is_scoped_to_the_account_and_region(aws):
    """Bucket names are globally unique, so an unscoped default would collide."""
    settings = cloud_state.resolve_backend_settings(_args())
    assert settings.bucket == "nyxgpt-tfstate-123456789012-us-west-2"
    assert settings.table == cloud_state.DEFAULT_TABLE_NAME
    assert settings.key == cloud_state.DEFAULT_STATE_KEY


def test_backend_region_defaults_to_the_region_the_substrate_is_provisioned_into(aws):
    cloud_infra.save_settings(
        cloud_infra.InfraSettings(aws_region="eu-west-1", owner_ip_cidr="198.51.100.7/32")
    )
    settings = cloud_state.resolve_backend_settings(_args(region=None))
    assert settings.region == "eu-west-1"


# --- Migration -----------------------------------------------------------


def test_bootstrap_creates_the_backend_without_switching_to_it(aws, terraform_calls):
    result = cloud_state.bootstrap_backend(_args())

    assert result["backend"]["enabled"] is False
    assert cloud_state.remote_state_enabled() is False
    # Nothing has moved yet, so Terraform must not have been run at all.
    assert terraform_calls == []


def test_migrate_copies_existing_state_up_and_records_the_backend(aws, terraform_calls):
    cloud_infra.CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    cloud_infra.TFSTATE_FILE.write_text('{"version": 4}', encoding="utf-8")

    result = cloud_state.migrate_to_remote(_args())

    init = terraform_calls[0]
    assert init[0] == "init"
    # -force-copy is what makes the copy non-interactive; the bare
    # -migrate-state stops to ask, and a wrapped command has no one to ask.
    assert "-migrate-state" in init and "-force-copy" in init
    # The local state path must not be passed once state lives in S3.
    assert not any(arg.startswith("-backend-config=path=") for arg in init)

    assert result["migrated_local_state"] is True
    saved = json.loads(cloud_state.BACKEND_FILE.read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert saved["bucket"] == "nyxgpt-tfstate-123456789012-us-west-2"
    assert cloud_state.BACKEND_FILE.stat().st_mode & 0o777 == 0o600


def test_migrate_reports_when_there_was_no_local_state_to_copy(aws, terraform_calls):
    result = cloud_state.migrate_to_remote(_args())
    assert result["migrated_local_state"] is False
    assert any("starts empty" in action for action in result["actions"])


def test_a_failed_migration_leaves_backend_json_describing_where_state_really_is(aws, monkeypatch):
    """Otherwise every later command would init against a backend holding nothing."""

    def boom(arguments, *, capture=False, extra_env=None):
        raise CloudCommandError("`terraform init` failed")

    monkeypatch.setattr(cloud_infra, "_run_terraform", boom)

    with pytest.raises(CloudCommandError):
        cloud_state.migrate_to_remote(_args())

    assert cloud_state.remote_state_enabled() is False
    # And the next command's sync repairs backend.tf to match.
    cloud_infra.sync_terraform_config()
    backend_tf = cloud_infra.TERRAFORM_DIR / cloud_state.BACKEND_TF_NAME
    assert 'backend "local"' in backend_tf.read_text(encoding="utf-8")


def test_migrate_back_to_local_restores_the_local_state_path(aws, terraform_calls):
    cloud_state.migrate_to_remote(_args())
    terraform_calls.clear()

    result = cloud_state.migrate_to_local()

    init = terraform_calls[0]
    assert "-migrate-state" in init and "-force-copy" in init
    assert f"-backend-config=path={cloud_infra.TFSTATE_FILE}" in init
    assert result["backend"]["enabled"] is False
    # The AWS resources are deliberately left in place -- this changes where
    # Terraform reads state, not what exists in the account.
    assert result["backend"]["bucket"]


def test_migrate_back_to_local_refuses_when_state_is_already_local():
    with pytest.raises(CloudCommandError, match="already the local file"):
        cloud_state.migrate_to_local()


def test_init_reconfigures_when_the_backend_changed_outside_a_migration(aws, terraform_calls):
    """The state a half-finished migration leaves: recorded mode != configured mode."""
    cloud_infra.sync_terraform_config()
    cloud_infra.terraform_init()
    (cloud_infra.TERRAFORM_DIR / ".terraform" / "nyxgpt-backend.mode").write_text(
        "s3\n", encoding="utf-8"
    )
    terraform_calls.clear()

    cloud_infra.terraform_init()

    assert "-reconfigure" in terraform_calls[0]
    assert "-migrate-state" not in terraform_calls[0]


def test_init_keeps_passing_the_local_state_path_while_state_is_local(terraform_calls):
    cloud_infra.sync_terraform_config()
    cloud_infra.terraform_init()
    assert f"-backend-config=path={cloud_infra.TFSTATE_FILE}" in terraform_calls[0]


# --- Recovery ------------------------------------------------------------


def _enable_remote():
    cloud_state.save_backend_settings(
        cloud_state.BackendSettings(
            bucket="nyxgpt-tfstate-x", table="locks", region="us-west-2", enabled=True
        )
    )


def test_unlock_forces_the_named_lock_and_nothing_else(aws, terraform_calls):
    _enable_remote()
    result = cloud_state.unlock_state("abc-123")

    assert ["force-unlock", "-force", "abc-123"] in terraform_calls
    assert result["lock_id"] == "abc-123"


def test_unlock_requires_a_lock_id(aws, terraform_calls):
    """Releasing "whatever is held" would break a lock a live apply still owns."""
    _enable_remote()
    with pytest.raises(CloudCommandError, match="lock id is required"):
        cloud_state.unlock_state("   ")


def test_unlock_explains_itself_when_there_is_no_remote_backend(terraform_calls):
    with pytest.raises(CloudCommandError, match="Remote state is not configured"):
        cloud_state.unlock_state("abc-123")


def test_backup_writes_the_state_terraform_itself_would_read(terraform_calls, tmp_path):
    destination = tmp_path / "backup.tfstate"
    result = cloud_state.backup_state(destination)

    assert ["state", "pull"] in terraform_calls
    assert destination.read_text(encoding="utf-8") == '{"version": 4, "serial": 3}'
    assert result["path"] == str(destination)
    assert destination.stat().st_mode & 0o777 == 0o600


def test_backup_works_before_migration_too(terraform_calls):
    """`state pull` reads whichever backend is active, so this is backend-agnostic."""
    result = cloud_state.backup_state()
    assert result["path"] == str(cloud_infra.CLOUD_DIR / "terraform.tfstate.backup")


def test_versions_lists_only_this_state_object_newest_first(aws):
    _enable_remote()
    aws["s3"].versions = [
        {
            "Key": cloud_state.DEFAULT_STATE_KEY,
            "VersionId": "older",
            "LastModified": "2026-08-01T00:00:00",
            "Size": 10,
            "IsLatest": False,
        },
        {
            "Key": cloud_state.DEFAULT_STATE_KEY,
            "VersionId": "newest",
            "LastModified": "2026-08-09T00:00:00",
            "Size": 20,
            "IsLatest": True,
        },
        # A different object sharing the key prefix must not be offered as a
        # restore candidate for this state.
        {
            "Key": cloud_state.DEFAULT_STATE_KEY + ".backup",
            "VersionId": "unrelated",
            "LastModified": "2026-08-10T00:00:00",
            "Size": 30,
            "IsLatest": True,
        },
    ]

    result = cloud_state.list_state_versions()

    assert [v["version_id"] for v in result["versions"]] == ["newest", "older"]


def test_versions_respects_the_limit_and_flags_truncation(aws):
    _enable_remote()
    aws["s3"].versions = [
        {
            "Key": cloud_state.DEFAULT_STATE_KEY,
            "VersionId": f"v{i}",
            "LastModified": f"2026-08-{i + 1:02d}T00:00:00",
            "Size": 1,
            "IsLatest": False,
        }
        for i in range(5)
    ]

    result = cloud_state.list_state_versions(limit=2)

    assert len(result["versions"]) == 2
    assert result["truncated"] is True


def test_restore_pushes_the_old_version_back_through_terraform(aws, terraform_calls):
    """An out-of-band S3 overwrite would leave DynamoDB's checksum describing the replaced version."""
    _enable_remote()

    result = cloud_state.restore_state_version("older")

    assert dict(aws["s3"].calls)["get_object"]["VersionId"] == "older"
    push = next(call for call in terraform_calls if call[:2] == ["state", "push"])
    assert push[2] == "-force"
    assert result["version_id"] == "older"


def test_restore_requires_a_version_id(aws):
    _enable_remote()
    with pytest.raises(CloudCommandError, match="version id is required"):
        cloud_state.restore_state_version("")


# --- Status --------------------------------------------------------------


def test_status_is_offline_and_reports_local_state_before_any_migration(aws):
    status = cloud_state.state_status()

    assert status["backend"] == "local"
    assert status["remote_enabled"] is False
    assert status["bootstrapped"] is False
    assert status["verified"] is False
    assert status["local_state_file"] == str(cloud_infra.TFSTATE_FILE)
    # A pollable dashboard endpoint must not make AWS calls.
    assert aws["s3"].calls == []


def test_status_reports_the_backend_and_locking_once_migrated(aws, terraform_calls):
    cloud_state.migrate_to_remote(_args())

    status = cloud_state.state_status()

    assert status["backend"] == "s3"
    assert status["remote_enabled"] is True
    assert status["locking"] == "dynamodb"
    assert status["bucket"] == "nyxgpt-tfstate-123456789012-us-west-2"


def test_verify_confirms_the_bucket_table_and_versioning_against_aws(aws, terraform_calls):
    cloud_state.migrate_to_remote(_args())

    status = cloud_state.state_status(verify=True)

    assert status["verified"] is True
    assert status["bucket_exists"] is True
    assert status["table_exists"] is True
    assert status["table_status"] == "ACTIVE"
    # Versioning is the whole recovery story, so it is reported explicitly.
    assert status["versioning_enabled"] is True


def test_verify_reports_versioning_that_was_turned_off_after_the_fact(aws, terraform_calls):
    cloud_state.migrate_to_remote(_args())
    aws["s3"].versioning_status = "Suspended"

    assert cloud_state.state_status(verify=True)["versioning_enabled"] is False

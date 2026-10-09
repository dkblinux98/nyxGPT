"""Unit tests for the one AWS account / SSH key resolver (#4186).

Nothing here reaches AWS: `ec2_key_pairs` and `account_id` are the two calls
the module makes and both are replaced, so the tests assert on the resolution
order, the prompts, the fingerprint arithmetic and -- most of all -- that a
run with no terminal takes the defaults and never waits for input.

The fingerprints are the one part that has to be right against something
outside this suite, so they are pinned to vectors cross-checked against
`ssh-keygen -lf` and `openssl pkey -pubin -outform DER | openssl md5 -c`,
which is the arithmetic EC2 performs for an imported key pair.
"""

from __future__ import annotations

import argparse

import pytest

from nyxgpt import cloud_identity
from nyxgpt.cloud import CloudCommandError

# Throwaway keys generated for this suite and used nowhere else; only the
# public halves exist. The expected fingerprints beside them were produced by
# `ssh-keygen -lf` and by `ssh-keygen -e -m PKCS8 | openssl pkey -pubin
# -outform DER | openssl md5 -c` -- the second being exactly the arithmetic EC2
# performs for an *imported* RSA key pair, which is the format the owner's
# `nyxgpt-smoke-key` was reported in.
RSA_PUB = (
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCrScJTknLBO9RYN+xUIkemJEpAPQSat+CFbTgO"
    "XVMxB5XwJhSTLFcWsCodt8fZ07+GN6j6aa0IdQZ3tL/mEi+N7LqH2c58hbZWrA5c+VozXenlczjN"
    "EpHQZDyCh9VL7tJRhYzGWpeJDU9Zx4sDI7FgvIBvE8fiqCshBsSIjzUnRrE2FXzPhpybXDkqFv/4"
    "I0mBuLr0D1hlgDuBwC5dGz1kPl8GAza1zPRNsK0W3tS6OVDe691u7WWmd4yz9aFGF5T1HApzWYje"
    "2l8Yvyxandxe/BmdZpQqXKm5NCgcjfCL10o7VTA9gsiQsGl3DsI42DqrjSJW8SCQFO7tDW2sRez/"
    " test@example"
)
RSA_MD5_FINGERPRINT = "77:11:67:bd:3d:89:97:3e:e0:78:ae:49:11:c4:2c:5e"
RSA_SHA256_FINGERPRINT = "VKQlBtMCP/x8PCoidyRBV/RVsEjLHDqeBbDQVI95xqM"

ED25519_PUB = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINPCT/3T15EUuC0EVD+y6K9Pi8vkg6+CcNCfm93D"
    "g8PP test@example"
)
ED25519_SHA256_FINGERPRINT = "Uj0qmp2Gi55/iqNDfZi/cv/FGiMfJWH6JQ04Av24Sak"


# Captured at import, before the autouse fixture replaces them. A test that
# restored `cloud_identity.account_id` by reading the attribute would restore
# the fixture's stub and assert nothing -- which is how a best-effort test
# passes without ever reaching the code it is about.
_REAL_RECORDED_SETTINGS = cloud_identity.recorded_settings
_REAL_ACCOUNT_ID = cloud_identity.account_id
_REAL_EC2_KEY_PAIRS = cloud_identity.ec2_key_pairs


def _args(**overrides) -> argparse.Namespace:
    base = {
        "profile": None,
        "region": None,
        "ssh_key_name": None,
        "ssh_public_key": None,
        "identity_file": None,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _no_ambient_sources(monkeypatch, tmp_path):
    """Neutralise every source the resolver reads that the developer's machine owns.

    Without this the suite answers from whoever ran it -- their `infra.json`,
    their `config.ini [cloud]`, their `AWS_PROFILE`, their `~/.ssh` -- and
    passes or fails according to their AWS setup rather than the code.
    """
    cloud_identity.reset_prompt_cache()
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {})
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "", "region": ""}
    )
    monkeypatch.setattr(cloud_identity, "account_id", lambda profile, region="": "")
    monkeypatch.setattr(cloud_identity, "ec2_key_pairs", lambda profile, region: [])
    monkeypatch.setattr(cloud_identity, "SSH_DIR", tmp_path / "empty-ssh")
    for var in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(cloud_identity.NONINTERACTIVE_ENV, raising=False)


# --- Fingerprints --------------------------------------------------------


def test_ed25519_fingerprint_is_the_one_ssh_keygen_prints():
    """EC2 reports an ED25519 pair as the base64 SHA-256 of the OpenSSH blob.

    Pinned to the value `ssh-keygen -lf` printed for this key, not recomputed
    here: a test that redoes the implementation's arithmetic proves only that
    the code agrees with itself.
    """
    assert ED25519_SHA256_FINGERPRINT in cloud_identity.public_key_fingerprints(ED25519_PUB)


def test_rsa_fingerprint_includes_the_hex_md5_ec2_reports_for_an_imported_pair():
    """The format EC2 uses for an *imported* RSA pair: hex MD5 of the DER SPKI.

    This is the half that mattered on 2026-10-09: the owner's account held a
    pair whose fingerprint matched `~/.ssh/id_rsa.pub`, and matching it needs
    this format and not the SHA-256 one. Pinned to what `openssl md5 -c`
    produced over the key's DER SubjectPublicKeyInfo, which is the only check
    that can prove the hand-rolled DER re-encoding is right.
    """
    fingerprints = cloud_identity.public_key_fingerprints(RSA_PUB)

    assert RSA_MD5_FINGERPRINT in fingerprints
    assert RSA_SHA256_FINGERPRINT in fingerprints


def test_an_ed25519_key_gets_no_md5_form():
    """Only RSA has a DER SubjectPublicKeyInfo this module can rebuild."""
    assert not [f for f in cloud_identity.public_key_fingerprints(ED25519_PUB) if ":" in f]


def test_normalize_fingerprint_reconciles_ssh_keygen_and_ec2_decoration():
    assert cloud_identity.normalize_fingerprint("SHA256:abc=") == "abc"
    assert cloud_identity.normalize_fingerprint(" abc== ") == "abc"
    # Case is significant in base64 and must survive.
    assert cloud_identity.normalize_fingerprint("AbC") == "AbC"


def test_a_non_key_file_is_refused_rather_than_fingerprinted():
    with pytest.raises(CloudCommandError, match="not an OpenSSH public key"):
        cloud_identity.public_key_fingerprints("this is not a key")


# --- Account resolution --------------------------------------------------


def test_the_flag_wins_over_everything(monkeypatch):
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {"aws_profile": "recorded"})
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "configured", "region": ""}
    )
    monkeypatch.setenv("AWS_PROFILE", "env")

    account = cloud_identity.account_default(_args(profile="flag"))

    assert account.profile == "flag"
    assert account.source == cloud_identity.SOURCE_FLAG


def test_the_record_wins_over_config_and_environment(monkeypatch):
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {"aws_profile": "recorded"})
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "configured", "region": ""}
    )
    monkeypatch.setenv("AWS_PROFILE", "env")

    assert cloud_identity.account_default(_args()).profile == "recorded"


def test_config_ini_wins_over_the_environment(monkeypatch):
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "configured", "region": ""}
    )
    monkeypatch.setenv("AWS_PROFILE", "env")

    assert cloud_identity.account_default(_args()).profile == "configured"


def test_the_environment_is_the_last_source(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "env")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-3")

    account = cloud_identity.account_default(_args())

    assert (account.profile, account.region) == ("env", "eu-west-3")
    assert account.source == cloud_identity.SOURCE_ENVIRONMENT


def test_nothing_configured_resolves_to_boto3s_own_chain():
    account = cloud_identity.account_default(_args())

    assert account.profile == ""
    assert "default credential chain" in account.label


# The `prior` layer: a calling command's own record, below its flags and above
# everything shared. `nyxgpt cloud state` is the one that has one
# (`backend.json`), and hand-rolling the four steps beneath it is what left it
# two steps short of the documented order.


def test_a_caller_owned_prior_sits_below_the_flag_and_above_the_record(monkeypatch):
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {"aws_profile": "recorded"})
    prior = cloud_identity.AccountChoice(profile="prior", region="ap-south-1")

    # Below the flag.
    assert cloud_identity.account_default(_args(profile="flag"), prior=prior).profile == "flag"
    # Above infra.json.
    resolved = cloud_identity.account_default(_args(), prior=prior)
    assert (resolved.profile, resolved.region) == ("prior", "ap-south-1")


def test_a_prior_that_names_nothing_changes_nothing(monkeypatch):
    """`nyxgpt cloud state` with no `backend.json` yet must get the plain chain."""
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "configured", "region": ""}
    )
    empty = cloud_identity.AccountChoice()

    resolved = cloud_identity.account_default(_args(), prior=empty)

    assert resolved.profile == "configured"
    assert resolved.source == cloud_identity.SOURCE_CONFIG


def test_clearing_the_profile_is_named_in_the_prompt_rather_than_being_a_secret(monkeypatch):
    """`-` is the only way to say 'no named profile' where Enter keeps one.

    Undocumented, it is a magic value the operator has to be told about; named
    in the prompt, it is an option they can see (review of #4187).
    """
    monkeypatch.setattr(cloud_identity, "prompting_enabled", lambda args=None: True)
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {"aws_profile": "recorded"})
    asked: list[str] = []

    def fake_ask(prompt, default=""):
        asked.append(prompt)
        return "-"

    monkeypatch.setattr(cloud_identity, "ask", fake_ask)

    assert cloud_identity.resolve_account(_args()).profile == ""
    assert "- to use boto3's default credential chain" in asked[0]


def test_nothing_recorded_means_no_profile_to_clear_and_no_such_offer(monkeypatch):
    """The inverse: the wording is only shown where it would do something."""
    monkeypatch.setattr(cloud_identity, "prompting_enabled", lambda args=None: True)
    asked: list[str] = []
    monkeypatch.setattr(
        cloud_identity, "ask", lambda prompt, default="": asked.append(prompt) or ""
    )

    cloud_identity.resolve_account(_args())

    assert "- to use" not in asked[0]
    assert "Enter to use boto3's default credential chain" in asked[0]


def test_a_prior_value_is_still_offered_for_confirmation(monkeypatch):
    """Only a flag skips the question -- a record is a default, however close."""
    monkeypatch.setattr(cloud_identity, "prompting_enabled", lambda args=None: True)
    monkeypatch.setattr(cloud_identity, "ask", lambda prompt, default="": "typed")

    resolved = cloud_identity.resolve_account(
        _args(), prior=cloud_identity.AccountChoice(profile="prior")
    )

    assert resolved.profile == "typed"


# --- The recorded-account label (one copy of four branches, D-066) --------


def test_the_recorded_account_label_names_the_profile_and_the_id():
    assert cloud_identity.recorded_account_label("nyxgpt", "066835328281") == (
        "nyxgpt (066835328281)"
    )


def test_the_recorded_account_label_is_explicit_about_what_it_does_not_know():
    """A blank reads as 'no profile', which is a claim about the deployment.

    'not recorded here' is a claim about *this machine's knowledge* of it,
    which is the true one on the instance and in an api Pod.
    """
    assert cloud_identity.recorded_account_label("", "") == "not recorded here"
    assert cloud_identity.recorded_account_label("nyxgpt", "") == "nyxgpt (account id not recorded)"
    assert "default credential chain" in cloud_identity.recorded_account_label("", "066835328281")


def test_the_recorded_label_is_not_the_resolve_time_label():
    """Two different claims, deliberately worded differently.

    `AccountChoice.label` says "account id not resolved" -- an STS call was
    made and failed. The recorded form says "not recorded" -- nobody here ever
    wrote one down. Collapsing them would report a failed lookup as a missing
    record and vice versa.
    """
    resolved = cloud_identity.AccountChoice(profile="nyxgpt").label
    recorded = cloud_identity.recorded_account_label("nyxgpt", "")

    assert "not resolved" in resolved
    assert "not recorded" in recorded
    assert resolved != recorded


def test_a_recorded_account_id_is_only_reused_for_the_profile_it_was_recorded_for(monkeypatch):
    """An id recorded for one profile describes a different account than another.

    Carrying it across would print a confident `other (066835328281)` for an
    account nothing checked -- the exact invisibility the label exists to end.
    """
    monkeypatch.setattr(
        cloud_identity,
        "recorded_settings",
        lambda: {"aws_profile": "recorded", "aws_account_id": "111122223333"},
    )

    assert cloud_identity.account_default(_args()).account_id == "111122223333"
    assert cloud_identity.account_default(_args(profile="other")).account_id == ""


def test_the_label_names_the_profile_and_the_account_it_resolves_to(monkeypatch):
    monkeypatch.setattr(cloud_identity, "account_id", lambda profile, region="": "066835328281")

    account = cloud_identity.resolve_account(_args(profile="nyxgpt"), interactive=False)

    assert account.label == "nyxgpt (066835328281)"


def test_the_label_says_so_when_the_account_id_could_not_be_read():
    account = cloud_identity.resolve_account(_args(profile="nyxgpt"), interactive=False)

    assert account.label == "nyxgpt (account id not resolved)"


def test_the_account_prompt_offers_the_resolved_value_as_the_default(monkeypatch):
    """Enter accepts the default, and the default shown is the whole answer.

    The issue is specific about this: the bracketed default is the profile
    *and* the account id it resolves to, so the account cannot be accepted
    without having been seen.
    """
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "nyxgpt", "region": "us-east-1"}
    )
    monkeypatch.setattr(cloud_identity, "account_id", lambda profile, region="": "066835328281")
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")

    account = cloud_identity.resolve_account(_args(), interactive=True)

    assert account.profile == "nyxgpt"
    assert prompts == [
        "AWS profile (or - to use boto3's default credential chain) " "[nyxgpt (066835328281)]: "
    ]


def test_the_prompt_says_in_words_what_enter_does_with_no_profile_resolved(monkeypatch):
    """`[(none)]` would leave the operator guessing what Enter is about to do."""
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")

    account = cloud_identity.resolve_account(_args(), interactive=True)

    assert account.profile == ""
    assert prompts == ["AWS profile (Enter to use boto3's default credential chain): "]


def test_a_typed_answer_overrides_the_default(monkeypatch):
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "nyxgpt", "region": ""}
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "other")

    account = cloud_identity.resolve_account(_args(), interactive=True)

    assert account.profile == "other"
    assert account.source == cloud_identity.SOURCE_PROMPT


def test_an_explicit_profile_flag_is_never_questioned(monkeypatch):
    def _refuse(prompt):
        raise AssertionError(f"prompted despite --profile: {prompt!r}")

    monkeypatch.setattr("builtins.input", _refuse)

    assert cloud_identity.resolve_account(_args(profile="flag"), interactive=True).profile == "flag"


# --- Non-interactive safety ----------------------------------------------


def test_yes_disables_prompting():
    assert cloud_identity.prompting_enabled(_args(yes=True)) is False


def test_the_environment_override_disables_prompting(monkeypatch):
    monkeypatch.setenv(cloud_identity.NONINTERACTIVE_ENV, "1")

    assert cloud_identity.prompting_enabled(_args()) is False


def test_no_tty_disables_prompting(monkeypatch):
    """CI and `nyxgpt cloud ops` over SSH: no terminal, so no question."""
    monkeypatch.setattr(cloud_identity.sys, "stdin", _NotATty())
    monkeypatch.setattr(cloud_identity.sys, "stdout", _NotATty())

    assert cloud_identity.prompting_enabled(_args()) is False


def test_a_closed_stream_is_not_a_terminal_and_is_not_a_crash(monkeypatch):
    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(cloud_identity.sys, "stdin", _Closed())

    assert cloud_identity.prompting_enabled(_args()) is False


class _NotATty:
    """Stand-in for a redirected stream."""

    def isatty(self) -> bool:
        return False


def test_the_non_interactive_failure_names_every_missing_input_and_its_flag():
    error = cloud_identity.missing_inputs_error(
        [("an SSH key", "--ssh-key-name <pair>"), ("a region", "--region <region>")]
    )
    message = str(error)

    assert "--ssh-key-name <pair>" in message
    assert "--region <region>" in message
    # Both at once, not the first one found: a scripted run corrected one flag
    # per failed invocation pays an AWS round trip per round.
    assert message.count("pass ") == 2


def test_no_prompt_or_error_cites_a_repository_path(tmp_path, monkeypatch):
    """#4182: the operator reading this has no checkout to open."""
    monkeypatch.setattr(cloud_identity, "SSH_DIR", tmp_path / "empty")
    with pytest.raises(CloudCommandError) as excinfo:
        cloud_identity.resolve_ssh(_args(), interactive=False)

    message = str(excinfo.value)
    assert "product_management/" not in message
    assert "docs/" not in message


def test_the_announcement_prints_the_account_and_the_key(capsys):
    account = cloud_identity.AccountChoice(
        profile="nyxgpt", region="us-east-1", account_id="066835328281"
    )
    ssh = cloud_identity.SshChoice(key_name="nyxgpt-smoke-key", identity_file="/home/x/.ssh/id_rsa")

    line = cloud_identity.announce(account, ssh)

    assert "nyxgpt (066835328281)" in line
    assert "us-east-1" in line
    assert "nyxgpt-smoke-key" in line
    assert line in capsys.readouterr().out


# --- SSH resolution ------------------------------------------------------


def _write_key(ssh_dir, name: str, material: str) -> None:
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / name).write_text(material + "\n", encoding="utf-8")


def test_the_recorded_key_is_the_first_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cloud_identity, "recorded_settings", lambda: {"ssh_key_name": "from-infra-json"}
    )
    _write_key(tmp_path / "ssh", "id_ed25519.pub", ED25519_PUB)

    choice = cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=tmp_path / "ssh")

    assert choice.key_name == "from-infra-json"
    assert choice.source == cloud_identity.SOURCE_RECORD


def test_a_recorded_key_costs_no_account_scan(monkeypatch, tmp_path):
    """First principle 1: do not pay a round trip to confirm what the record said."""
    monkeypatch.setattr(cloud_identity, "recorded_settings", lambda: {"ssh_key_name": "recorded"})

    def _refuse(profile, region):
        raise AssertionError("scanned the account despite a recorded key")

    monkeypatch.setattr(cloud_identity, "ec2_key_pairs", _refuse)

    assert (
        cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=tmp_path / "ssh").key_name
        == "recorded"
    )


def test_a_fingerprint_matched_key_pair_is_offered_by_name_with_the_file_it_matches(
    monkeypatch, tmp_path
):
    """The owner's 2026-10-09 case, which took a hand-run fingerprint comparison.

    The account held a pair matching a local public key and nothing in nyxGPT
    would say so -- the deploy stopped instead, with the answer already on the
    machine.
    """
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_rsa.pub", RSA_PUB)
    (ssh_dir / "id_rsa").write_text("not a real key\n", encoding="utf-8")
    monkeypatch.setattr(
        cloud_identity,
        "ec2_key_pairs",
        # Exactly the shape `describe_key_pairs` returns for an imported pair.
        lambda profile, region: [{"name": "nyxgpt-smoke-key", "fingerprint": RSA_MD5_FINGERPRINT}],
    )

    choice = cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=ssh_dir)

    assert choice.key_name == "nyxgpt-smoke-key"
    assert choice.source == cloud_identity.SOURCE_MATCHED_KEY_PAIR
    assert choice.public_key_path == str(ssh_dir / "id_rsa.pub")
    # And the private half beside it, so no --identity-file is needed either.
    assert choice.identity_file == str(ssh_dir / "id_rsa")
    assert "nyxgpt-smoke-key" in choice.label
    assert "id_rsa.pub" in choice.label


def test_an_unmatched_key_pair_is_not_offered(monkeypatch, tmp_path):
    """A pair EC2 created is fingerprinted from its private half and cannot match."""
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    monkeypatch.setattr(
        cloud_identity,
        "ec2_key_pairs",
        lambda profile, region: [{"name": "created-by-ec2", "fingerprint": "aa:bb:cc"}],
    )

    choice = cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=ssh_dir)

    assert choice.key_name == ""
    assert choice.source == cloud_identity.SOURCE_LOCAL_PUBLIC_KEY
    assert choice.public_key == ED25519_PUB


def test_a_local_public_key_is_the_last_candidate(tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)

    choice = cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=ssh_dir)

    assert choice.public_key == ED25519_PUB
    assert "register" in choice.label


def test_modern_key_types_are_preferred_over_rsa(tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_rsa.pub", RSA_PUB)
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)

    assert cloud_identity.local_public_keys(ssh_dir)[0].name == "id_ed25519.pub"


def test_a_stray_file_in_ssh_is_skipped_rather_than_offered(tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "known_hosts.pub", "not a key at all")
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)

    choice = cloud_identity.resolve_ssh(_args(), interactive=False, ssh_dir=ssh_dir)

    assert choice.public_key == ED25519_PUB


def test_both_ssh_flags_together_are_refused():
    with pytest.raises(CloudCommandError, match="not both"):
        cloud_identity.resolve_ssh(
            _args(ssh_key_name="pair", ssh_public_key="k.pub"), interactive=False
        )


def test_an_explicit_key_name_is_never_questioned(monkeypatch):
    def _refuse(prompt):
        raise AssertionError(f"prompted despite --ssh-key-name: {prompt!r}")

    monkeypatch.setattr("builtins.input", _refuse)

    choice = cloud_identity.resolve_ssh(_args(ssh_key_name="pair"), interactive=True)

    assert (choice.key_name, choice.source) == ("pair", cloud_identity.SOURCE_FLAG)


def test_an_explicit_public_key_brings_its_private_half_along(tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    (ssh_dir / "id_ed25519").write_text("not a real key\n", encoding="utf-8")

    choice = cloud_identity.resolve_ssh(
        _args(ssh_public_key=str(ssh_dir / "id_ed25519.pub")), interactive=False
    )

    assert choice.public_key == ED25519_PUB
    assert choice.identity_file == str(ssh_dir / "id_ed25519")


def test_the_ssh_prompt_lists_the_candidates_and_enter_takes_the_first(monkeypatch, tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    _write_key(ssh_dir, "id_rsa.pub", RSA_PUB)
    answers: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "")

    choice = cloud_identity.resolve_ssh(_args(), interactive=True, ssh_dir=ssh_dir)

    assert choice.public_key == ED25519_PUB
    # Two questions: which key, then which private half to authenticate with.
    assert len(answers) == 2
    assert answers[0].startswith("Choice [1]")
    assert answers[1].startswith("Private key to authenticate with")


def test_the_ssh_prompt_takes_a_chosen_alternative(monkeypatch, tmp_path):
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    _write_key(ssh_dir, "id_rsa.pub", RSA_PUB)
    replies = iter(["2", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))

    choice = cloud_identity.resolve_ssh(_args(), interactive=True, ssh_dir=ssh_dir)

    assert choice.public_key == RSA_PUB


def test_an_unparseable_choice_is_re_asked_rather_than_silently_defaulted(monkeypatch, tmp_path):
    """A typo'd key choice would hand the instance a key the operator did not pick."""
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    _write_key(ssh_dir, "id_rsa.pub", RSA_PUB)
    replies = iter(["x", "9", "2", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))

    assert cloud_identity.resolve_ssh(_args(), interactive=True, ssh_dir=ssh_dir).public_key == (
        RSA_PUB
    )


def test_a_bounded_number_of_retries_then_the_default(monkeypatch):
    """An unbounded re-ask loop is its own way to hang a command."""
    monkeypatch.setattr("builtins.input", lambda prompt: "nonsense")

    assert cloud_identity.ask_choice("Choice", ["a", "b"]) == 0


def test_eof_at_a_prompt_takes_the_default(monkeypatch):
    def _eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert cloud_identity.ask("AWS profile", "nyxgpt") == "nyxgpt"


# --- Best-effort lookups -------------------------------------------------


def test_a_failing_key_pair_scan_is_an_empty_list_not_an_error(monkeypatch):
    """This only ever improves a default, so no failure here may become the error."""

    class _Angry:
        def describe_key_pairs(self):
            raise RuntimeError("ExpiredToken")

    monkeypatch.setattr(cloud_identity, "ec2_key_pairs", _REAL_EC2_KEY_PAIRS)
    monkeypatch.setattr(cloud_identity, "_client", lambda service, profile, region: _Angry())

    assert cloud_identity.ec2_key_pairs("nyxgpt", "us-east-1") == []


def test_a_failing_account_lookup_is_an_empty_string_not_an_error(monkeypatch):
    class _Angry:
        def get_caller_identity(self):
            raise RuntimeError("NoCredentialProviders")

    monkeypatch.setattr(cloud_identity, "account_id", _REAL_ACCOUNT_ID)
    monkeypatch.setattr(cloud_identity, "_client", lambda service, profile, region: _Angry())
    cloud_identity._ACCOUNT_ID_CACHE.clear()

    assert cloud_identity.account_id("nyxgpt", "us-east-1") == ""


def test_the_account_lookup_is_made_once_per_process(monkeypatch):
    """One STS round trip is the budget; the resolver is called by more than one layer."""
    calls: list[int] = []

    class _Counting:
        def get_caller_identity(self):
            calls.append(1)
            return {"Account": "066835328281"}

    monkeypatch.setattr(cloud_identity, "account_id", _REAL_ACCOUNT_ID)
    monkeypatch.setattr(cloud_identity, "_client", lambda service, profile, region: _Counting())
    cloud_identity._ACCOUNT_ID_CACHE.clear()

    assert cloud_identity.account_id("nyxgpt", "us-east-1") == "066835328281"
    assert cloud_identity.account_id("nyxgpt", "us-east-1") == "066835328281"
    assert len(calls) == 1


def test_a_recorded_settings_read_that_blows_up_is_a_missing_default(monkeypatch):
    """A corrupt `infra.json` is a missing default, never a reason a command cannot run."""
    from nyxgpt import cloud_infra

    def _angry():
        raise OSError("disk went away")

    # The autouse fixture stubs this out; restore the real one to exercise it.
    monkeypatch.setattr(cloud_identity, "recorded_settings", _REAL_RECORDED_SETTINGS)
    monkeypatch.setattr(cloud_infra, "load_settings", _angry)

    assert cloud_identity.recorded_settings() == {}


# --- Asked once per command ----------------------------------------------


def test_a_typed_answer_is_not_asked_for_again(monkeypatch, tmp_path):
    """One `cloud deploy --os macos` resolves settings four times.

    `cloud_mac` does it three times (pricing, plan, launch) and
    `cloud_deploy` then calls `apply_infra`. Without the memo the operator is
    asked the same two questions four times -- and a Dedicated Host
    allocation is not a flow anyone should be made to re-answer mid-way.
    """
    ssh_dir = tmp_path / "ssh"
    _write_key(ssh_dir, "id_ed25519.pub", ED25519_PUB)
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "")

    first = cloud_identity.resolve_ssh(_args(), interactive=True, ssh_dir=ssh_dir)
    asked_once = len(asked)
    second = cloud_identity.resolve_ssh(_args(), interactive=True, ssh_dir=ssh_dir)

    assert second == first
    assert len(asked) == asked_once, asked


def test_an_explicit_flag_still_beats_a_remembered_answer(monkeypatch):
    monkeypatch.setattr(
        cloud_identity, "configured_reference", lambda: {"profile": "nyxgpt", "region": ""}
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "typed")

    cloud_identity.resolve_account(_args(), interactive=True)

    assert cloud_identity.resolve_account(_args(profile="flag"), interactive=True).profile == (
        "flag"
    )


def test_an_identical_announcement_is_printed_once(capsys):
    """Four identical lines read as four decisions; a different line always prints."""
    account = cloud_identity.AccountChoice(profile="nyxgpt", account_id="066835328281")
    other = cloud_identity.AccountChoice(profile="other", account_id="999988887777")

    cloud_identity.announce(account)
    cloud_identity.announce(account)
    cloud_identity.announce(other)

    printed = capsys.readouterr().out
    assert printed.count("nyxgpt (066835328281)") == 1
    assert printed.count("other (999988887777)") == 1

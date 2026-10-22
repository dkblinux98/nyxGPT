# Plan-level proof that two Dedicated Hosts get two independent stacks (#4200).
#
# **The question.** `nyxgpt cloud destroy` hands the deferred host release to
# this root module. It used to be applied into ONE state file with ONE fixed
# `name_prefix`, so scheduling a second host's release did not create a second
# schedule -- Terraform found the existing `aws_scheduler_schedule.release` and
# updated it in place to point at the new host. The first host, still allocated,
# still inside AWS's 24-hour minimum and still billing about $15.60 a day, was
# left with nothing scheduled to release it; and because the schedule that would
# have sent the Slack message now named the other host, nothing said so.
#
# So the property this file pins is: **the names are the host's.** Two host ids
# must produce two sets of AWS resource names with nothing in common. Separate
# Terraform state is the other half and belongs to the caller
# (`cloud_mac.release_paths`), where `tests/unit/test_cloud_mac.py` and the
# `two-hosts` scenario of `scripts/cloud_stale_record_smoke.py` assert it.
#
# **Both halves are proved** (#3753's fault-injection rule). `name_prefix` is an
# input, so a job that only checks the per-host naming passes on a build whose
# caller still composes one fixed prefix. The last run block passes the pre-#4200
# prefix for both hosts and REQUIRES the collision to reproduce: identical
# schedule names, which is a second apply retargeting the first host's schedule.
#
# Offline, and with the REAL provider. `data.aws_caller_identity.current` is
# the only thing here that would reach AWS, so it is the only thing overridden:
# a whole `mock_provider "aws"` would also stub `aws_iam_policy_document`, whose
# rendered JSON is a pure local computation that `aws_iam_role` then validates --
# mocking it makes the policies empty and the plan fail. Everything else needs
# no account: `command = plan` creates nothing and the skip_* flags below keep
# the provider from checking its own credentials. Run by
# .github/workflows/terraform-aws-validate.yml.

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "123456789012"
  }
}

variables {
  aws_region                 = "us-east-1"
  release_at                 = "2026-10-22T18:30:00"
  slack_channel              = "C0TESTCHANNEL"
  slack_authorization_header = "Bearer xoxb-plan-only-never-sent"

  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

# The two host ids, with the per-host prefix `cloud_mac._release_name_prefix`
# composes: `<settings name_prefix>-mac-<the host id minus its h- prefix>`.
run "first_host_names_only_itself" {
  command = plan

  variables {
    host_id     = "h-0aaa1111aaaa1111a"
    name_prefix = "nyxgpt-tf-mac-0aaa1111aaaa1111a"
  }

  assert {
    condition     = output.resource_names.schedule == "nyxgpt-tf-mac-0aaa1111aaaa1111a-release"
    error_message = "The schedule's name must carry this host's id, or a second host's apply retargets it."
  }

  assert {
    condition     = output.host_id == "h-0aaa1111aaaa1111a"
    error_message = "The stack must release the host it was told to release."
  }

  # AWS caps EventBridge schedule names and IAM role names at 64 characters. An
  # overflow fails the apply, and a failed apply here means no release schedule
  # for a host that is already billing -- the defect, by another route.
  assert {
    condition = alltrue([
      for name in values(output.resource_names) : length(name) <= 64
    ])
    error_message = "A composed resource name exceeds AWS's 64-character limit for schedule and IAM role names."
  }
}

run "second_host_names_only_itself" {
  command = plan

  variables {
    host_id     = "h-0bbb2222bbbb2222b"
    name_prefix = "nyxgpt-tf-mac-0bbb2222bbbb2222b"
  }

  assert {
    condition     = output.resource_names.schedule == "nyxgpt-tf-mac-0bbb2222bbbb2222b-release"
    error_message = "The schedule's name must carry this host's id."
  }

  # Nothing in common with the first host's stack. Checked name by name rather
  # than on the schedule alone: the state machine, the two IAM roles and the
  # EventBridge connection are all `name_prefix`-derived, and a collision on any
  # one of them makes the second apply modify the first host's stack.
  assert {
    condition = length(setintersection(
      toset(values(output.resource_names)),
      toset([
        "nyxgpt-tf-mac-0aaa1111aaaa1111a-release",
        "nyxgpt-tf-mac-0aaa1111aaaa1111a-release-sfn",
        "nyxgpt-tf-mac-0aaa1111aaaa1111a-slack",
        "nyxgpt-tf-mac-0aaa1111aaaa1111a-release-scheduler",
      ])
    )) == 0
    error_message = "Two hosts' stacks share an AWS resource name, so applying the second modifies the first."
  }

  assert {
    condition     = strcontains(output.resource_names.schedule, "0bbb2222bbbb2222b")
    error_message = "The schedule name must be derived from the host id, not from a fixed string."
  }
}

# --- The negative control: the pre-#4200 naming must still collide ---------
#
# One fixed prefix for every host, which is what this module was always applied
# with. Both hosts compose the SAME schedule name, so the second `terraform
# apply` finds the first host's schedule and retargets it. If this run block
# ever stops reproducing that, the two above prove nothing about the fix -- they
# would be passing on a module whose names cannot collide for some other reason.
run "the_pre_fix_prefix_names_the_first_host_stack_generically" {
  command = plan

  variables {
    host_id     = "h-0aaa1111aaaa1111a"
    name_prefix = "nyxgpt-tf-mac"
  }

  assert {
    condition     = output.resource_names.schedule == "nyxgpt-tf-mac-release"
    error_message = "The pre-#4200 prefix no longer produces the colliding name, so the control is vacuous."
  }
}

run "and_the_second_host_composes_the_identical_name_which_is_the_defect" {
  command = plan

  variables {
    host_id     = "h-0bbb2222bbbb2222b"
    name_prefix = "nyxgpt-tf-mac"
  }

  # The same name as the run above, for a DIFFERENT host. That is the whole
  # defect in one assertion: `terraform apply` finds
  # `aws_scheduler_schedule.release` already named `nyxgpt-tf-mac-release` and
  # updates it to release h-0bbb..., and h-0aaa... -- allocated, billing, inside
  # its 24-hour minimum -- is left with no release scheduled at all.
  assert {
    condition     = output.resource_names.schedule == "nyxgpt-tf-mac-release"
    error_message = "Expected the pre-fix naming to collide for two hosts; it no longer does, so the per-host assertions above prove nothing."
  }

  assert {
    condition     = !strcontains(output.resource_names.schedule, "0bbb2222bbbb2222b")
    error_message = "Expected the pre-fix name to carry no host id at all -- that is what made it collide."
  }
}

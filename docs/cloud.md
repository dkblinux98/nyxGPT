# nyxGPT Cloud (AWS)

`nyxgpt cloud` is the CLI surface for AWS-deployed nyxGPT stacks (P6-11-class
scope). It currently covers `allow-ip` (#3630); provisioning and teardown
(`nyxgpt cloud deploy`/`destroy`, #3513) land separately.

Install the AWS SDK dependency with:

```bash
pip install "nyxgpt[cloud]"
```

`boto3` is kept out of the base install -- it's only needed for AWS
deployments, not the local stack every other `nyxgpt` command drives.

---

## Background: the owner-IP-scoped SSH rule

Per
[`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`](../product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md),
an AWS-deployed nyxGPT instance is reached only over an SSH tunnel
(`nyxgpt cloud tunnel`, forthcoming): the API, web UI, and every
observability endpoint bind to `127.0.0.1` on the instance and are never
opened in the security group. The security group allows exactly one inbound
rule -- TCP port 22, scoped to the owner's current public IP, never
`0.0.0.0/0`.

The tradeoff: when the owner's IP changes (ISP renewal, travel, mobile
tethering), that rule goes stale and the instance becomes unreachable,
**including over SSH** -- there is no other way in. `nyxgpt cloud allow-ip`
exists to fix exactly this, and does so by talking only to the AWS EC2 API,
never the instance, so it works from the new IP while still locked out.

---

## `nyxgpt cloud allow-ip`

Refreshes the security group's port-22 ingress rule to the caller's current
public IP.

```bash
nyxgpt cloud allow-ip
```

What it does:

1. Detects the caller's current public IP (via `https://checkip.amazonaws.com`,
   AWS's own IP-echo endpoint -- no third-party dependency).
2. Resolves the target security group: `--security-group-id` if given,
   otherwise `~/.nyxGPT/cloud/state.json`'s `security_group_id` (written by
   `nyxgpt cloud deploy`).
3. Revokes every existing port-22 ingress CIDR that doesn't match the new
   IP, and authorizes the new one -- unless it's already the only allowed
   source, in which case the command is a no-op (idempotent).
4. Prints the old and new source CIDR.

### Options

| Flag | Description |
| --- | --- |
| `--ip <addr>` | Use this IP or CIDR instead of auto-detecting the caller's current public IP. A bare address (no `/`) is scoped to `/32`; an explicit CIDR is kept as passed. `0.0.0.0/0` is always refused. |
| `--security-group-id <id>` | Security group to update. Defaults to `~/.nyxGPT/cloud/state.json`'s `security_group_id`. |
| `--region <region>` | AWS region. Defaults to `~/.nyxGPT/cloud/state.json`'s `region`, then boto3's normal region resolution (`AWS_REGION`/`AWS_DEFAULT_REGION`/profile config). |

### Example

```bash
$ nyxgpt cloud allow-ip
Security group sg-0123456789abcdef0: SSH ingress rule updated.
  old: 198.51.100.7/32
  new: 203.0.113.42/32

$ nyxgpt cloud allow-ip
Security group sg-0123456789abcdef0: SSH already allowed from 203.0.113.42/32 -- no change.
```

### Credentials

`allow-ip` uses boto3's normal credential resolution (environment variables,
`~/.aws/credentials`, an instance/SSO profile, ...) -- it does not collect or
store AWS credentials itself. Guided credential collection is separate scope
(P6-13).

---

## Note for the AWS Terraform module (P6-8)

`allow-ip` mutates the security group's port-22 ingress rule directly via
the AWS API, outside of Terraform. When the AWS Terraform module lands, its
security-group resource must not fight that: give the ingress rule a
`lifecycle { ignore_changes = [ingress] }` (or manage it as a separate
`aws_security_group_rule` excluded from the plan) so a routine
`terraform apply` doesn't revert an `allow-ip` refresh back to a stale IP.
The module should also write `security_group_id` and `region` to
`~/.nyxGPT/cloud/state.json` on apply, so `allow-ip` can auto-discover its
target without `--security-group-id`/`--region`.

## Lockout recovery

If you're locked out of an AWS-deployed instance because the SSH rule no
longer matches your current IP:

1. **First resort:** run `nyxgpt cloud allow-ip` from the machine with the
   new IP. It only needs AWS API credentials, not access to the instance
   itself, so it works even though SSH is currently refused.
2. **Fallback (no local AWS credentials available):** update the security
   group's port-22 ingress rule directly from the AWS Console (EC2 →
   Security Groups → the deployment's group → Edit inbound rules), or the
   AWS CLI (`aws ec2 authorize-security-group-ingress` /
   `revoke-security-group-ingress`) from any machine with credentials for
   the account. Scope the new rule to your current public IP only -- never
   `0.0.0.0/0`.

Once the rule is refreshed, `nyxgpt cloud tunnel` (or a direct
`ssh -L ...`, see
[`docs/security.md#network-security`](security.md#network-security)) reaches
the instance again.

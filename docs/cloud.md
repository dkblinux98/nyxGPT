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
store AWS credentials itself. See "Guided AWS credentials setup" below for
how to get a profile in place.

---

## Guided AWS credentials setup (P6-13, #3512)

Every `nyxgpt cloud` command (and `[secrets] provider = ssm`/`secretsmanager`
above) ultimately calls boto3, which needs AWS credentials available
somewhere. `nyxgpt cloud credentials-setup` (CLI) and the `/admin` **AWS
Credentials** wizard (web) walk through getting a profile in place, with the
same masked-entry, what-it-is/where-to-get-it treatment as the guided
secrets flow (#3505) -- but the AWS access key ID/secret access key
collected here are **never written to `config.ini`**. They're routed
instead to one of:

| Destination | Where the key pair goes |
| --- | --- |
| `profile` (default) | `~/.aws/credentials`, under the chosen profile name -- exactly what `aws configure --profile <name>` would produce |
| `keychain` | The OS keychain, via the optional `keyring` package (`pip install nyxgpt[cloud]`) |
| `ambient` | Nowhere -- credentials are already available some other way (an existing profile, an EC2 instance role, an SSO session, environment variables) and nothing is written |

Only the non-secret *reference* -- profile name, region, and which
destination was chosen -- is written to `config.ini`'s `[cloud]` section, so
`cloud.py`/`cloud_secrets.py` can find it again:

```ini
[cloud]
profile = nyxgpt
region = us-east-1
credentials_source = profile
```

```bash
$ nyxgpt cloud credentials-setup
============================================================
nyxGPT Guided AWS Credentials Setup
============================================================
AWS profile name [nyxgpt]:
AWS region [us-east-1]:

How should nyxGPT get AWS credentials?
  1) Enter an access key pair -- written to ~/.aws/credentials
  2) Enter an access key pair -- stored in the OS keychain instead of a file
  3) Already configured elsewhere (existing profile, instance role, SSO, env vars)
Choice [1]: 1
AWS access key ID:
AWS secret access key:

Saved -- profile='nyxgpt' region='us-east-1' destination='profile'.
Access key written to /home/you/.aws/credentials under [nyxgpt].
```

The same flow optionally walks through the `[secrets]` provider reference
above (provider/region/ssm_prefix/secretsmanager_id) in one pass, so a
cloud-deploy setup doesn't need a separate detour through the general
Configuration Wizard -- those fields aren't secret values themselves (the
actual application secrets stay in SSM/Secrets Manager), just which store to
use.

The `/admin` **AWS Credentials** wizard (`web/src/app/admin/aws-credentials`)
is the same flow's web surface: `GET /api/v1/config/aws-credentials` reports
current status (masked, never cleartext), `POST /api/v1/config/aws-credentials`
saves a key pair to the chosen destination, and
`POST /api/v1/config/aws-credentials/secret-store` saves the `[secrets]`
reference. It seeds the same defaults the CLI offers (`nyxgpt` profile,
`us-east-1` region, "AWS CLI profile file" destination) when nothing has been
saved yet, so both surfaces produce identical results on a fresh install.

---

## Cloud secrets (SSM / Secrets Manager)

On a cloud (AWS) deploy, `[auth] api_key`, `[openai] api_key`, and
`[github] pat` must never be baked into an AMI, user-data script, tfvars
file, or `config.ini` itself (P6-10, #3507). Set `[secrets] provider` in
`config.ini` and nyxGPT resolves those three credentials from AWS at read
time instead:

```ini
[secrets]
provider = ssm            # or "secretsmanager"
region = us-east-1        # optional -- falls back to boto3's normal region resolution
ssm_prefix = /nyxgpt       # provider = ssm
secretsmanager_id = nyxgpt # provider = secretsmanager
```

Leaving `provider` blank (the default) is a local deploy: the three
credentials are read from `config.ini` exactly as before, unaffected.

### SSM Parameter Store layout (`provider = ssm`)

One `SecureString` parameter per credential, under `ssm_prefix`:

| Parameter | Value |
|---|---|
| `{ssm_prefix}/auth_api_key` | The shared secret checked by `[auth] enabled` middleware |
| `{ssm_prefix}/openai_api_key` | OpenAI API key |
| `{ssm_prefix}/github_pat` | GitHub Personal Access Token |

```bash
aws ssm put-parameter --name /nyxgpt/auth_api_key --type SecureString --value "..."
aws ssm put-parameter --name /nyxgpt/openai_api_key --type SecureString --value "..."
aws ssm put-parameter --name /nyxgpt/github_pat --type SecureString --value "..."
```

Only credentials actually used need to be set -- a missing/unreadable
parameter resolves to an empty value for that credential (see "Failure
behavior" below), not an error that blocks the others.

### Secrets Manager layout (`provider = secretsmanager`)

One secret, at `secretsmanager_id`, holding a single JSON object with all
three keys:

```bash
aws secretsmanager create-secret --name nyxgpt --secret-string '{
  "auth_api_key": "...",
  "openai_api_key": "...",
  "github_pat": "..."
}'
```

Secrets Manager bills per secret rather than per value, so one secret with
several keys is the natural fit here (unlike SSM, which is priced and
structured per parameter).

### IAM permissions

The instance role needs read access to whichever provider is configured:

- `provider = ssm`: `ssm:GetParameter` on the `ssm_prefix` path, plus
  `kms:Decrypt` on the key used to encrypt the `SecureString` parameters
  (the default `alias/aws/ssm` key, unless a customer-managed key is used).
- `provider = secretsmanager`: `secretsmanager:GetSecretValue` on
  `secretsmanager_id`.

No other AWS permissions are required for secret resolution.

### Rotation

Rotate a credential by updating its value in AWS -- `aws ssm put-parameter
... --overwrite` or `aws secretsmanager update-secret ...` -- nothing on
the instance needs to change:

- Resolved values are cached in-process for 5 minutes, so a rotation takes
  effect on its own within that window without a restart.
- To force it immediately, restart the API: `nyxgpt ops restart api`.

**The `/admin/access` dashboard's "rotate API key" button is disabled when a
cloud secrets provider is configured.** `get_auth_api_key` always prefers the
AWS-resolved value over `config.ini`, so a rotation written to `config.ini`
by that endpoint would be inert -- the middleware would keep enforcing the
old cloud-stored key while the dashboard reported the new one as active.
`POST /admin/access` rejects `{"rotate": true}` with `400` in that case;
rotate via the AWS CLI/console as above instead.

### Failure behavior

If a provider is configured but AWS resolution fails (missing parameter,
denied IAM permission, boto3 not installed, etc.), that credential
resolves to `""` -- it is never silently satisfied by falling back to a
`config.ini` value cloud deploys don't populate anyway. For `[auth]
api_key` this fails *closed*: with auth enabled and an empty expected key,
no provided key can ever match, so the API rejects every request rather
than accepting none. For `[openai] api_key` / `[github] pat`, that
integration simply doesn't work until the underlying AWS issue is fixed;
check the nyxGPT process logs for a `Cloud secret resolution failed for
...` warning naming the failing key and provider.

A sustained failure (outage, bad IAM, wrong prefix) is remembered for only
30 seconds (vs. the 5-minute success cache), so resolution is retried
periodically rather than requiring a restart once the underlying issue is
fixed.

### Testing

`nyxgpt[cloud]` (`pip install "nyxgpt[cloud]"`) is required at runtime for
either provider -- see `src/nyxgpt/cloud_secrets.py`. Tests exercise both
providers against a mocked boto3 client (no live AWS dependency); see
`tests/unit/test_cloud_secrets.py`.

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

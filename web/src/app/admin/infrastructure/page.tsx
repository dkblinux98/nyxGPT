'use client';

// Every local deployment mode, plus the AWS substrate and the release deployed
// onto it -- all of it information only.
//
// The AWS section used to be its own screen at `/admin/cloud-infrastructure`
// with Plan, Terraform-state and tunnel controls on it. The owner removed both
// the screen and the controls (2026-08-16, #3804): every acting control there
// changed the substrate the UI itself runs on, and driving it safely would
// need a *second* nyxGPT, which collides with the first on :8000/:3000. So
// cloud lifecycle is `nyxgpt cloud ...` and this page reports. Reading does
// not remove the reader, which is why observation folds in cleanly and
// operation did not.
//
// The substrate facts come from whichever source can actually see them from
// here -- instance metadata on an EC2 instance, Terraform state on the
// workstation that provisioned it, and *unknown* on a machine that is neither
// (see `cloud_infra.infra_status`). A blank "not provisioned" next to accurate
// local status would read as a contradiction rather than as a missing source.

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';
import { apiErrorText, errorMessage } from '../../../lib/apiError';

type DeploymentModeName = 'native' | 'compose' | 'terraform' | 'kubernetes' | 'none';

type InfraStatus = {
  mode: DeploymentModeName;
  // True when this answer was computed by an api process running INSIDE the
  // Kubernetes deployment it is describing (#3988). The page used to have no
  // idea: served from the api Pod, it reported the cluster it was running in
  // as NOT DEPLOYED, surveyed Docker Compose against the container's own
  // filesystem, and offered native remedies aimed at a host the Pod cannot
  // see. Optional so the page still renders against an api process from
  // before #3988.
  in_cluster?: boolean;
  // Which build the native api/web are running: 'artifact' (published or
  // vendored builds -- the default) or 'dev' (a checkout's working tree,
  // `nyxgpt up --dev`, #3789). Surfaced here for the same reason `nyxgpt ops
  // status` prints it: a healthy-looking native stack must not let a
  // dev-mode install read as a verdict on the artifact path.
  // Optional so the page still renders against an api process from before
  // #3789 (e.g. mid-upgrade, when the web UI restarts first): absent is read
  // as the artifact default, exactly as the CLI reads a missing marker.
  install_mode?: {
    mode: 'artifact' | 'dev';
    checkout: string | null;
    label: string;
    components: string[];
    // WHICH build, not merely which mode (#3861). A mode cannot tell a 2.1.0
    // keg from a 3.0.0rc12 one -- both are 'artifact' -- which is how four
    // install identities accumulated on one machine unseen. `known: false`
    // means the marker predates identities (or none exists); the page says
    // so rather than presenting the mode as if it identified the build.
    identity?: {
      known: boolean;
      manager: string;
      services: Record<string, string>;
      version: string;
      channel: string;
      detail: string;
    };
    // False when the api process cannot honestly answer for a native install
    // from where it runs -- today, only from inside a Pod (#3988). The card
    // then states its scope instead of reporting an install identity, and
    // offers no remedy: `nyxgpt up` / `nyxgpt ops doctor` act on a host this
    // process has no access to.
    in_scope?: boolean;
    out_of_scope_reason?: string;
    // Which build the api process answering THIS request is executing,
    // against the venv the installed native service execs (#4133). Every
    // other field above -- including `identity.version` -- is derived from
    // what is on disk, and a process outlives the build it was started from:
    // a `brew upgrade` on a running stack left the api serving from a venv
    // the upgrade had deleted while `ops install` reported 56/56 [OK] and
    // every version surface reported the new keg. `state === 'mismatch'` is
    // the only actionable value; 'undetermined' and 'not_applicable' are
    // reported as themselves and never as a pass. Optional so the page still
    // renders against an api process from before #4133.
    running_build?: {
      state: 'match' | 'mismatch' | 'undetermined' | 'not_applicable';
      running: {
        executable: string;
        prefix: string;
        python: string;
        pid: number;
        version: string;
        prefix_exists: boolean;
      } | null;
      expected_prefix: string;
      expected_source: string;
      detail: string;
      remediation: string;
      summary: string;
    };
  };
  native: Record<string, string>;
  // Whether the native card's Docker-backed read (Cassandra, the one native
  // component that is a container) could be made at all (#4022). False means
  // this API process may not talk to the Docker daemon, so `native.cassandra`
  // is `unknown` and must not be read as "not running" — the exact false
  // negative that told the owner Cassandra was absent while it was serving.
  native_probe_available: boolean;
  native_probe_reason?: string;
  compose: Record<string, string>;
  compose_probe_available: boolean;
  // Why the probe could not run, when it could not (#3812) -- reported so the
  // page can name the cause ("`docker compose ps` exited 125: permission
  // denied ...") instead of only saying it can't tell.
  compose_probe_reason?: string;
  // Whether a Compose survey is a question about this deployment AT ALL, as
  // distinct from one that could not be answered (#4137). Two vantage points
  // are out of scope, not one: inside a Pod (#3988) and on a Kubernetes
  // *host*, which is neither in-cluster nor Compose. Keying the badge off
  // `inCluster` recognised only the first, so a k3s instance reached over the
  // wrapped tunnel showed CANNOT DETERMINE above its own running Pods.
  // Optional so an api process from before #4137 still renders.
  compose_in_scope?: boolean;
  compose_out_of_scope_reason?: string;
  conflicts: string[];
  terraform: {
    // Answered by whether the container reads happened, not by whether a
    // `docker` binary exists (#4022) — the same correction #3812 made to
    // `compose_probe_available`.
    probe_available: boolean;
    probe_reason?: string;
    deployed: boolean;
    containers: Record<string, string>;
    // The Terraform deployment's OWN install mode (#3835): 'artifact' (images
    // built from the published nyxgpt-api / nyxgpt-web source tarballs -- the
    // repo-less default; PR #4011 moved this path off GHCR images onto the
    // same artifact channel every other local install mode uses) or 'dev'
    // (images built from a checkout's working tree, `--dev`). Reported separately
    // from `install_mode` above because that one describes the native
    // services, which are a different deployment and frequently in the other
    // mode. Optional so the page still renders against an api process from
    // before #3835; `recorded` is false when no Terraform install has ever
    // written the marker, so the page can stay silent instead of asserting a
    // default -- and when something IS deployed with no marker, say so
    // instead (see `terraformImageMode`).
    install_mode?: {
      // 'unrecorded' since #3988: a two-value field cannot express "nothing
      // wrote this down", and `mode: 'artifact'` beside a `label` that said
      // unrecorded is one payload carrying both the honest answer and the
      // wrong one. `terraformImageMode` already derived the tri-state from
      // `recorded`/`deployed`; the api now agrees with it.
      mode: 'artifact' | 'dev' | 'unrecorded';
      checkout: string | null;
      label: string;
      images: Record<string, string>;
      recorded: boolean;
    };
  };
  kubernetes: {
    available: boolean;
    configured: boolean;
    probe_available: boolean;
    deployed: boolean;
    namespace: string;
    pods: string[];
    // Per-Pod ready/pending/failed (#3827). Optional on purpose, like
    // `observability` below: an older api that predates this field must fall
    // back to the plain `pods` lines, not take the page down.
    pod_states?: {
      name: string;
      state: 'ready' | 'pending' | 'failed' | string;
      summary: string;
      details: string;
    }[];
    // Pods no node would take (#3825) -- the FAILED subset of `pod_states`
    // whose remedy is a bigger cluster VM rather than a fix to the workload,
    // so the page can print that remedy once instead of per badge. Optional so
    // an api that predates the field degrades to "none reported" instead of
    // breaking the page.
    unschedulable?: string[];
    context: string;
    provisioned: boolean;
    // What the two images in this cluster were built from (#3834): the
    // published nyxgpt-api/nyxgpt-web artifacts, or a checkout's working tree
    // (`nyxgpt ops install --kubernetes --dev`). `recorded: false`
    // means no marker -- deployed before nyxGPT recorded one, or from another
    // machine -- which must read as UNRECORDED, never as the artifact
    // default: here that default would be a guess about someone else's
    // deployment. Optional so the page still renders against an api process
    // from before #3834.
    install_mode?: {
      // 'unrecorded' is a real value of `mode` since #3988's second round:
      // the owner's `--dev` cluster was reported as `mode: 'artifact'` with
      // `label: 'unrecorded ...'` beside it, so a reader of either field was
      // told something the other denied. `source` names which record
      // answered -- the cluster's own ConfigMap, or this machine's marker --
      // because "unrecorded" only means something with the *where* beside it.
      mode: 'artifact' | 'dev' | 'unrecorded';
      checkout: string | null;
      label: string;
      recorded: boolean;
      source?: string;
    };
    // Which nyxGPT this deployment is running (#3988, second round). The
    // Definition of Done asks this page for "what version ... without a
    // terminal" and the card answered Pods and nothing else -- never a
    // vantage-point limit, since in-cluster the api process serving this page
    // IS this deployment's api. `known: false` is the honest answer for a
    // deployment installed before the cluster carried a record; the card says
    // unknown rather than showing a release nobody installed. Optional so the
    // page still renders against an api process from before this field.
    version?: {
      known: boolean;
      version: string;
      channel: string;
      source: string;
    };
    // The in-cluster observability layer (#3787): Kubernetes mode cannot use
    // the Compose observability profiles, so it deploys its own. Optional on
    // purpose: an older api that predates this field must degrade to "NOT
    // DEPLOYED", not take the whole Infrastructure page down with it (#3468).
    observability?: {
      probe_available: boolean;
      deployed: boolean;
      workloads: Record<string, string>;
      // Badged from the same vocabulary as the Pod list above (#3827), minus
      // the one state only a Pod can be in: READY/PENDING/FAILED here, and
      // additionally SUPERSEDED there (#3990), because supersession is a
      // question about one replica being rolled past and a *workload* is never
      // rolled past. Without these states this section rendered raw
      // `"0/1 ready"`/`"1/1 ready"`/`"absent"` strings in undifferentiated
      // grey -- a workload that is up, one still rolling out and one that
      // never deployed all looked identical, on the same card that badges
      // every Pod. Optional, so an older api falls back to those plain lines.
      workload_states?: {
        name: string;
        state: 'ready' | 'pending' | 'failed' | string;
        summary: string;
        details: string;
      }[];
      port_forward_command: string;
      // The wrapped command that re-publishes a stripped node port (#3986).
      // Optional for the same reason as every field above it: an api process
      // from before this round must leave the sentence short, not render
      // `undefined` into a command an operator might copy.
      publish_command?: string;
    };
  };
  serving:
    | { supported: false; message: string }
    | {
        supported: true;
        active: boolean;
        weight_percent: number;
        stable: { state: string; message: string; version: string | null };
        canary: { state: string; message: string; version: string | null };
        components: Record<
          string,
          {
            active: boolean;
            weight_percent: number;
            stable: { state: string; message: string; version: string | null };
            canary: { state: string; message: string; version: string | null };
          }
        >;
      };
};

// --- AWS substrate + deployment (information only, #3804) ---

// Which source answered. `imds` = read from the instance this dashboard is
// running on; `cluster-record` = read from the cloud-deploy record the install
// wrote into the cluster this page is served from a Pod of (#4138 — a Pod can
// reach neither IMDS nor the host's ~/.nyxGPT/cloud, so this is the only
// source it has, and it is the instance's own facts); `terraform-state` = read
// from the state file on this machine; `none` = none of those, which is
// *unknown* and never "not provisioned".
type SubstrateSource = 'imds' | 'cluster-record' | 'terraform-state' | 'none';

type CloudInfraStatus = {
  source: SubstrateSource;
  source_label: string;
  on_ec2: boolean;
  known: boolean;
  provisioned: boolean;
  region: string;
  instance_id: string;
  instance_type: string;
  public_ip: string;
  vpc_id: string;
  subnet_id: string;
  security_group_id: string;
  ssh_key_name: string;
  // Which AWS account the substrate was provisioned in (#4186): the profile
  // name and the account id it resolved to, recorded by the resolver at
  // provision time so reporting it here needs no credential. Both empty on a
  // machine that did not provision it — the instance, or an api Pod — which is
  // the same "no source here can answer" `known` already covers.
  // `aws_account_label` is the rendered form, built by the one Python copy of
  // that wording and read here as-is. Optional: an api older than this page
  // does not send it.
  aws_profile: string;
  aws_account_id: string;
  aws_account_label?: string;
  ssh_identity_file: string;
  owner_ip_cidr: string;
  access_model: { open_ports: number[]; ssh_only: boolean; reachability: string };
  // #4181. What AWS last said about this instance, and on whose authority.
  // `observation.usable` is the only flag this page may claim presence or
  // billing from; the ids above are what a previous run recorded and are not
  // evidence on their own. Optional: an api older than this page does not
  // send it, and a missing block reads as "not confirmed", which is correct.
  observation?: VerifiedObservation;
};

type DeployHealth = {
  checked: boolean;
  healthy: boolean;
  status: number;
  reason: string;
};

type DeployHistoryEntry = {
  ts: number;
  action: string;
  outcome: string;
  version?: string;
  detail?: string;
};

// How this machine reaches the deployment (#3813). `known` is false on the
// instance itself and on a machine with no deploy record: the SSH user and
// identity file live in the record the deploy wrote on the operator's
// workstation, so `reason` says why there is nothing to show rather than
// rendering a blank target. `tunnel_invocation` is the raw ssh the wrapped
// tunnel executes -- shown as diagnostics, never as the instruction.
type CloudConnection = {
  known: boolean;
  host: string;
  user: string;
  identity_file: string;
  target: string;
  tunnel_invocation: string;
  command: string;
  reason: string;
};

// Five sources, not three (#3993). 'deploy-attempt' is a deploy this machine
// started and did not finish; 'substrate-record' is a provisioned instance
// with no deploy recorded against it. Both are *known* -- this machine wrote
// the record -- and neither is *deployed*, which is why the badge below reads
// `deployed` and not `known`: reporting DEPLOYED for a provision that died
// partway is the same class of lie the whole issue is about.

// An EC2 Mac Dedicated Host that has not been released yet (#3995). Every
// field is empty/false/null when there is none.
type MacHost = {
  host_id: string;
  instance_id: string;
  instance_type: string;
  region: string;
  availability_zone: string;
  allocated_at: string;
  release_at: string;
  release_scheduled: boolean;
  release_scheduled_at?: string;
  hourly_rate: number | null;
  // What AWS billed, from Cost Explorer, and nothing else (#4136). `null` means
  // nobody has asked AWS -- it is never quietly replaced by the local estimate,
  // which is carried separately below. The old "Accrued" row computed
  // `rate * (now - allocated_at)` and read $48.44 for a $12.02 bill that had
  // stopped two days earlier, because a local clock keeps counting after the
  // charges do not.
  accrued_cost: number | null;
  accrued_source?: string;
  estimated_cost?: number | null;
  spend_through?: string;
  spend_as_of?: string;
  spend_error?: string;
  currency: string;
  releasable_now: boolean;
  // When AWS last confirmed this host exists. Empty means no run has, which is
  // a different claim from "it is gone" -- and the reason this panel does not
  // say "still billing" on the strength of the record alone.
  verified_at?: string;
  host_present?: boolean | null;
  // Free internal-consistency findings. Non-empty means the record's fields
  // were written by different runs about different hosts, so none of them can
  // be read together.
  incoherent?: string[];
  // #4181. `billing` is no longer a constant `true` -- it IS `usable`, the one
  // gate any surface may claim presence, billing or release through, computed
  // once in `cloud_mac.observe_host`. `provenance` is the sentence to print
  // when the gate is closed, and it names which of the four causes applied:
  // never asked, asked and could not get an answer, answered from the wrong
  // account, or answered about a record that contradicts itself. Both are
  // rendered server-side so this card cannot word them differently from
  // `nyxgpt cloud status` (D-066).
  billing: boolean;
  usable?: boolean;
  provenance?: string;
  observation?: VerifiedObservation;
};

// #4181. What AWS last said about one recorded resource, and on whose
// authority -- `cloud_verified.Observation.to_dict()`. Shipped on both
// substrate payloads so this page never re-derives "may I state this as a
// fact?": `usable` is the answer and `provenance` is the sentence for when it
// is `false`.
type VerifiedObservation = {
  confirmed: boolean;
  confirmed_at: string;
  present: boolean | null;
  coherent: boolean;
  usable: boolean;
  findings: string[];
  reason: string;
  profile: string;
  account_id: string;
  account_label: string;
  provenance: string;
};

type CloudDeployStatus = {
  // 'cluster-record' (#4138) is the api Pod of a --kubernetes cloud
  // deployment: on the instance, so the version is still first-hand, but the
  // instance's identity comes from the record the install wrote into the
  // cluster rather than from an IMDS read a Pod cannot make.
  source:
    | 'deploy-record'
    | 'local-instance'
    | 'cluster-record'
    | 'deploy-attempt'
    | 'substrate-record'
    | 'none';
  known: boolean;
  // The last deploy this machine started, whatever became of it (#3993).
  // Absent on a payload from before that existed; `{}` means none was ever
  // started here.
  attempt?: {
    status?: string;
    phase?: string;
    version?: string;
    error?: string;
  };
  on_instance: boolean;
  deployed: boolean;
  version: string;
  host: string;
  instance_id: string;
  instance_type: string;
  region: string;
  // The AWS account and SSH key pair this deployment was made with (#4186),
  // lifted to the top level of the payload so this card can show them without
  // reaching into the substrate block. Local reads of what the resolver
  // recorded; empty on a machine that did not run the deploy.
  // `aws_account_label` is the rendered form — see `CloudInfraStatus`.
  aws_profile: string;
  aws_account_id: string;
  aws_account_label?: string;
  ssh_key_name: string;
  profiles: string[];
  // Where the deployment's chat sessions live (#3865). Empty when the deploy
  // record predates the flag, which is not the same claim as 'file'.
  session_backend: string;
  // What runs the stack on the instance (#3956): 'kubernetes' (a single-node
  // k3s cluster running k8s/*.yaml) or 'native'. Empty on a deploy record
  // that predates the flag -- reported as unknown, never as 'native'.
  substrate: string;
  // Whether the instance is running a shipped working tree rather than the
  // published release `version` names (#3950). Only the deploy record can
  // answer: an instance asked about itself reads its own package metadata,
  // which gives the version and not where it came from.
  dev: boolean;
  source_dir: string;
  // Which target OS's bootstrap provisioned the instance (#3867). Empty when
  // the deploy record predates `--os`, which is not the same claim as 'linux'.
  os_family: string;
  // An EC2 Mac Dedicated Host that is still allocated (#3995). Present with an
  // empty host_id when there is none. It outlives both the instance and the
  // deploy record by design -- `cloud destroy` terminates the Mac at once but
  // AWS refuses to release the host for 24 hours -- so it is the one thing on
  // this page that can be true while every other field says 'nothing is
  // deployed', and the one thing still costing money when it is.
  mac_host: MacHost;
  connection: CloudConnection;
  infra: CloudInfraStatus;
  tunnel: { running: boolean; pid: number };
  // The EC2 Mac's screen path (#4121). Two independent facts, because they are
  // independently true: `running` is whether the SSH forward is up on the
  // operator's machine right now, and `configured` is whether nyxGPT ever
  // enabled Screen Sharing on that Mac -- which outlives any one tunnel. The
  // credential is deliberately NOT in this payload; it lives in
  // ~/.nyxGPT/secrets and `password_file` names the path, never the secret
  // (#3458/#3466's rule for the HTTP API).
  screen?: {
    running: boolean;
    pid: number;
    local_port: number;
    url: string;
    configured: boolean;
    configured_at: string;
    password_file: string;
    command: string;
    stop_command: string;
  };
  health: DeployHealth;
  history: DeployHistoryEntry[];
  urls: Record<string, string>;
  // The wrapped `nyxgpt` commands that own each lifecycle action, rendered as
  // pointers. Taken from the backend's own LIFECYCLE_COMMANDS so what this
  // page prints cannot drift from what the CLI accepts.
  commands: Record<string, string>;
};

type CloudStateStatus = {
  backend: string;
  remote_enabled: boolean;
  bucket: string;
  table: string;
  key: string;
  region: string;
  locking: string;
  local_state_file: string;
};

const boxStyle: React.CSSProperties = {
  padding: '1.5rem',
  backgroundColor: 'var(--background-secondary)',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
};

const MODE_LABELS: Record<DeploymentModeName, string> = {
  native: 'Native (Homebrew services + Cassandra container)',
  compose: 'Docker Compose',
  // "Terraform" alone was ambiguous and actively misread (#3804): this mode
  // detects `nyxgpt-tf-*` containers on *this* machine, so an AWS instance
  // that Terraform provisioned reported "Terraform: NOT DEPLOYED". The two
  // uses of Terraform in this product have to be distinguishable on the page,
  // so the AWS section carries the other one.
  terraform: 'Terraform (local containers)',
  kubernetes: 'Kubernetes',
  none: 'Nothing detected running',
};

// "Not checked" is its own answer rather than "unhealthy": no tunnel means the
// stack is unreachable from here, which says nothing about whether it runs.
function healthLabel(health: DeployHealth | undefined): string {
  if (!health) return 'unknown';
  if (health.healthy) return 'healthy (HTTP 200 over the tunnel)';
  if (!health.checked) return `not checked — ${health.reason || 'no probe was run'}`;
  return `unhealthy — ${health.status ? `HTTP ${health.status}` : 'no response'} over the tunnel`;
}

function historyLabel(entry: DeployHistoryEntry): string {
  const when = Number.isFinite(entry.ts) ? new Date(entry.ts * 1000).toLocaleString() : '';
  const what = entry.version ? `${entry.action} ${entry.version}` : entry.action;
  return `${when} · ${what} · ${entry.outcome}`;
}

// The AWS account a deployment was made in, as the profile name and the
// account id it resolved to (#4186). The *string* is rendered server-side by
// `cloud_identity.recorded_account_label` and shipped in both status payloads,
// so this surface and `nyxgpt cloud status` cannot word it differently — the
// four branches used to be written out again here in TypeScript and had
// already drifted on the apostrophe (ledger D-066).
//
// The fallback covers one case only: an api older than this page, which has no
// such field. It is the empty-knowledge wording, not a second copy of the
// decision — there is nothing to decide from, since neither input is present
// either.
function awsAccountLabel(label: string | undefined): string {
  return label || 'not recorded here';
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <li style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', padding: '2px 0' }}>
      <span style={{ color: 'var(--foreground-muted)' }}>{label}</span>
      <code>{value || '—'}</code>
    </li>
  );
}

function badgeStyle(ok: boolean, neutral = false): React.CSSProperties {
  return {
    fontSize: '0.75rem',
    fontWeight: 600,
    padding: '2px 8px',
    borderRadius: 999,
    background: neutral ? 'var(--background)' : ok ? '#22c55e' : '#ef4444',
    color: neutral ? 'var(--foreground-muted)' : 'white',
    border: neutral ? '1px solid var(--border-color)' : 'none',
  };
}

// A Pod is ready, still starting, superseded or broken -- the same states
// `nyxgpt ops` prints as [OK]/[PENDING]/[SUPERSEDED]/[FAIL] (#3827, #3990).
// Pending is amber rather than red on purpose: it is a normal stage of a
// rollout, and colouring it as a failure is the browser version of the defect
// this fixed. Superseded is grey for the same reason in the other direction:
// the Pod really is dead, but its workload has already replaced it, so nothing
// about it is a call to action.
function podStateBadgeStyle(state: string): React.CSSProperties {
  const color =
    state === 'ready'
      ? '#22c55e'
      : state === 'pending'
        ? '#f59e0b'
        : state === 'superseded'
          ? '#6b7280'
          : '#ef4444';
  return {
    fontSize: '0.7rem',
    fontWeight: 600,
    padding: '1px 8px',
    borderRadius: 999,
    background: color,
    color: 'white',
    whiteSpace: 'nowrap',
  };
}

// The state a component carries when the read could not be made at all --
// `ops.DOCKER_STATE_UNKNOWN` (#4022). Rendered amber and spelled out, never
// folded in with the greys: 'absent' asserts the container is not there,
// 'unknown' asserts only that this process was not allowed to look.
const CONTAINER_STATE_UNKNOWN = 'unknown';

function ComponentList({ components }: { components: Record<string, string> }) {
  const entries = Object.entries(components);
  if (entries.length === 0) {
    return <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>None running.</p>;
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
      {entries.map(([component, state]) => {
        const unknown = state === CONTAINER_STATE_UNKNOWN;
        const running = state === 'running' || state === 'started';
        return (
          <li key={component} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
            <span>{component}</span>
            <span style={{ color: unknown ? '#f59e0b' : running ? '#22c55e' : 'var(--foreground-muted)' }}>
              {unknown ? 'unknown — cannot determine' : state}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Which build the Terraform containers are running -- the card's tri-state (#3835).
 *
 * `unrecorded` is the one that has to exist: a deployment that is *running*
 * with no marker is not the artifact default, it is a deployment whose build
 * nobody wrote down. Every Terraform deployment made before #3835 was built
 * from a working tree, so badging that "ARTIFACT IMAGES" asserts the exact
 * opposite of the truth -- the dev-read-as-artifact misreading this issue
 * exists to remove. Mirrors `InstallModeState.short_label(deployed=...)`.
 */
function terraformImageMode(
  terraform: InfraStatus['terraform'],
): 'dev' | 'artifact' | 'unrecorded' {
  if (terraform.install_mode?.mode === 'dev') {
    return 'dev';
  }
  if (terraform.deployed && !terraform.install_mode?.recorded) {
    return 'unrecorded';
  }
  return 'artifact';
}

export default function InfrastructurePage() {
  const [status, setStatus] = useState<InfraStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cloud, setCloud] = useState<CloudDeployStatus | null>(null);
  const [cloudState, setCloudState] = useState<CloudStateStatus | null>(null);
  // Its own error slot: the local and cloud reads are independent subsystems,
  // and a shared one would let whichever finished last hide the other's
  // failure behind its own.
  const [cloudError, setCloudError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/infra/status', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(apiErrorText(data, `HTTP ${res.status}`));
      }
      setStatus(data);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  // `probe_health=true` is what turns "a deploy was recorded" into "the stack
  // answers right now". It costs one short request through the tunnel, and the
  // backend skips it when there is no tunnel to probe through -- so it is
  // asked for on load and on an explicit refresh, never on a timer.
  //
  // `verify_host=true` does the same for the Dedicated Host (#4136): one
  // `DescribeHosts` plus an hourly-cached Cost Explorer read, so this panel can
  // say "AWS confirmed this host" and show the figure AWS actually billed
  // rather than repeating a local record and a local multiplication back. On
  // the same explicit load, for the same reason.
  const loadCloud = useCallback(async () => {
    setCloudError(null);
    try {
      const [deployRes, stateRes] = await Promise.all([
        fetch('/api/v1/cloud/deploy?probe_health=true&verify_host=true', { cache: 'no-store' }),
        fetch('/api/v1/cloud/state', { cache: 'no-store' }),
      ]);
      const deployData = await deployRes.json();
      if (!deployRes.ok) {
        throw new Error(apiErrorText(deployData, `HTTP ${deployRes.status}`));
      }
      setCloud(deployData as CloudDeployStatus);
      if (stateRes.ok) {
        setCloudState((await stateRes.json()) as CloudStateStatus);
      }
    } catch (e: unknown) {
      setCloudError(errorMessage(e));
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([loadStatus(), loadCloud()]);
  }, [loadStatus, loadCloud]);

  useEffect(() => {
    void loadStatus();
    void loadCloud();
  }, [loadStatus, loadCloud]);

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading infrastructure status...</p>
      </div>
    );
  }

  // The substrate facts ride along with the deployment status rather than
  // being fetched twice -- `cloud_deploy.deploy_status` embeds exactly the
  // `cloud_infra.infra_status` payload `GET /api/v1/cloud/infra` returns.
  const substrate = cloud?.infra ?? null;

  // Rows this vantage point cannot honestly answer (#3988). `in_scope: false`
  // is the api saying so explicitly; `in_cluster` is the fallback for the
  // in-between moment when the api has one field and not the other. Never
  // inferred from an empty result -- an empty native snapshot on a host is a
  // real "nothing running", and conflating the two is the defect.
  const inCluster = status?.in_cluster === true;
  const nativeOutOfScope = status?.install_mode?.in_scope === false || inCluster;
  // Same shape for the Compose card, with the api's explicit verdict first
  // (#4137): a Kubernetes HOST is out of scope for Compose while being very
  // much not in-cluster, so `inCluster` can only be the fallback here, never
  // the test. Without that, a k3s instance read "CANNOT DETERMINE" and named
  // a `docker-compose.yml` it does not use, above its own list of ready Pods.
  const composeOutOfScope = status?.compose_in_scope === false || inCluster;

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Infrastructure Status
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          What&apos;s actually running, honestly reported for every local deployment mode and for
          the AWS substrate.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      <div
        style={{
          marginBottom: '1.5rem',
          padding: '0.75rem 1rem',
          borderRadius: '0.375rem',
          background: 'var(--info-bg)',
          border: '1px solid var(--border-color)',
          fontSize: '0.875rem',
        }}
      >
        Full local Terraform and Kubernetes stacks are available today via{' '}
        <code>nyxgpt ops install --terraform</code> and{' '}
        <code>nyxgpt ops install --kubernetes</code> — see <code>docs/terraform.md</code>{' '}
        and <code>docs/kubernetes.md</code>. Neither requires a pre-existing cluster: the
        Kubernetes path provisions a local <code>kind</code> cluster automatically when none is
        reachable, and uses an existing cluster (minikube, Docker Desktop, ...) as-is when one
        is. <strong>This page reports; it does not install, deploy or destroy anything.</strong>{' '}
        Local infrastructure is created and torn down with <code>nyxgpt ops</code>, and the AWS
        substrate with <code>nyxgpt cloud</code> — a dashboard cannot safely change the substrate
        it is itself running on.
      </div>

      {error && (
        <div style={{ marginBottom: '1.5rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      {status && (
        <div style={{ display: 'grid', gap: '1.5rem' }}>
          {/* --- Detected mode --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Detected mode</h2>
              <button
                onClick={() => void refreshAll()}
                disabled={refreshing}
                title="Re-poll current status -- does not change anything"
                style={{
                  padding: '0.4rem 0.8rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  fontSize: '0.8rem',
                  fontWeight: 600,
                  cursor: refreshing ? 'not-allowed' : 'pointer',
                  background: 'var(--background)',
                  opacity: refreshing ? 0.6 : 1,
                }}
              >
                {refreshing ? 'Refreshing…' : 'Refresh status'}
              </button>
            </div>
            <p style={{ fontSize: '1rem', marginBottom: status.conflicts.length > 0 ? '0.75rem' : 0 }}>
              {MODE_LABELS[status.mode]}
            </p>
            {status.conflicts.length > 0 && (
              <p style={{ fontSize: '0.85rem', color: '#ef4444' }}>
                Port conflict: {status.conflicts.join(', ')} reported running in both native and
                Compose form. Run <code>nyxgpt ops doctor</code> for details.
              </p>
            )}
          </div>

          {/* --- Serving --- */}
          <div style={boxStyle}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Serving traffic
            </h2>
            {!status.serving.supported ? (
              <p style={{ fontSize: '0.875rem' }}>{status.serving.message}</p>
            ) : (
              <div style={{ display: 'grid', gap: '1rem' }}>
                {Object.entries(status.serving.components).map(([component, c]) => (
                  <div key={component} style={{ fontSize: '0.875rem', display: 'grid', gap: '0.4rem' }}>
                    <p style={{ fontWeight: 600, textTransform: 'capitalize' }}>{component}</p>
                    <p>
                      {c.active
                        ? `Canary rollout active -- ${c.weight_percent}% of traffic to canary.`
                        : 'No canary rollout active -- stable serves 100% of traffic.'}
                    </p>
                    <p>
                      Stable: <strong>{c.stable.state}</strong>
                      {c.stable.version ? ` (${c.stable.version})` : ''} —{' '}
                      {c.stable.message}
                    </p>
                    <p>
                      Canary: <strong>{c.canary.state}</strong>
                      {c.canary.version ? ` (${c.canary.version})` : ''} —{' '}
                      {c.canary.message}
                    </p>
                  </div>
                ))}
              </div>
            )}
            <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)', marginTop: '0.75rem' }}>
              To control which instance serves traffic (stable vs. canary), see the{' '}
              <a href="/admin/canary" style={{ color: '#0066cc' }}>
                Canary page
              </a>
              .
            </p>
          </div>

          {/* --- Native --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Native</h2>
              {/* Two different "cannot say" conditions, and they are not the
                  same claim: out-of-scope means there is nothing to determine
                  here, so it short-circuits; an unavailable probe means the
                  mode is real but unread, which belongs beside the mode. */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                {nativeOutOfScope ? (
                  <span style={badgeStyle(false, true)}>NOT IN SCOPE</span>
                ) : (
                  <>
                    {!status.native_probe_available && (
                      <span style={badgeStyle(false, true)}>CANNOT DETERMINE</span>
                    )}
                    <span style={badgeStyle(status.install_mode?.mode !== 'dev', false)}>
                      {status.install_mode?.mode === 'dev' ? 'DEV INSTALL' : 'ARTIFACT INSTALL'}
                    </span>
                  </>
                )}
              </div>
            </div>
            {/* An install identity, and the remedies that go with it, describe
                the MACHINE this api process runs on. From inside a Pod that is
                not the deployment the operator is looking at, so the card
                states its scope rather than reporting someone else's install
                (#3988). */}
            {nativeOutOfScope ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                {status.install_mode?.out_of_scope_reason ??
                  'Not in scope from here: this API is running inside a Kubernetes Pod.'}{' '}
                Run <code>nyxgpt ops status</code> on the host to survey a native install there.
              </p>
            ) : (
            <>
            <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
              {status.install_mode?.mode === 'dev' ? (
                <>
                  {status.install_mode.components.join(' and ')} run the working tree at{' '}
                  <code>{status.install_mode.checkout ?? 'an unrecorded checkout'}</code> (editable
                  venv + dev server), not a published build — so this stack is not exercising the
                  artifact path. Run <code>nyxgpt up</code> to return to it.
                </>
              ) : (
                <>
                  {status.install_mode?.label ??
                    'artifact (published/vendored build -- the repo-less default)'}
                </>
              )}
            </p>
            {status.install_mode?.identity?.known ? (
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                Installed build:{' '}
                <code>
                  {status.install_mode.identity.version || 'unknown version'} (
                  {status.install_mode.identity.channel})
                </code>
                , registered with <code>{status.install_mode.identity.manager}</code> as{' '}
                {Object.entries(status.install_mode.identity.services).map(
                  ([component, service], index, all) => (
                    <span key={component}>
                      <code>
                        {component}={service}
                      </code>
                      {index < all.length - 1 ? ', ' : ''}
                    </span>
                  ),
                )}
                .
              </p>
            ) : (
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                No install identity recorded — this machine cannot say which build the native
                api/web came from, only that they are {status.install_mode?.mode ?? 'artifact'}{' '}
                installs. Run <code>nyxgpt up</code> (add <code>--dev</code> from a checkout) to
                record one, and <code>nyxgpt ops doctor</code> to list any services left behind by
                an earlier install.
              </p>
            )}
            {/*
              What this api process is ACTUALLY executing, next to the
              installed build above (#4133). The two paragraphs above are
              derived from disk -- a marker file and the Cellar -- and both
              were reporting the new keg while the process serving this page
              came from a venv a `brew upgrade` had already deleted. This row
              is read from the serving process's own `sys.prefix`, so the
              process that may be wrong is the one answering.

              `not_applicable` renders nothing: on a Compose/Kubernetes
              deployment there is no keg for this process to match, and a
              permanent row saying so is what teaches an operator to skip the
              one that matters.
            */}
            {status.install_mode?.running_build &&
              status.install_mode.running_build.state === 'mismatch' && (
                <div
                  style={{
                    border: '1px solid var(--error, #b91c1c)',
                    borderRadius: '6px',
                    padding: '0.75rem',
                    marginBottom: '0.75rem',
                    fontSize: '0.8rem',
                  }}
                >
                  <p style={{ fontWeight: 600, marginBottom: '0.35rem' }}>
                    Running build does not match the installed build
                  </p>
                  <p style={{ marginBottom: '0.35rem' }}>
                    This API process is executing{' '}
                    <code>{status.install_mode.running_build.running?.prefix}</code> (python{' '}
                    {status.install_mode.running_build.running?.python}), but the installed
                    service execs{' '}
                    <code>{status.install_mode.running_build.expected_prefix}</code>. The version
                    reported everywhere else describes what is installed, not this process.
                  </p>
                  {status.install_mode.running_build.running?.prefix_exists === false && (
                    <p style={{ marginBottom: '0.35rem' }}>
                      That path no longer exists — this process is holding deleted files open,
                      and the next restart by any path (reboot, self-heal, the Restart control)
                      will fail to start the API.
                    </p>
                  )}
                  <p style={{ marginBottom: 0 }}>
                    Fix: <code>{status.install_mode.running_build.remediation}</code>
                  </p>
                </div>
              )}
            {status.install_mode?.running_build &&
              status.install_mode.running_build.state === 'undetermined' && (
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                  Could not confirm that this API process is running the installed build:{' '}
                  {status.install_mode.running_build.detail}. That is not the same as a match —
                  check with <code>nyxgpt ops status</code>.
                </p>
              )}
            {!status.native_probe_available && (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                Cassandra runs as a Docker container, and this API process could not read
                container state — so its row below says <code>unknown</code>, which is not the
                same as &quot;not running&quot;.
                {status.native_probe_reason ? (
                  <>
                    {' '}
                    Reason: <code>{status.native_probe_reason}</code>.
                  </>
                ) : null}{' '}
                This is usually a service session that predates its <code>docker</code> group
                membership; recreate the session with{' '}
                <code>sudo loginctl terminate-user $USER</code> to fix it permanently. Check the
                current state with <code>nyxgpt ops status</code>.
              </p>
            )}
            <ComponentList components={status.native} />
            </>
            )}
          </div>

          {/* --- Compose --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Docker Compose</h2>
              {!status.compose_probe_available && (
                <span style={badgeStyle(false, true)}>
                  {composeOutOfScope ? 'NOT IN SCOPE' : 'CANNOT DETERMINE'}
                </span>
              )}
            </div>

            {/* Two different unknowns, and #3988 is about telling them apart.
                From a host that runs Compose, "could not run the survey" is a
                probe failure and its cause belongs on screen. Where there is
                no Compose tier to survey at all there is no such verdict to
                reach -- and the old rendering leaked a `docker-compose.yml`
                path to the operator as the reason for a verdict about a
                deployment that does not use one (inside a Pod, the
                CONTAINER's own `/root/.nyxGPT/...`, #3988; on a k3s host,
                `/home/ec2-user/.nyxGPT/...`, #4137).

                The gate is the API's own scope verdict rather than
                `inCluster`, because in-cluster is only one of the two vantage
                points it is true of -- see `compose_in_scope`. */}
            {composeOutOfScope ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                {status.compose_out_of_scope_reason ??
                  status.compose_probe_reason ??
                  'Not in scope from here: this API is running inside a Kubernetes Pod, which has no host filesystem and no Docker socket.'}
              </p>
            ) : !status.compose_probe_available ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                Cannot determine from here — the Compose survey could not be run from wherever
                this API process is running, so nothing below can be read as &quot;not
                running&quot;.
                {status.compose_probe_reason ? (
                  <>
                    {' '}
                    Reason: <code>{status.compose_probe_reason}</code>.
                  </>
                ) : null}{' '}
                Check it yourself with <code>nyxgpt ops status</code>.
              </p>
            ) : (
              <ComponentList components={status.compose} />
            )}
          </div>

          {/* --- Terraform, the *local container* stack. Named in full because
              the AWS section below is also Terraform-provisioned (#3804). --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Terraform (local containers)</h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                {(status.terraform.deployed || status.terraform.install_mode?.recorded) && (
                  <span
                    style={badgeStyle(
                      terraformImageMode(status.terraform) === 'artifact',
                      terraformImageMode(status.terraform) === 'unrecorded',
                    )}
                  >
                    {status.terraform.install_mode?.mode === 'dev'
                      ? 'DEV IMAGES'
                      : terraformImageMode(status.terraform) === 'unrecorded'
                        ? 'IMAGES NOT RECORDED'
                        : 'ARTIFACT IMAGES'}
                  </span>
                )}
                <span style={badgeStyle(status.terraform.deployed, !status.terraform.probe_available)}>
                  {!status.terraform.probe_available
                    ? 'CANNOT DETERMINE'
                    : status.terraform.deployed
                      ? 'DEPLOYED'
                      : 'NOT DEPLOYED'}
                </span>
              </div>
            </div>

            <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
              The <code>nyxgpt-tf-*</code> containers Terraform runs on <em>this</em> machine. An
              AWS instance that Terraform provisioned is a different thing and is reported under
              AWS below.
            </p>

            {/* This deployment's own install mode (#3835) — never the native
                marker above it, which describes a different deployment. */}
            {(status.terraform.deployed || status.terraform.install_mode?.recorded) && (
              <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                {status.terraform.install_mode?.mode === 'dev' ? (
                  <>
                    The api and web containers were built from the working tree at{' '}
                    <code>{status.terraform.install_mode.checkout ?? 'an unrecorded checkout'}</code>,
                    not from published images — so this deployment is not exercising the artifact
                    path. Re-run <code>nyxgpt up --terraform</code> without{' '}
                    <code>--dev</code> to return to it.
                  </>
                ) : !status.terraform.install_mode?.recorded ? (
                  // Equivalent to `terraformImageMode(...) === 'unrecorded'`
                  // under the guard above: this paragraph only renders when
                  // something is deployed or a marker exists, so "not
                  // recorded" here always means containers are running.
                  // Written this way so the last branch has a label to show
                  // rather than a fallback that can never be reached.
                  <>
                    Containers are running, but no install recorded what they were built from — this
                    deployment predates the per-deployment install-mode marker, or was brought up
                    outside <code>nyxgpt ops</code>. Whether its api and web images came from a
                    checkout or from the published images is unknown, so neither is claimed here.
                    Re-run <code>nyxgpt up --terraform</code> (add <code>--dev</code> for a
                    working-tree build) to redeploy it and record the mode.
                  </>
                ) : (
                  <>{status.terraform.install_mode.label}</>
                )}
              </p>
            )}

            {!status.terraform.probe_available ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                Cannot determine from this deployment mode — docker isn&apos;t reachable from
                wherever this API process is running, so nothing below can be read as &quot;not
                running&quot;.
                {status.terraform.probe_reason ? (
                  <>
                    {' '}
                    Reason: <code>{status.terraform.probe_reason}</code>.
                  </>
                ) : null}{' '}
                Check it yourself with <code>nyxgpt ops status</code>.
              </p>
            ) : (
              <ComponentList components={status.terraform.containers} />
            )}
          </div>

          {/* --- Kubernetes --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Kubernetes</h2>
              <span style={badgeStyle(status.kubernetes.deployed, !status.kubernetes.probe_available)}>
                {!status.kubernetes.probe_available
                  ? 'CANNOT DETERMINE'
                  : status.kubernetes.deployed
                    ? 'DEPLOYED'
                    : 'NOT DEPLOYED'}
              </span>
            </div>

            {!status.kubernetes.available ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                kubectl not found from this vantage point — no cluster configured to detect.
              </p>
            ) : !status.kubernetes.configured ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No cluster configured from this vantage point — no kubeconfig current-context, and
                no in-cluster ServiceAccount credentials.
              </p>
            ) : !status.kubernetes.probe_available ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                Cannot determine from this deployment mode — the cluster wasn&apos;t reachable.
              </p>
            ) : (
              <>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                  Context: <code>{status.kubernetes.context}</code>
                  {/* In-cluster is its own case (#3988): there is no kubeconfig
                      context to name, and this process cannot see whether the
                      cluster carrying it is one nyxGPT provisioned -- so it
                      claims neither. */}
                  {inCluster
                    ? ' — this page is being served from inside this cluster, and is reporting the deployment it is itself running in.'
                    : status.kubernetes.provisioned
                      ? ' — local kind cluster provisioned by nyxgpt (torn down together on `nyxgpt ops down --kubernetes`).'
                      : ' — bring-your-own cluster (never destroyed by `nyxgpt ops down --kubernetes`).'}
                </p>
                {/* What version this deployment is running (#3988). The Definition
                    of Done asks this page for it directly, and the card used to
                    answer Pods and stop -- while the api process serving the page
                    knew its own version all along. */}
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                  Version:{' '}
                  {status.kubernetes.version?.known ? (
                    <>
                      <strong>{status.kubernetes.version.version}</strong>
                      {status.kubernetes.version.channel &&
                      status.kubernetes.version.channel !== 'unknown'
                        ? ` (${status.kubernetes.version.channel} channel)`
                        : ''}
                      {status.kubernetes.version.source
                        ? ` — from ${status.kubernetes.version.source}.`
                        : ''}
                    </>
                  ) : (
                    <>
                      <strong>unknown</strong> — this deployment carries no install record to read
                      a version from, and this dashboard is not being served from inside it.
                      Re-run <code>nyxgpt ops install --kubernetes</code> to record one, or ask
                      the host with <code>nyxgpt ops status</code>.
                    </>
                  )}
                </p>
                {/* The deployment's own install mode (#3834) -- what the images in
                    THIS cluster were built from. Never the native marker: a host
                    can run a native dev install and a Kubernetes artifact
                    deployment at once, and reporting one for the other is the
                    defect this section exists to prevent. */}
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                  Install mode:{' '}
                  {!status.kubernetes.install_mode?.recorded ? (
                    <>
                      <strong>unrecorded</strong> — neither this cluster nor the machine this
                      dashboard runs on holds an install record for this deployment. It was
                      deployed before nyxGPT recorded one in the cluster. Re-run{' '}
                      <code>nyxgpt ops install --kubernetes</code> (add <code>--dev</code> for a
                      working-tree build) to record it.
                    </>
                  ) : status.kubernetes.install_mode.mode === 'dev' ? (
                    <>
                      <strong>dev</strong> — the Pods run images built from the working tree at{' '}
                      <code>{status.kubernetes.install_mode.checkout ?? 'an unrecorded checkout'}</code>{' '}
                      as it was at install time, not from published artifacts. Re-run{' '}
                      <code>nyxgpt ops install --kubernetes</code> without{' '}
                      <code>--dev</code> to deploy the artifacts.
                    </>
                  ) : (
                    <>
                      <strong>artifact</strong> — images built from the published{' '}
                      <code>nyxgpt-api</code>/<code>nyxgpt-web</code> artifacts (no checkout
                      involved).
                    </>
                  )}
                  {status.kubernetes.install_mode?.recorded &&
                  status.kubernetes.install_mode.source
                    ? ` Read from ${status.kubernetes.install_mode.source}.`
                    : ''}
                </p>
                {status.kubernetes.pod_states && status.kubernetes.pod_states.length > 0 ? (
                  /* Three states, not two (#3827): a Pod that is still pulling its
                     image is PENDING, not a failure -- the install used to print
                     [FAIL] for exactly this and buried the one Pod that really
                     could not start. FAILED carries the scheduler's/kubelet's own
                     reason, because "Pending" on its own does not distinguish
                     "downloading" from "this node cannot fit it".

                     Four since #3990's rework: a terminal Pod its own workload
                     has already replaced is SUPERSEDED. Kubernetes keeps those
                     for diagnosis, so every rollout leaves one behind, and
                     badging it FAILED would show a serving deployment as broken
                     for ever -- and would contradict `nyxgpt ops status`, which
                     is the disagreement #3827 exists to prevent. */
                  <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.8rem' }}>
                    {status.kubernetes.pod_states.map((pod) => (
                      <li key={pod.name} style={{ padding: '3px 0' }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem' }}>
                          <span style={{ fontFamily: 'monospace' }}>{pod.name}</span>
                          <span style={podStateBadgeStyle(pod.state)}>
                            {pod.state === 'ready'
                              ? 'READY'
                              : pod.state === 'pending'
                                ? 'PENDING'
                                : pod.state === 'superseded'
                                  ? 'SUPERSEDED'
                                  : 'FAILED'}
                          </span>
                        </div>
                        <div style={{ color: 'var(--foreground-muted)', fontFamily: 'monospace' }}>
                          {pod.summary}
                          {pod.details ? ` — ${pod.details}` : ''}
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : status.kubernetes.pods.length > 0 ? (
                  <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.8rem', fontFamily: 'monospace' }}>
                    {status.kubernetes.pods.map((line, idx) => (
                      <li key={idx} style={{ padding: '2px 0' }}>
                        {line}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                    No pods in the <code>{status.kubernetes.namespace}</code> namespace.
                  </p>
                )}

                {/* #3825: an unschedulable Pod reads as `Pending` in the list
                    above, indistinguishable from one that is starting -- so a
                    deployment missing prometheus for want of node memory
                    looked healthy here. Named explicitly, with the wrapped
                    command that diagnoses and refuses it up front. Reporting
                    only: the cure is more memory or CPU on the cluster VM,
                    which no page served by that cluster can grant itself. */}
                {(status.kubernetes.unschedulable?.length ?? 0) > 0 && (
                  <div
                    style={{
                      marginTop: '0.75rem',
                      padding: '0.75rem',
                      border: '1px solid var(--border-color)',
                      borderRadius: '4px',
                      fontSize: '0.8rem',
                    }}
                  >
                    <strong>
                      {status.kubernetes.unschedulable?.length} Pod(s) could not be scheduled
                    </strong>
                    <ul style={{ margin: '0.35rem 0', paddingLeft: '1.1rem', fontFamily: 'monospace' }}>
                      {status.kubernetes.unschedulable?.map((name) => (
                        <li key={name}>{name}</li>
                      ))}
                    </ul>
                    <span style={{ color: 'var(--foreground-muted)' }}>
                      No node had enough unreserved memory or CPU for them. Give the cluster VM
                      more of either (Docker Desktop: Settings &rarr; Resources), then re-run{' '}
                      <code>nyxgpt ops install --kubernetes</code> — it checks the node&apos;s
                      capacity against the stack before applying anything.
                    </span>
                  </div>
                )}

                {/* In-cluster observability (#3787). Kubernetes mode runs its own
                    Grafana/Prometheus/Loki/Jaeger/GlitchTip: the Compose profiles
                    scrape the host and resolve Compose service names, so they are
                    unreachable from a cluster. */}
                <div style={{ marginTop: '1rem', paddingTop: '0.75rem', borderTop: '1px solid var(--border-color)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                    <h3 style={{ fontSize: '0.95rem', fontWeight: 'bold' }}>In-cluster observability</h3>
                    <span style={badgeStyle(Boolean(status.kubernetes.observability?.deployed))}>
                      {status.kubernetes.observability?.deployed ? 'DEPLOYED' : 'NOT DEPLOYED'}
                    </span>
                  </div>
                  {status.kubernetes.observability?.deployed ? (
                    <>
                      {status.kubernetes.observability.workload_states &&
                      status.kubernetes.observability.workload_states.length > 0 ? (
                        /* Badged READY/PENDING/FAILED from the same vocabulary as the
                           Pods above (#3827): `0/1 ready` is PENDING, not a quiet grey
                           line the operator has to interpret against a Pod list that
                           already ruled on the same condition two sections up. Three
                           of the four, not four: the Pod list also badges SUPERSEDED
                           (#3990), which a workload can never be -- only one of its
                           replicas can be rolled past. */
                        <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                          {status.kubernetes.observability.workload_states.map((workload) => (
                            <li
                              key={workload.name}
                              style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem', padding: '3px 0' }}
                            >
                              <span>{workload.name}</span>
                              <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                <span style={{ color: 'var(--foreground-muted)', fontFamily: 'monospace', fontSize: '0.8rem' }}>
                                  {workload.summary}
                                </span>
                                <span style={podStateBadgeStyle(workload.state)}>
                                  {workload.state === 'ready'
                                    ? 'READY'
                                    : workload.state === 'pending'
                                      ? 'PENDING'
                                      : 'FAILED'}
                                </span>
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <ComponentList components={status.kubernetes.observability.workloads} />
                      )}
                      {/* READY here means the workload is RUNNING, which is not the
                          same as receiving anything -- a collector with no clients is
                          as ready as one with a thousand, and #3990 was exactly that:
                          ten READY badges over a tier that observed nothing. The
                          data-flow answer is a `kubectl exec` into the Grafana and api
                          Pods, which this api's ServiceAccount deliberately has no
                          `pods/exec` rights for, so the page names the command that
                          asks instead of growing the privilege to ask it itself. */}
                      <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
                        READY means the workload is running, not that telemetry is
                        reaching it. For what each backend has actually received —
                        Jaeger&apos;s spans, Prometheus&apos;s scrape targets,
                        Loki&apos;s log labels, GlitchTip&apos;s errors, and whether
                        Grafana&apos;s GlitchTip credential still authenticates — run{' '}
                        <code>nyxgpt ops status</code>.
                      </p>
                      {/* #3986: this card used to state as a fact that the SRE
                          Services were ClusterIP and a forward the only way in.
                          The SRE-tier publish falsified that --
                          where nyxGPT provisioned the cluster the install maps
                          Grafana 3001, Prometheus 9090, Jaeger 16686 and GlitchTip
                          8080 on the host and this page's own SRE links reach them
                          with no terminal, which is what the Definition of Done
                          asks for. Both paths are named and neither is asserted:
                          served from the api Pod, this card can see neither the
                          node's port mappings nor the Services (#3988).

                          The cause is named without naming the command that
                          causes it ("re-applying the shipped manifests", not
                          `kubectl apply -k k8s/`): no raw command string
                          renders anywhere in this card, which is the invariant
                          the suite's two `/kubectl/` negative assertions pin.
                          Operational Command Wrapping is most load-bearing
                          here, on the one surface read without a terminal. */}
                      <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
                        Where nyxGPT provisioned the cluster, the install publishes Grafana,
                        Prometheus, Jaeger and GlitchTip on the ports this dashboard links to —
                        no command needed. On a bring-your-own cluster, reach them with{' '}
                        <code>{status.kubernetes.observability.port_forward_command}</code>
                        {status.kubernetes.observability.publish_command ? (
                          <>
                            ; if re-applying the shipped manifests has stripped the published
                            ports, put them back with{' '}
                            <code>{status.kubernetes.observability.publish_command}</code>.
                          </>
                        ) : (
                          '.'
                        )}
                      </p>
                    </>
                  ) : (
                    <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                      No observability workloads in the <code>{status.kubernetes.namespace}</code>{' '}
                      namespace — deploy them with{' '}
                      <code>nyxgpt ops observability --kubernetes</code> (
                      <code>nyxgpt ops install --kubernetes</code> includes them unless{' '}
                      <code>--skip-observability</code> is passed).
                    </p>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* --- AWS: substrate, deployment, state backend and history (#3804) ---
          Outside the `status &&` block on purpose: the local probe failing is
          no reason to stop reporting the cloud, and vice versa. Information
          only -- there is not a single control in here. */}
      <div style={{ display: 'grid', gap: '1.5rem', marginTop: '1.5rem' }}>
        <div style={boxStyle}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>AWS substrate</h2>
            <span style={badgeStyle(Boolean(substrate?.provisioned), !substrate?.known || !substrate?.provisioned)}>
              {!substrate?.known
                ? 'UNKNOWN'
                : substrate.provisioned
                  ? 'PROVISIONED'
                  : 'NOT PROVISIONED'}
            </span>
          </div>

          <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
            A VPC, a public subnet, one security group that opens{' '}
            <strong>port 22 only</strong> to the operator&apos;s own IP — never{' '}
            <code>0.0.0.0/0</code> — and a single EC2 instance. The app, web UI and every
            observability endpoint bind <code>127.0.0.1</code> on that instance and are reached
            over an SSH tunnel.
          </p>

          {cloudError && (
            <div style={{ marginBottom: '1rem' }}>
              <ErrorMessage message={cloudError} onRetry={() => void loadCloud()} />
            </div>
          )}

          {!substrate?.known ? (
            <p style={{ fontSize: '0.875rem' }}>
              Unknown from this machine — it is neither an EC2 instance nor one that has
              provisioned the substrate, so nothing here can answer. This is not the same as
              &ldquo;not provisioned&rdquo;. Run <code>nyxgpt cloud infra status</code> where the
              substrate was provisioned.
            </p>
          ) : (
            <>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                <Row label="Read from" value={substrate.source_label} />
                <Row label="Region" value={substrate.region} />
                <Row label="Instance" value={substrate.instance_id} />
                <Row label="Instance type" value={substrate.instance_type} />
                <Row label="Public IP" value={substrate.public_ip} />
                <Row label="VPC" value={substrate.vpc_id} />
                <Row label="Subnet" value={substrate.subnet_id} />
                <Row label="Security group" value={substrate.security_group_id} />
                {/* #4186: the account was the one provisioning input no screen
                    ever showed, so an operator with more than one AWS account
                    could not tell which held their instance. Profile and
                    account id together — either alone is ambiguous. */}
                <Row label="AWS account" value={awsAccountLabel(substrate.aws_account_label)} />
                <Row label="SSH key pair" value={substrate.ssh_key_name} />
                <Row
                  label="SSH allowed from"
                  value={
                    substrate.owner_ip_cidr ||
                    (substrate.on_ec2
                      ? 'not visible from the instance — it is a security-group rule, not metadata'
                      : '')
                  }
                />
                <Row
                  label="Open ports"
                  value={
                    substrate.access_model.open_ports.length > 0
                      ? substrate.access_model.open_ports.join(', ')
                      : 'none'
                  }
                />
              </ul>
              {!substrate.provisioned && (
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.75rem' }}>
                  This machine has Terraform state for the substrate and it records no instance.
                </p>
              )}
            </>
          )}
        </div>

        <div style={boxStyle}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Cloud deployment</h2>
            {/* Two states, not three. A deployment is known only from the
                record the deploy wrote here or from being the instance, and
                in both cases something *is* deployed. The absence of a
                record on this machine is not evidence that nothing is
                deployed -- another operator's would say otherwise -- so
                there is deliberately no NOT DEPLOYED to claim it. */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              {/* #3950: a dev deploy and an artifact deploy of the same
                  version are identical in every other field on this card, so
                  without this the page would report a working-tree build as
                  though it were a published release. Same badge shape the
                  Native card uses for the local equivalent. */}
              {cloud?.deployed && cloud.dev ? (
                <span style={badgeStyle(false, false)}>DEV BUILD</span>
              ) : null}
              {/* #3993: three verdicts, because there are three answers.
                  Keying the badge off `known` alone said DEPLOYED for a
                  deploy that died partway -- the failure family this issue
                  exists to close. */}
              <span style={badgeStyle(Boolean(cloud?.deployed), !cloud?.known)}>
                {cloud?.deployed
                  ? 'DEPLOYED'
                  : cloud?.source === 'deploy-attempt'
                    ? 'NOT COMPLETED'
                    : cloud?.source === 'substrate-record'
                      ? 'SUBSTRATE ONLY'
                      : 'UNKNOWN'}
              </span>
            </div>
          </div>

          {!cloud?.known ? (
            <p style={{ fontSize: '0.875rem' }}>
              Unknown from this machine — no deploy has been recorded here and this is not the
              instance. Run <code>{cloud?.commands?.status ?? 'nyxgpt cloud status'}</code>{' '}
              where the deploy was run.
              {/* #4181. A declined consent lands here: it is no longer counted
                  as an unfinished deploy, because nothing was created and
                  there is nothing to resume. It is still the answer to "what
                  happened when I ran that command", so it is said rather than
                  swallowed by the sentence above. */}
              {cloud?.attempt?.status === 'declined' && (
                <>
                  {' '}
                  The last deploy started here was <strong>DECLINED</strong> at the{' '}
                  <code>{cloud.attempt.phase || 'unknown'}</code> phase — the priced disclosure
                  was shown and not accepted, so nothing was created and nothing is billed.
                </>
              )}
            </p>
          ) : !cloud.deployed ? (
            /* #3993. Observable, never operable (D-017): this states what
               exists and names the command that moves it forward, and drives
               nothing itself. Saying "unknown" here sent the owner looking
               for another workstation while their own state file named the
               instance. The billing sentence is conditioned on evidence an
               instance exists (D-018): a deploy that failed at or before the
               substrate step records no ids, and asserting billing over it is
               the lie this card was written to end. */
            <p style={{ fontSize: '0.875rem' }}>
              {cloud.source === 'deploy-attempt'
                ? `A deploy started on this machine and did not finish${
                    cloud.attempt?.phase ? ` — it stopped at the \`${cloud.attempt.phase}\` phase` : ''
                  }${cloud.attempt?.error ? `: ${cloud.attempt.error}` : '.'}`
                : 'A substrate is provisioned, but no deploy has been recorded against it.'}{' '}
              {/* #4181 splits what D-018 conflated. "Something is recorded
                  here" and "something exists in AWS and is costing money" are
                  different claims, and this card printed the second from
                  evidence for only the first — live, with AWS holding no
                  instances at all. The billing assertion now needs a
                  confirmation from THIS run, which is exactly what
                  `observation.usable` is, computed once in Python for both
                  substrates. */}
              {cloud.mac_host?.usable || cloud.infra?.observation?.usable ? (
                <>
                  An instance exists and is being billed — AWS confirmed it in this run (
                  {cloud.mac_host?.usable
                    ? cloud.mac_host.provenance
                    : cloud.infra?.observation?.provenance}
                  ). This is not the same as nothing being deployed, and not the same as unknown.
                  Re-run <code>{cloud.commands?.deploy ?? 'nyxgpt cloud deploy'}</code>{' '}
                  (idempotent), or{' '}
                  <code>{cloud.commands?.destroy ?? 'nyxgpt cloud destroy --yes'}</code> to tear it
                  down.
                </>
              ) : cloud.source !== 'deploy-attempt' ||
                cloud.instance_id ||
                cloud.host ||
                cloud.instance_type ||
                cloud.mac_host?.host_id ? (
                <>
                  This machine’s records name a resource, but nothing confirmed it at AWS in this
                  run — so nyxGPT cannot tell you whether it still exists or is still being
                  billed. The ids on this card are what was recorded here.{' '}
                  <code>{cloud.commands?.status ?? 'nyxgpt cloud status'}</code> asks AWS and
                  clears what it no longer has.
                </>
              ) : (
                <>
                  Nothing is recorded as provisioned by this attempt: it failed at or before the
                  substrate step, so no instance was created here and nothing from it is being
                  billed. Re-run{' '}
                  <code>{cloud.commands?.deploy ?? 'nyxgpt cloud deploy'}</code> (idempotent).
                </>
              )}
            </p>
          ) : (
            <>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                {/* #4138: three vantage points, named rather than conflated.
                    A Pod of the deployment's own cluster IS the deployed stack
                    answering — so the release below is first-hand — but the
                    instance it names was read from the record the install
                    wrote into the cluster, because a Pod reaches neither IMDS
                    nor the host's deploy record. Saying "read first-hand"
                    flatly there would claim an IMDS read nothing made. */}
                {cloud.source === 'cluster-record'
                  ? 'Read from inside the deployment: this dashboard is served by an api Pod of the cluster on the instance, so the release below is the one answering this request. The instance it runs on comes from the cloud-deploy record `nyxgpt ops install --kubernetes` wrote into this cluster from the instance itself.'
                  : cloud.on_instance
                    ? 'Read first-hand: this dashboard is served by the deployed stack itself, so the release below is the one answering this request.'
                    : 'What `nyxgpt cloud deploy` last put on the instance: a published nyxGPT release — or, under --dev, a copy of an operator’s working tree — and the observability profiles it enabled. The instance clones no repository either way.'}
              </p>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                <Row label="Installed version" value={cloud.version} />
                {/* #3950. Named on every deployment rather than only on dev
                    ones: "published release" is a claim worth stating, and a
                    row that appears only in one state is a row an operator
                    does not know to look for. Observed, never driven — the
                    build source is chosen at `nyxgpt cloud deploy`. */}
                <Row
                  label="Build source"
                  value={
                    cloud.dev
                      ? `working tree shipped from ${cloud.source_dir || 'an unrecorded checkout'} (--dev) — not a published ${cloud.version} release, and not exercising the artifact path`
                      : cloud.on_instance
                        ? 'not recorded here — the deploy record lives on the workstation that ran the deploy'
                        : 'published release, installed from PyPI on the instance'
                  }
                />
                <Row label="Host" value={cloud.host} />
                <Row
                  label="Instance"
                  value={
                    cloud.instance_type
                      ? `${cloud.instance_id} (${cloud.instance_type})`
                      : cloud.instance_id
                  }
                />
                <Row label="Region" value={cloud.region} />
                {/* #4186. Same pair as the substrate card above, repeated here
                    because this is the card an operator reads after a deploy
                    and "which account is this in?" is part of the answer. */}
                <Row label="AWS account" value={awsAccountLabel(cloud.aws_account_label)} />
                <Row label="SSH key pair" value={cloud.ssh_key_name} />
                {/* #3867: the two target OSes are provisioned by different
                    bootstraps and do not leave the instance in the same
                    shape — an EC2 Mac runs the Homebrew formulas under
                    launchd and can host no containers. Reported here because
                    nothing else on this page distinguishes them. Observed,
                    never driven: the pointer is `nyxgpt cloud deploy --os`.

                    #4150: this row used to read "no observability stack, no
                    self-heal watchdog", which an operator could reasonably
                    read as the full list of what a Mac gives up — and the
                    model backend was quietly missing too. It is now installed
                    there (api, web AND ollama), so the row says what the Mac
                    HAS before what it lacks, and attributes the gap to the
                    container tier rather than to a vague shortfall.

                    The nested-virtualization clause covers the CONTAINER TIER
                    and stops there. Self-healing being off is not a platform
                    limit: the watchdog is a thread inside the api process
                    (docs/self-healing.md), it ships disabled everywhere, and
                    this bootstrap simply does not turn it on. Attributing it
                    to the platform would repeat #4150's own mistake one
                    component over, so it is named separately, as a default,
                    with the page that toggles it. */}
                <Row
                  label="Target OS"
                  value={
                    cloud.os_family === 'macos'
                      ? 'macOS (EC2 Mac) — remote Homebrew tap + brew services: api, web and the ollama model backend. No containers (no nested virtualization), so no observability stack and no Cassandra. Self-healing is off — a default this bootstrap does not change, not a platform limit; turn it on from the Self-Heal page.'
                      : cloud.os_family === 'linux'
                        ? 'Linux — published PyPI release + systemd --user, via nyxgpt ops install'
                        : 'not recorded — this deploy predates the `nyxgpt cloud deploy --os` flag'
                  }
                />
                {/* #3956: which substrate the instance runs. Observed, never
                    driven — switching substrates rebuilds the machine this
                    page may itself be served from, which is exactly the class
                    of action the Definition of Done keeps in the CLI (#3804).
                    'unknown' rather than 'native' when nothing was recorded,
                    for the same reason the session backend above says 'not
                    recorded': a deploy predating the flag is not a claim
                    about what is running. */}
                <Row
                  label="Substrate"
                  value={
                    cloud.substrate === 'kubernetes'
                      ? 'single-node k3s cluster on the instance, running k8s/*.yaml — canary rollout available via `nyxgpt cloud canary`'
                      : cloud.substrate === 'native'
                        ? 'native services on the instance — `nyxgpt cloud deploy --kubernetes` deploys onto a cluster instead, which is what canary rollout needs'
                        : 'not recorded — this deploy predates the substrate record; `nyxgpt cloud ops status` reports what the instance is actually running'
                  }
                />
                <Row label="Observability profiles" value={cloud.profiles.join(', ')} />
                {/* #3865: a cloud deploy used to run the back-compat `file`
                    backend silently, so chats lived as JSON on the instance's
                    disk and no other mode could see them. Reported here
                    because it is the kind of state that is invisible until
                    someone goes looking for a session that is not there.
                    Observed, never driven — the pointer below names the
                    wrapped command that changes it. */}
                <Row
                  label="Chat sessions"
                  value={
                    cloud.session_backend === 'cassandra'
                      ? 'Cassandra (nyxgpt.chat_sessions) — shared with every mode pointed at the same Cassandra'
                      : cloud.session_backend === 'file'
                        ? 'JSON files on the instance’s own disk — not shared with any other mode, and lost with the instance'
                        : 'not recorded — this deploy predates the session-backend flag; `nyxgpt cloud ops session-backend` reports what the instance is actually running'
                  }
                />
                <Row
                  label="Access tunnel"
                  value={
                    cloud.on_instance
                      ? 'not applicable — the tunnel is opened from the operator’s machine, not this one'
                      : cloud.tunnel.running
                        ? `open (pid ${cloud.tunnel.pid})`
                        : 'closed'
                  }
                />
                <Row label="Stack health" value={healthLabel(cloud.health)} />
                {/* #4121, macOS only. There is no screen to share on a Linux
                    instance, so a row claiming one is closed would answer a
                    question that does not apply. Observed, never operated
                    (D-017): the row reports the path and names the command,
                    and this page has no button that opens it -- a UI cannot
                    safely drive access to the substrate serving it. */}
                {cloud.os_family === 'macos' && !cloud.on_instance && (
                  <Row
                    label="Mac screen path"
                    value={
                      cloud.screen?.running
                        ? `open at ${cloud.screen.url} (pid ${cloud.screen.pid}) — close it with \`${cloud.screen.stop_command}\``
                        : cloud.screen?.configured
                          ? `Screen Sharing is enabled on the Mac (loopback only) but no tunnel is open — \`${cloud.screen.command}\` re-opens it`
                          : `not set up — \`${cloud.screen?.command ?? 'nyxgpt cloud screen'}\` opens one`
                    }
                  />
                )}
              </ul>

              {/* The connection target (#3813). Reported, not offered: this
                  page never opens an SSH session, it says what the wrapped
                  command connects to so an operator does not have to
                  reconstruct it from a deploy's scrollback. */}
              <div style={{ marginTop: '1rem' }}>
                <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                  Connection target
                </h3>
                {cloud.connection?.known ? (
                  <>
                    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                      <Row label="SSH target" value={cloud.connection.target} />
                      <Row
                        label="Identity file"
                        value={
                          cloud.connection.identity_file ||
                          '(ssh’s own ~/.ssh defaults and agent)'
                        }
                      />
                    </ul>
                    {cloud.connection.tunnel_invocation && (
                      <p
                        style={{
                          fontSize: '0.75rem',
                          color: 'var(--foreground-muted)',
                          marginTop: '0.5rem',
                        }}
                      >
                        Diagnostics — what <code>{cloud.connection.command}</code> executes on your
                        behalf. Run the wrapped command, not this:
                        <br />
                        <code style={{ wordBreak: 'break-all' }}>
                          {cloud.connection.tunnel_invocation}
                        </code>
                      </p>
                    )}
                  </>
                ) : (
                  <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
                    Not reportable from here — {cloud.connection?.reason}.
                  </p>
                )}
              </div>

              {Object.keys(cloud.urls).length > 0 && !cloud.on_instance && (
                <div style={{ marginTop: '1rem' }}>
                  <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>URLs</h3>
                  <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                    Every one is a <code>localhost</code> address forwarded over the tunnel — there
                    is no instance-facing URL, by design. They resolve only while the tunnel is
                    open.
                  </p>
                  <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                    {Object.entries(cloud.urls).map(([name, url]) => (
                      <li key={name} style={{ padding: '2px 0' }}>
                        <span style={{ color: 'var(--foreground-muted)' }}>{name}</span>{' '}
                        <code>{url}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          {/* #3995. Outside the known/unknown branch above, deliberately: the
              ordinary end state of a macOS teardown is "no deployment, one
              Dedicated Host still billing until tomorrow", and a block that
              only rendered for a live deployment would hide the single
              remaining charge at exactly the moment it is all that is left.
              Observed, never driven (Definition of Done): the release is
              already scheduled in AWS, and the pointer names the wrapped
              command that schedules it when it is not. */}
          {cloud?.mac_host?.host_id && (
            <div style={{ marginTop: '1rem' }}>
              {/* #4136. "Still billing" is a claim about AWS, so it is only made
                  when AWS confirmed the host. An unconfirmed record says so
                  instead: this panel reported "still billing" over a host that
                  had been released three days earlier, because the local record
                  was the only thing anything asked. */}
              {/* #4181. The gate is `usable`, not `verified_at`: a
                  confirmation over a record whose own fields contradict each
                  other confirms a host but not the block describing it, and
                  this heading said "still billing" directly above the
                  INCOHERENT warnings about the same block. */}
              <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                {cloud.mac_host.usable
                  ? 'EC2 Mac Dedicated Host — still billing'
                  : 'EC2 Mac Dedicated Host — recorded here, not confirmed at AWS'}
              </h3>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                AWS bills an allocated Dedicated Host for a 24-hour minimum and refuses to release
                one before that window closes, so <code>nyxgpt cloud destroy</code> terminates the
                Mac immediately and defers only the host release. This host outlives the instance
                and the deploy record by design.
              </p>
              {/* #4181 finding 6: the REASON, not the remedy for a reason
                  nobody checked. This used to say "nothing has asked AWS
                  about this host yet" whatever the cause — including a run
                  whose boto3 was missing and a run that asked the wrong
                  account. `provenance` carries whichever applied. */}
              {!cloud.mac_host.usable && (
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                  {cloud.mac_host.provenance ??
                    'Nothing has asked AWS about this host in this run.'}{' '}
                  Every row below is what this machine RECORDED, not what AWS reports: none of it
                  is evidence that the host exists, that it is billing, or that its release is
                  scheduled. <code>nyxgpt cloud status</code> asks, and clears the block if the
                  host is gone.
                </p>
              )}
              {(cloud.mac_host.incoherent?.length ?? 0) > 0 && (
                <p style={{ fontSize: '0.8rem', color: 'var(--danger, #c0392b)', marginBottom: '0.5rem' }}>
                  This record is internally inconsistent, so its fields were written by different
                  runs about different hosts and cannot be read together:
                  {' '}
                  {cloud.mac_host.incoherent?.join('; ')}. Run{' '}
                  <code>nyxgpt cloud status</code>, which asks AWS and rebuilds it.
                </p>
              )}
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                <Row
                  label="Host"
                  value={`${cloud.mac_host.host_id}${
                    cloud.mac_host.instance_type ? ` (${cloud.mac_host.instance_type})` : ''
                  }`}
                />
                <Row
                  label="Location"
                  value={`${cloud.mac_host.region || 'unknown'} / ${
                    cloud.mac_host.availability_zone || 'unknown'
                  }`}
                />
                <Row label="Allocated" value={cloud.mac_host.allocated_at || 'unknown'} />
                <Row
                  label="Releasable at"
                  value={
                    cloud.mac_host.releasable_now
                      ? `${cloud.mac_host.release_at || 'unknown'} — that moment has passed`
                      : `${cloud.mac_host.release_at || 'unknown'} (AWS’s 24-hour minimum)`
                  }
                />
                {/* #4181 finding 3. "The scheduled release has fired" and "a
                    one-shot AWS schedule releases it" are statements about
                    EventBridge, and they were rendered from the same fields
                    the INCOHERENT warning above had just disqualified. The
                    recorded value is still shown; the conclusion is not. */}
                <Row
                  label="Release"
                  value={
                    !cloud.mac_host.usable
                      ? `recorded as ${
                          cloud.mac_host.release_scheduled ? 'scheduled' : 'NOT scheduled'
                        } — not verified in this run, so nyxGPT cannot say whether anything will release this host. \`${
                          cloud?.commands?.destroy ?? 'nyxgpt cloud destroy --yes'
                        }\` schedules it and reports what AWS said`
                      : cloud.mac_host.release_scheduled && cloud.mac_host.releasable_now
                      ? 'the scheduled release has fired — Slack has the outcome. Not “released”: nothing here watched it. The block clears as soon as AWS confirms the host is gone, which `nyxgpt cloud status` asks'
                      : cloud.mac_host.release_scheduled
                      ? 'scheduled — a one-shot AWS schedule releases it and reports the outcome to Slack'
                      : `not scheduled yet — \`${
                          cloud?.commands?.destroy ?? 'nyxgpt cloud destroy --yes'
                        }\` terminates the Mac and schedules it`
                  }
                />
                <Row
                  label="Confirmed at AWS"
                  value={
                    cloud.mac_host.usable
                      ? `${cloud.mac_host.verified_at} in ${
                          cloud.mac_host.observation?.account_label || 'the resolved account'
                        }`
                      : cloud.mac_host.provenance ??
                        'never — nothing here has asked AWS whether this host exists'
                  }
                />
                {/* #4136. AWS's figure when there is one, and the local
                    estimate named as an estimate when there is not. A number
                    that is not the bill may be shown; it may not be shown as
                    the bill. */}
                <Row
                  label="Spend"
                  value={
                    cloud.mac_host.accrued_cost !== null && cloud.mac_host.accrued_cost !== undefined
                      ? `${cloud.mac_host.currency || 'USD'} ${cloud.mac_host.accrued_cost.toFixed(2)} from AWS Cost Explorer${
                          cloud.mac_host.spend_through ? ` through ${cloud.mac_host.spend_through}` : ''
                        } (as of ${cloud.mac_host.spend_as_of || 'unknown'})`
                      : cloud.mac_host.estimated_cost !== null &&
                        cloud.mac_host.estimated_cost !== undefined &&
                        cloud.mac_host.hourly_rate
                      ? `no AWS figure (${
                          cloud.mac_host.spend_error ||
                          cloud.mac_host.observation?.reason ||
                          'AWS has not been asked yet'
                        }). Local ESTIMATE only: ${cloud.mac_host.currency || 'USD'} ${cloud.mac_host.estimated_cost.toFixed(
                          2,
                        )} at $${cloud.mac_host.hourly_rate.toFixed(4)}/hour, counted from the recorded allocation time — it keeps counting whether or not AWS is still charging`
                      : 'unknown — no AWS figure and no rate was recorded for this host'
                  }
                />
              </ul>
            </div>
          )}
        </div>

        <div style={boxStyle}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Terraform state backend</h2>
            <span style={badgeStyle(Boolean(cloudState?.remote_enabled), substrate?.on_ec2 || !cloudState)}>
              {substrate?.on_ec2
                ? 'NOT ON THIS MACHINE'
                : !cloudState
                  ? 'UNKNOWN'
                  : cloudState.remote_enabled
                    ? 'S3 + DYNAMODB LOCK'
                    : 'LOCAL FILE'}
            </span>
          </div>

          {substrate?.on_ec2 ? (
            <p style={{ fontSize: '0.875rem' }}>
              Terraform state lives on the machine that provisioned the substrate, not on the
              instance — there is nothing here to report. Read it with{' '}
              <code>nyxgpt cloud state status</code> there.
            </p>
          ) : !cloudState ? (
            <p style={{ fontSize: '0.875rem' }}>Unknown — the state backend could not be read.</p>
          ) : (
            <>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                {cloudState.remote_enabled
                  ? 'State is shared and locked: concurrent applies block instead of racing, and every write keeps its predecessor in the bucket for recovery.'
                  : 'State is a single local file on this machine. A second operator or a CI runner applying the same substrate cannot see it, and two concurrent applies can corrupt it. `nyxgpt cloud state migrate` moves it to a versioned, encrypted bucket with a DynamoDB lock.'}
              </p>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                <Row label="Backend" value={cloudState.backend} />
                <Row label="Locking" value={cloudState.locking} />
                {cloudState.remote_enabled ? (
                  <>
                    <Row label="Bucket" value={cloudState.bucket} />
                    <Row label="Object key" value={cloudState.key} />
                    <Row label="Lock table" value={cloudState.table} />
                    <Row label="Region" value={cloudState.region} />
                  </>
                ) : (
                  <Row label="State file" value={cloudState.local_state_file} />
                )}
              </ul>
            </>
          )}
        </div>

        {/* Written by `nyxgpt.cloud_deploy` itself, so a deploy run from a
            terminal appears here too. */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Deploy history
          </h2>
          {cloud && cloud.history.length > 0 ? (
            <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.85rem' }}>
              {cloud.history.map((entry, index) => (
                <li
                  key={`${entry.ts}-${index}`}
                  style={{
                    padding: '0.4rem 0',
                    borderBottom: '1px solid var(--border-color)',
                    display: 'flex',
                    gap: '0.75rem',
                    alignItems: 'baseline',
                  }}
                >
                  <span
                    style={{
                      fontSize: '0.7rem',
                      fontWeight: 700,
                      color: entry.outcome === 'succeeded' ? '#22c55e' : '#ef4444',
                      minWidth: 70,
                    }}
                  >
                    {entry.outcome}
                  </span>
                  <span>
                    {historyLabel(entry)}
                    {entry.detail ? (
                      <span style={{ color: 'var(--foreground-muted)' }}> — {entry.detail}</span>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
              No deploy or teardown has been recorded on this machine — the history is written
              wherever <code>nyxgpt cloud deploy</code> ran.
            </p>
          )}
        </div>

        {/* --- Pointers, not buttons. Rendered from the backend's
            LIFECYCLE_COMMANDS so this list cannot drift from what the CLI
            accepts, and every entry is a wrapped `nyxgpt` command. --- */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Cloud lifecycle commands
          </h2>
          <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', marginBottom: '1rem' }}>
            None of these is a dashboard button, and none of them should be. They create, change
            and delete real billed infrastructure — including the machine this dashboard may be
            served from — so they are run deliberately from a terminal:
          </p>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
            {[
              ['Deploy or redeploy the stack', cloud?.commands?.deploy ?? 'nyxgpt cloud deploy'],
              ['Tear the whole deployment down', cloud?.commands?.destroy ?? 'nyxgpt cloud destroy --yes'],
              [
                'Run the end-to-end cloud test (deploys, verifies chat/RAG/observability, then tears down)',
                cloud?.commands?.smoke ?? 'nyxgpt cloud smoke',
              ],
              ['Show this state from a terminal', cloud?.commands?.status ?? 'nyxgpt cloud status'],
              ['Open the access tunnel', cloud?.commands?.tunnel ?? 'nyxgpt cloud tunnel'],
              ['Close it again', cloud?.commands?.tunnel_stop ?? 'nyxgpt cloud tunnel --stop'],
              // #4121. Only on a macOS deployment: naming it on a Linux one
              // would advertise a capability that box has not got.
              ...(cloud?.os_family === 'macos'
                ? ([
                    [
                      'Open the Mac’s screen',
                      cloud?.commands?.screen ?? 'nyxgpt cloud screen',
                    ],
                  ] as Array<[string, string]>)
                : []),
              [
                // Not "containers" (#4161): an EC2 Mac target runs the stack
                // as `brew services` and can host no Docker daemon at all.
                'Inspect what the instance is running',
                cloud?.commands?.ops_status ?? 'nyxgpt cloud ops status',
              ],
              ['Diagnose the instance', cloud?.commands?.doctor ?? 'nyxgpt cloud ops doctor'],
              [
                'Read the observability logins',
                cloud?.commands?.credentials ?? 'nyxgpt cloud credentials',
              ],
              ['Re-allow SSH after your public IP changes', cloud?.commands?.allow_ip ?? 'nyxgpt cloud allow-ip'],
              ['Preview a substrate change without creating anything', 'nyxgpt cloud infra plan'],
              ['Move Terraform state to S3 with a DynamoDB lock', 'nyxgpt cloud state migrate'],
              ['List, restore or unlock stored state versions', 'nyxgpt cloud state versions'],
            ].map(([label, command]) => (
              <li key={command} style={{ padding: '0.3rem 0' }}>
                <span style={{ color: 'var(--foreground-muted)' }}>{label}</span>
                <br />
                <code>{command}</code>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

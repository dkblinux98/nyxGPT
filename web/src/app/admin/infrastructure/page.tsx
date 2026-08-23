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
  };
  native: Record<string, string>;
  compose: Record<string, string>;
  compose_probe_available: boolean;
  // Why the probe could not run, when it could not (#3812) -- reported so the
  // page can name the cause ("`docker compose ps` exited 125: permission
  // denied ...") instead of only saying it can't tell.
  compose_probe_reason?: string;
  conflicts: string[];
  terraform: {
    probe_available: boolean;
    deployed: boolean;
    containers: Record<string, string>;
    // The Terraform deployment's OWN install mode (#3835): 'artifact' (the
    // published container images, the repo-less default) or 'dev' (images
    // built from a checkout's working tree, `--dev`). Reported separately
    // from `install_mode` above because that one describes the native
    // services, which are a different deployment and frequently in the other
    // mode. Optional so the page still renders against an api process from
    // before #3835; `recorded` is false when no Terraform install has ever
    // written the marker, so the page can stay silent instead of asserting a
    // default -- and when something IS deployed with no marker, say so
    // instead (see `terraformImageMode`).
    install_mode?: {
      mode: 'artifact' | 'dev';
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
      mode: 'artifact' | 'dev';
      checkout: string | null;
      label: string;
      recorded: boolean;
    };
    // The in-cluster observability layer (#3787): Kubernetes mode cannot use
    // the Compose observability profiles, so it deploys its own. Optional on
    // purpose: an older api that predates this field must degrade to "NOT
    // DEPLOYED", not take the whole Infrastructure page down with it (#3468).
    observability?: {
      probe_available: boolean;
      deployed: boolean;
      workloads: Record<string, string>;
      // The same three states the Pod list above badges (#3827). Without it
      // this section rendered raw `"0/1 ready"`/`"1/1 ready"`/`"absent"`
      // strings in undifferentiated grey -- a workload that is up, one still
      // rolling out and one that never deployed all looked identical, on the
      // same card that badges every Pod READY/PENDING/FAILED. Optional, so an
      // older api falls back to those plain lines.
      workload_states?: {
        name: string;
        state: 'ready' | 'pending' | 'failed' | string;
        summary: string;
        details: string;
      }[];
      port_forward_command: string;
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
// running on; `terraform-state` = read from the state file on this machine;
// `none` = neither, which is *unknown* and never "not provisioned".
type SubstrateSource = 'imds' | 'terraform-state' | 'none';

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
  owner_ip_cidr: string;
  access_model: { open_ports: number[]; ssh_only: boolean; reachability: string };
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
  hourly_rate: number | null;
  accrued_cost: number | null;
  currency: string;
  releasable_now: boolean;
  billing: boolean;
};

type CloudDeployStatus = {
  source: 'deploy-record' | 'local-instance' | 'deploy-attempt' | 'substrate-record' | 'none';
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

// A Pod is ready, still starting, or broken -- the same three states
// `nyxgpt ops` prints as [OK]/[PENDING]/[FAIL] (#3827). Pending is amber
// rather than red on purpose: it is a normal stage of a rollout, and colouring
// it as a failure is the browser version of the defect this fixed.
function podStateBadgeStyle(state: string): React.CSSProperties {
  const color = state === 'ready' ? '#22c55e' : state === 'pending' ? '#f59e0b' : '#ef4444';
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

function ComponentList({ components }: { components: Record<string, string> }) {
  const entries = Object.entries(components);
  if (entries.length === 0) {
    return <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>None running.</p>;
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
      {entries.map(([component, state]) => (
        <li key={component} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
          <span>{component}</span>
          <span style={{ color: state === 'running' || state === 'started' ? '#22c55e' : 'var(--foreground-muted)' }}>
            {state}
          </span>
        </li>
      ))}
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
  const loadCloud = useCallback(async () => {
    setCloudError(null);
    try {
      const [deployRes, stateRes] = await Promise.all([
        fetch('/api/v1/cloud/deploy?probe_health=true', { cache: 'no-store' }),
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
              <span style={badgeStyle(status.install_mode?.mode !== 'dev', false)}>
                {status.install_mode?.mode === 'dev' ? 'DEV INSTALL' : 'ARTIFACT INSTALL'}
              </span>
            </div>
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
            <ComponentList components={status.native} />
          </div>

          {/* --- Compose --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Docker Compose</h2>
              {!status.compose_probe_available && (
                <span style={badgeStyle(false, true)}>CANNOT DETERMINE</span>
              )}
            </div>

            {!status.compose_probe_available ? (
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
                wherever this API process is running.
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
                No kubeconfig current-context configured — no cluster to detect.
              </p>
            ) : !status.kubernetes.probe_available ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                Cannot determine from this deployment mode — the cluster wasn&apos;t reachable.
              </p>
            ) : (
              <>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                  Context: <code>{status.kubernetes.context}</code>
                  {status.kubernetes.provisioned
                    ? ' — local kind cluster provisioned by nyxgpt (torn down together on `nyxgpt ops down --kubernetes`).'
                    : ' — bring-your-own cluster (never destroyed by `nyxgpt ops down --kubernetes`).'}
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
                      <strong>unrecorded</strong> — no marker for this deployment on the machine
                      this dashboard runs on. It was deployed before nyxGPT recorded one, or from
                      another machine.
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
                </p>
                {status.kubernetes.pod_states && status.kubernetes.pod_states.length > 0 ? (
                  /* Three states, not two (#3827): a Pod that is still pulling its
                     image is PENDING, not a failure -- the install used to print
                     [FAIL] for exactly this and buried the one Pod that really
                     could not start. FAILED carries the scheduler's/kubelet's own
                     reason, because "Pending" on its own does not distinguish
                     "downloading" from "this node cannot fit it". */
                  <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.8rem' }}>
                    {status.kubernetes.pod_states.map((pod) => (
                      <li key={pod.name} style={{ padding: '3px 0' }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem' }}>
                          <span style={{ fontFamily: 'monospace' }}>{pod.name}</span>
                          <span style={podStateBadgeStyle(pod.state)}>
                            {pod.state === 'ready' ? 'READY' : pod.state === 'pending' ? 'PENDING' : 'FAILED'}
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
                        /* Badged with the same three states as the Pods above (#3827):
                           `0/1 ready` is PENDING, not a quiet grey line the operator
                           has to interpret against a Pod list that already ruled on
                           the same condition two sections up. */
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
                      <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
                        Services are ClusterIP-only. Publish Grafana, Prometheus, Jaeger and
                        GlitchTip on the ports this dashboard links to with{' '}
                        <code>{status.kubernetes.observability.port_forward_command}</code>.
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
              {cloud.source !== 'deploy-attempt' ||
              cloud.instance_id ||
              cloud.host ||
              cloud.instance_type ? (
                <>
                  An instance exists and is being billed — this is not the same as nothing being
                  deployed, and not the same as unknown. Re-run{' '}
                  <code>{cloud.commands?.deploy ?? 'nyxgpt cloud deploy'}</code> (idempotent), or{' '}
                  <code>{cloud.commands?.destroy ?? 'nyxgpt cloud destroy --yes'}</code> to tear it
                  down.
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
                {cloud.on_instance
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
                {/* #3867: the two target OSes are provisioned by different
                    bootstraps and do not leave the instance in the same
                    shape — an EC2 Mac runs the Homebrew formulas under
                    launchd with no observability stack and no self-heal
                    watchdog. Reported here because nothing else on this page
                    distinguishes them. Observed, never driven: the pointer
                    is `nyxgpt cloud deploy --os`. */}
                <Row
                  label="Target OS"
                  value={
                    cloud.os_family === 'macos'
                      ? 'macOS (EC2 Mac) — remote Homebrew tap + brew services; no observability stack, no self-heal watchdog'
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
              <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                EC2 Mac Dedicated Host — still billing
              </h3>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                AWS bills an allocated Dedicated Host for a 24-hour minimum and refuses to release
                one before that window closes, so <code>nyxgpt cloud destroy</code> terminates the
                Mac immediately and defers only the host release. This host outlives the instance
                and the deploy record by design.
              </p>
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
                <Row
                  label="Release"
                  value={
                    cloud.mac_host.release_scheduled && cloud.mac_host.releasable_now
                      ? 'the scheduled release has fired — Slack has the outcome. Not “released”: nothing here watched it, and the row clears on the next lifecycle command, which asks AWS'
                      : cloud.mac_host.release_scheduled
                      ? 'scheduled — a one-shot AWS schedule releases it and reports the outcome to Slack'
                      : `not scheduled yet — \`${
                          cloud?.commands?.destroy ?? 'nyxgpt cloud destroy --yes'
                        }\` terminates the Mac and schedules it`
                  }
                />
                <Row
                  label="Accrued"
                  value={
                    cloud.mac_host.accrued_cost !== null && cloud.mac_host.hourly_rate
                      ? `$${cloud.mac_host.accrued_cost.toFixed(2)} at $${cloud.mac_host.hourly_rate.toFixed(4)}/hour (the 24-hour minimum is charged either way)`
                      : 'unknown — no rate was recorded for this host'
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
              [
                'Inspect the containers running on the instance',
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

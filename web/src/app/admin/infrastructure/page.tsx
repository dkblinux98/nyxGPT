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
  };
  kubernetes: {
    available: boolean;
    configured: boolean;
    probe_available: boolean;
    deployed: boolean;
    namespace: string;
    pods: string[];
    // Pods no node would take (#3825). Optional so an api that predates the
    // field degrades to "none reported" instead of breaking the page.
    unschedulable?: string[];
    context: string;
    provisioned: boolean;
    // The in-cluster observability layer (#3787): Kubernetes mode cannot use
    // the Compose observability profiles, so it deploys its own. Optional on
    // purpose: an older api that predates this field must degrade to "NOT
    // DEPLOYED", not take the whole Infrastructure page down with it (#3468).
    observability?: {
      probe_available: boolean;
      deployed: boolean;
      workloads: Record<string, string>;
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

type CloudDeployStatus = {
  source: 'deploy-record' | 'local-instance' | 'none';
  known: boolean;
  on_instance: boolean;
  deployed: boolean;
  version: string;
  host: string;
  instance_id: string;
  instance_type: string;
  region: string;
  profiles: string[];
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
        <code>nyxgpt ops install --terraform --local</code> and{' '}
        <code>nyxgpt ops install --kubernetes --local</code> — see <code>docs/terraform.md</code>{' '}
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
              <span style={badgeStyle(status.terraform.deployed, !status.terraform.probe_available)}>
                {!status.terraform.probe_available
                  ? 'CANNOT DETERMINE'
                  : status.terraform.deployed
                    ? 'DEPLOYED'
                    : 'NOT DEPLOYED'}
              </span>
            </div>

            <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
              The <code>nyxgpt-tf-*</code> containers Terraform runs on <em>this</em> machine. An
              AWS instance that Terraform provisioned is a different thing and is reported under
              AWS below.
            </p>

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
                {status.kubernetes.pods.length > 0 ? (
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
                      <code>nyxgpt ops install --kubernetes --local</code> — it checks the node&apos;s
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
                      <ComponentList components={status.kubernetes.observability.workloads} />
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
                      <code>nyxgpt ops observability --kubernetes --local</code> (
                      <code>nyxgpt ops install --kubernetes --local</code> includes them unless{' '}
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
            <span style={badgeStyle(Boolean(cloud?.known), !cloud?.known)}>
              {cloud?.known ? 'DEPLOYED' : 'UNKNOWN'}
            </span>
          </div>

          {!cloud?.known ? (
            <p style={{ fontSize: '0.875rem' }}>
              Unknown from this machine — no deploy has been recorded here and this is not the
              instance. Run <code>{cloud?.commands?.status ?? 'nyxgpt cloud status'}</code>{' '}
              where the deploy was run.
            </p>
          ) : (
            <>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                {cloud.on_instance
                  ? 'Read first-hand: this dashboard is served by the deployed stack itself, so the release below is the one answering this request.'
                  : 'What `nyxgpt cloud deploy` last put on the instance: a published nyxGPT release (never a copy of a repository) and the observability profiles it enabled.'}
              </p>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                <Row label="Installed version" value={cloud.version} />
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
                <Row label="Observability profiles" value={cloud.profiles.join(', ')} />
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

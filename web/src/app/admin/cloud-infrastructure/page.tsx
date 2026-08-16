'use client';

// SRE/admin surface for the AWS substrate (P6-8, #3509) and the deployment on
// it (P6-11, #3513) -- the dashboard counterpart of `nyxgpt cloud infra` and
// `nyxgpt cloud deploy`.
//
// **This page does not deploy or tear anything down.** The owner's 2026-08-09
// decision on P6-15 (#3514) extends the #3410 status-only precedent from the
// local Infrastructure page to cloud: the cloud surface is
// status-plus-CLI-pointers, because cloud lifecycle actions are rare,
// consequential operations for which a deliberate CLI invocation is the safer
// surface. So the Apply, Deploy and Destroy controls that #3509/#3513 shipped
// here are gone, replaced by the wrapped `nyxgpt` commands that own them
// (rendered from the backend's own LIFECYCLE_COMMANDS, so this page can never
// drift from what the CLI actually accepts).
//
// What stays interactive is deliberately narrow and follows the decision's
// own rationale -- neither rare nor consequential nor irreversible:
//   * Plan, which previews what would change and creates nothing.
//   * The access tunnel, which is a local SSH forward, not a lifecycle action;
//     closing and reopening it changes nothing in AWS.
//   * The Terraform state panel (P6-9, #3510), a separate subsystem with its
//     own merged acceptance. The #3514 decision names `nyxgpt cloud deploy` /
//     `nyxgpt cloud destroy`, so it is left as it was rather than reworked on
//     an inference.

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type AccessModel = {
  open_ports: number[];
  ssh_only: boolean;
  world_open_ingress: boolean;
  reachability: string;
};

type CloudInfraStatus = {
  provisioned: boolean;
  config_synced: boolean;
  state_file: string;
  state_file_exists: boolean;
  region: string;
  instance_id: string;
  instance_type: string;
  public_ip: string;
  private_ip: string;
  vpc_id: string;
  security_group_id: string;
  ssh_key_name: string;
  owner_ip_cidr: string;
  access_model: AccessModel;
};

type ProvisionInputs = {
  region: string;
  ssh_key_name: string;
  ssh_public_key: string;
  instance_type: string;
  owner_ip: string;
};

// Terraform remote state (P6-9, #3510). A fresh install keeps state in one
// local file on whoever's machine ran the last apply; migrating it to S3 with
// a DynamoDB lock is what makes a second operator (or CI) safe to run.
type CloudStateStatus = {
  backend: string;
  remote_enabled: boolean;
  bootstrapped: boolean;
  bucket: string;
  table: string;
  key: string;
  region: string;
  locking: string;
  local_state_file: string;
  local_state_exists: boolean;
};

type StateVersion = {
  version_id: string;
  last_modified: string;
  size: number;
  latest: boolean;
};

// The deployment itself (P6-11, #3513): what release is installed on the
// instance, and whether the SSH tunnel that is the only way to reach it is
// currently open. Every URL is a localhost one -- nothing on the instance is
// reachable without the tunnel, by design.
type TunnelStatus = {
  running: boolean;
  pid: number;
  host: string;
  profiles: string[];
  urls: Record<string, string>;
};

// Whether the deployed stack answers /health through the tunnel. `checked` is
// false when the backend was not asked to probe, or when there is no tunnel
// open to probe through -- which is a different thing from "unhealthy", and is
// rendered as such.
type DeployHealth = {
  checked: boolean;
  healthy: boolean;
  status: number;
  url?: string;
  reason: string;
};

// One deploy or teardown, as recorded by whichever surface ran it (P6-15,
// #3514). The CLI writes these too, so the dashboard shows the terminal's
// history and not just its own.
type DeployHistoryEntry = {
  ts: number;
  action: string;
  outcome: string;
  version?: string;
  host?: string;
  instance_id?: string;
  region?: string;
  detail?: string;
};

type CloudDeployStatus = {
  deployed: boolean;
  version: string;
  host: string;
  instance_id: string;
  region: string;
  profiles: string[];
  tunnel: TunnelStatus;
  health: DeployHealth;
  history: DeployHistoryEntry[];
  urls: Record<string, string>;
  access_command: string;
  commands: Record<string, string>;
};

const boxStyle: React.CSSProperties = {
  padding: '1.5rem',
  backgroundColor: 'var(--background-secondary)',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
};

const fieldStyle: React.CSSProperties = {
  width: '100%',
  padding: '0.5rem',
  borderRadius: '0.375rem',
  border: '1px solid var(--border-color)',
  background: 'var(--background)',
  color: 'var(--foreground)',
  fontSize: '0.875rem',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: '0.8rem',
  fontWeight: 600,
  marginBottom: '0.25rem',
};

function buttonStyle(disabled: boolean, danger = false): React.CSSProperties {
  return {
    padding: '0.5rem 1rem',
    border: `1px solid ${danger ? '#ef4444' : 'var(--border-color)'}`,
    borderRadius: '0.375rem',
    fontSize: '0.875rem',
    fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer',
    background: danger ? '#ef4444' : 'var(--background)',
    color: danger ? 'white' : 'var(--foreground)',
    opacity: disabled ? 0.6 : 1,
  };
}

function errorText(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    const record = data as Record<string, unknown>;
    const nested = record.error;
    if (nested && typeof nested === 'object') {
      const message = (nested as Record<string, unknown>).message;
      if (typeof message === 'string') return message;
    }
    if (typeof nested === 'string') return nested;
    if (typeof record.detail === 'string') return record.detail;
  }
  return fallback;
}

// "Not checked" is reported as its own answer rather than collapsed into
// "unhealthy": a closed tunnel means the stack is unreachable from here, which
// says nothing about whether it is running on the instance.
function healthLabel(health: DeployHealth | undefined): string {
  if (!health) return '';
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

export default function CloudInfrastructurePage() {
  const [status, setStatus] = useState<CloudInfraStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<'' | 'plan'>('');
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [stateMessage, setStateMessage] = useState<string | null>(null);
  const [inputs, setInputs] = useState<ProvisionInputs>({
    region: '',
    ssh_key_name: '',
    ssh_public_key: '',
    instance_type: '',
    owner_ip: '',
  });

  const [stateStatus, setStateStatus] = useState<CloudStateStatus | null>(null);
  const [stateBusy, setStateBusy] = useState<'' | 'migrate' | 'unlock' | 'versions' | 'restore'>(
    ''
  );
  const [stateError, setStateError] = useState<string | null>(null);
  const [versions, setVersions] = useState<StateVersion[] | null>(null);
  const [lockId, setLockId] = useState('');
  // Restore is armed per version rather than fired on the click that selects
  // it -- see the confirmation note where it is rendered.
  const [pendingRestore, setPendingRestore] = useState<string | null>(null);

  const [deployStatus, setDeployStatus] = useState<CloudDeployStatus | null>(null);
  const [deployBusy, setDeployBusy] = useState<'' | 'refresh' | 'tunnel' | 'tunnel-stop'>('');
  const [deployError, setDeployError] = useState<string | null>(null);
  const [deployMessage, setDeployMessage] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/cloud/infra', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
      setStatus(data as CloudInfraStatus);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Kept separate from loadStatus, down to its own error slot: the two are
  // independent subsystems, and a shared `error` would let whichever request
  // finished last overwrite the other panel's failure with its own.
  const loadStateStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/cloud/state', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
      setStateStatus(data as CloudStateStatus);
    } catch (e: unknown) {
      setStateError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Third independent subsystem, third error slot, same reasoning as above.
  //
  // `probe_health=true` is what turns "we recorded a deploy" into "the stack
  // is answering right now". It costs one short request through the tunnel,
  // which is why the backend makes it opt-in and why this page asks for it
  // only on load and on an explicit Refresh, never on a timer.
  const loadDeployStatus = useCallback(async () => {
    setDeployError(null);
    try {
      const res = await fetch('/api/v1/cloud/deploy?probe_health=true', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
      setDeployStatus(data as CloudDeployStatus);
    } catch (e: unknown) {
      setDeployError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshDeployStatus = useCallback(async () => {
    setDeployBusy('refresh');
    setDeployMessage(null);
    try {
      await loadDeployStatus();
    } finally {
      setDeployBusy('');
    }
  }, [loadDeployStatus]);

  useEffect(() => {
    void loadStatus();
    void loadStateStatus();
    void loadDeployStatus();
  }, [loadStatus, loadStateStatus, loadDeployStatus]);

  // Plan is the one substrate action this page still runs, and it is safe to:
  // it reports what an apply *would* change and creates nothing. Applying it
  // is `nyxgpt cloud infra apply` (or `nyxgpt cloud deploy`, which applies as
  // its first step) -- see the file header for why that is not a button.
  const runPlan = useCallback(async () => {
    setBusy('plan');
    setError(null);
    setMessage(null);
    try {
      // Empty strings mean "unchanged" -- the backend falls back to the
      // settings the last run saved, exactly as the CLI flags do.
      const body: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(inputs)) {
        if (value.trim()) body[key] = value.trim();
      }

      const res = await fetch('/api/v1/cloud/infra/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store',
      });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));

      setMessage('Plan complete. Nothing was created or changed.');
      await loadStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [inputs, loadStatus]);

  const runStateAction = useCallback(
    async (action: 'migrate' | 'unlock' | 'versions' | 'restore', versionId?: string) => {
      setStateBusy(action);
      setStateError(null);
      setStateMessage(null);
      try {
        if (action === 'versions') {
          const res = await fetch('/api/v1/cloud/state/versions', { cache: 'no-store' });
          const data = await res.json();
          if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
          setVersions((data.versions ?? []) as StateVersion[]);
          // A refreshed list is a different list -- never carry an armed
          // selection over onto whatever now sits in that row.
          setPendingRestore(null);
          return;
        }

        const body: Record<string, unknown> = {};
        if (action === 'unlock') body.lock_id = lockId.trim();
        if (action === 'restore') {
          body.version_id = versionId;
          // The API refuses a restore without this, deliberately. It is only
          // ever set here, on the second click of the arm-then-confirm pair --
          // sending it from the button that merely picks a version would turn
          // the backend's guard into a formality.
          body.confirm = true;
        }

        const res = await fetch(`/api/v1/cloud/state/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          cache: 'no-store',
        });
        const data = await res.json();
        if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));

        if (action === 'migrate') {
          const backend = (data.backend ?? {}) as Record<string, string>;
          setStateMessage(
            `Remote state active: s3://${backend.bucket}/${backend.key}, locked by DynamoDB table ${backend.table}. Concurrent applies now block instead of racing.`
          );
        } else if (action === 'unlock') {
          setStateMessage(`Released lock ${lockId.trim()}.`);
          setLockId('');
        } else {
          setStateMessage(
            `Restored version ${versionId} as the current state. Run a Plan to see what Terraform now believes differs from AWS.`
          );
          setVersions(null);
          setPendingRestore(null);
        }
        await loadStateStatus();
      } catch (e: unknown) {
        setStateError(e instanceof Error ? e.message : String(e));
      } finally {
        setStateBusy('');
      }
    },
    [lockId, loadStateStatus]
  );

  const runTunnel = useCallback(
    async (action: 'start' | 'stop') => {
      setDeployBusy(action === 'stop' ? 'tunnel-stop' : 'tunnel');
      setDeployError(null);
      setDeployMessage(null);
      try {
        const res = await fetch('/api/v1/cloud/deploy/tunnel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
          cache: 'no-store',
        });
        const data = await res.json();
        if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
        setDeployMessage(
          action === 'stop'
            ? 'Access tunnel closed. Nothing on the instance is reachable until it is reopened.'
            : 'Access tunnel open. The URLs below now resolve to the instance.'
        );
        await loadDeployStatus();
      } catch (e: unknown) {
        setDeployError(e instanceof Error ? e.message : String(e));
      } finally {
        setDeployBusy('');
      }
    },
    [loadDeployStatus]
  );

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading cloud substrate status...</p>
      </div>
    );
  }

  const anyBusy = busy !== '';
  const stateAnyBusy = stateBusy !== '';
  const remoteState = stateStatus?.remote_enabled === true;

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          AWS Cloud Infrastructure
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          The state of the AWS substrate — a VPC, a public subnet, one SSH-only security group,
          and a single EC2 instance — and of the nyxGPT release deployed onto it.
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
        The instance opens <strong>port 22 only</strong>, scoped to your public IP — never{' '}
        <code>0.0.0.0/0</code>. The app, web UI, and every observability endpoint bind{' '}
        <code>127.0.0.1</code> on the instance and are reached over an SSH tunnel.{' '}
        <strong>This page reports; it does not deploy.</strong> Deploying and tearing down are
        deliberate CLI operations — rare, consequential and irreversible enough that they should
        not be one stray click away — so they live in <code>nyxgpt cloud deploy</code> and{' '}
        <code>nyxgpt cloud destroy</code>, listed below. This follows the same status-only
        judgment as the local{' '}
        <a href="/admin/infrastructure" style={{ color: '#0066cc' }}>
          Infrastructure Status
        </a>{' '}
        page. To check that the <em>install</em> path still works on the target distro without
        deploying anything, run the{' '}
        <a href="/admin/cloud-smoke" style={{ color: '#0066cc' }}>
          Cloud Artifact Smoke
        </a>{' '}
        — a bare Amazon Linux 2023 container, no AWS account and no charges.
      </div>

      {error && (
        <div style={{ marginBottom: '1.5rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      {message && (
        <div
          style={{
            marginBottom: '1.5rem',
            padding: '0.75rem 1rem',
            borderRadius: '0.375rem',
            background: 'var(--background-secondary)',
            border: '1px solid #22c55e',
            fontSize: '0.875rem',
          }}
        >
          {message}
        </div>
      )}

      <div style={{ display: 'grid', gap: '1.5rem' }}>
        <div style={boxStyle}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '0.75rem',
            }}
          >
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Current substrate</h2>
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 999,
                background: status?.provisioned ? '#22c55e' : 'var(--background)',
                color: status?.provisioned ? 'white' : 'var(--foreground-muted)',
                border: status?.provisioned ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {status?.provisioned ? 'provisioned' : 'not provisioned'}
            </span>
          </div>

          <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
            <Row label="Region" value={status?.region ?? ''} />
            <Row label="Instance" value={status?.instance_id ?? ''} />
            <Row label="Instance type" value={status?.instance_type ?? ''} />
            <Row label="Public IP" value={status?.public_ip ?? ''} />
            <Row label="VPC" value={status?.vpc_id ?? ''} />
            <Row label="Security group" value={status?.security_group_id ?? ''} />
            <Row label="SSH key pair" value={status?.ssh_key_name ?? ''} />
            <Row label="SSH allowed from" value={status?.owner_ip_cidr ?? ''} />
            <Row
              label="Open ports"
              value={
                status && status.access_model.open_ports.length > 0
                  ? status.access_model.open_ports.join(', ')
                  : 'none'
              }
            />
          </ul>
        </div>

        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
            Preview a substrate change
          </h2>
          <p
            style={{
              fontSize: '0.8rem',
              color: 'var(--foreground-muted)',
              marginBottom: '1rem',
            }}
          >
            Plan reports what an apply <em>would</em> change and creates nothing. Anything left
            blank keeps the value saved by the previous run; the SSH source defaults to this
            machine&apos;s current public IP. To actually apply what the plan shows, run{' '}
            <code>{deployStatus?.commands?.deploy ?? 'nyxgpt cloud deploy'}</code>.
          </p>

          <div style={{ display: 'grid', gap: '0.75rem', gridTemplateColumns: '1fr 1fr' }}>
            <div>
              <label htmlFor="region" style={labelStyle}>
                AWS region
              </label>
              <input
                id="region"
                style={fieldStyle}
                placeholder="us-east-1"
                value={inputs.region}
                onChange={(e) => setInputs({ ...inputs, region: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="instance_type" style={labelStyle}>
                Instance type
              </label>
              <input
                id="instance_type"
                style={fieldStyle}
                placeholder="m5.large"
                value={inputs.instance_type}
                onChange={(e) => setInputs({ ...inputs, instance_type: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="ssh_key_name" style={labelStyle}>
                Existing EC2 key pair
              </label>
              <input
                id="ssh_key_name"
                style={fieldStyle}
                placeholder="my-existing-pair"
                value={inputs.ssh_key_name}
                onChange={(e) => setInputs({ ...inputs, ssh_key_name: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="ssh_public_key" style={labelStyle}>
                …or a public key file to register
              </label>
              <input
                id="ssh_public_key"
                style={fieldStyle}
                placeholder="~/.ssh/id_ed25519.pub"
                value={inputs.ssh_public_key}
                onChange={(e) => setInputs({ ...inputs, ssh_public_key: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="owner_ip" style={labelStyle}>
                SSH source IP/CIDR (optional)
              </label>
              <input
                id="owner_ip"
                style={fieldStyle}
                placeholder="auto-detect"
                value={inputs.owner_ip}
                onChange={(e) => setInputs({ ...inputs, owner_ip: e.target.value })}
              />
            </div>
          </div>

          <div style={{ display: 'flex', gap: '0.75rem', marginTop: '1.25rem' }}>
            <button
              onClick={() => void runPlan()}
              disabled={anyBusy}
              style={buttonStyle(anyBusy)}
              title="Show what would change. Creates nothing."
            >
              {busy === 'plan' ? 'Planning…' : 'Plan'}
            </button>
          </div>
        </div>

        <div style={boxStyle}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '0.75rem',
            }}
          >
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Terraform state</h2>
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 999,
                background: remoteState ? '#22c55e' : 'var(--background)',
                color: remoteState ? 'white' : 'var(--foreground-muted)',
                border: remoteState ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {remoteState ? 'S3 + DynamoDB lock' : 'local file'}
            </span>
          </div>

          {stateError && (
            <div style={{ marginBottom: '1rem' }}>
              <ErrorMessage message={stateError} onRetry={loadStateStatus} />
            </div>
          )}

          {stateMessage && (
            <div
              style={{
                marginBottom: '1rem',
                padding: '0.75rem 1rem',
                borderRadius: '0.375rem',
                background: 'var(--background)',
                border: '1px solid #22c55e',
                fontSize: '0.875rem',
              }}
            >
              {stateMessage}
            </div>
          )}

          <p
            style={{
              fontSize: '0.8rem',
              color: 'var(--foreground-muted)',
              marginBottom: '1rem',
            }}
          >
            {remoteState
              ? 'State is shared and locked: concurrent applies block instead of racing, and every write keeps its predecessor in the bucket for recovery.'
              : 'State is a single local file on this machine. A second operator or a CI runner applying the same substrate cannot see it, and two concurrent applies can corrupt it. Migrating creates a versioned, encrypted bucket and a DynamoDB lock table, then copies the existing state up.'}
          </p>

          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              margin: '0 0 1rem',
              fontSize: '0.875rem',
            }}
          >
            <Row label="Backend" value={stateStatus?.backend ?? ''} />
            <Row label="Locking" value={stateStatus?.locking ?? ''} />
            {remoteState ? (
              <>
                <Row label="Bucket" value={stateStatus?.bucket ?? ''} />
                <Row label="Object key" value={stateStatus?.key ?? ''} />
                <Row label="Lock table" value={stateStatus?.table ?? ''} />
                <Row label="Region" value={stateStatus?.region ?? ''} />
              </>
            ) : (
              <Row label="State file" value={stateStatus?.local_state_file ?? ''} />
            )}
          </ul>

          {!remoteState && (
            <button
              onClick={() => void runStateAction('migrate')}
              disabled={stateAnyBusy}
              style={buttonStyle(stateAnyBusy)}
              title="Create the state bucket and lock table, then move existing state into them"
            >
              {stateBusy === 'migrate' ? 'Migrating…' : 'Migrate to S3 + DynamoDB'}
            </button>
          )}

          {remoteState && (
            <div style={{ display: 'grid', gap: '1rem' }}>
              <div>
                <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                  Recover a previous state
                </h3>
                <p
                  style={{
                    fontSize: '0.8rem',
                    color: 'var(--foreground-muted)',
                    marginBottom: '0.5rem',
                  }}
                >
                  Each version is the complete state as it stood after one apply. Restoring
                  replaces what Terraform believes exists in AWS — the version it replaces stays
                  in the bucket, so the restore itself is reversible. Because a later apply
                  against the wrong version can destroy live resources, Restore only selects a
                  version; a second, explicit confirmation sends it.
                </p>
                <button
                  onClick={() => void runStateAction('versions')}
                  disabled={stateAnyBusy}
                  style={buttonStyle(stateAnyBusy)}
                >
                  {stateBusy === 'versions' ? 'Loading…' : 'List versions'}
                </button>

                {versions !== null && versions.length === 0 && (
                  <p style={{ fontSize: '0.8rem', marginTop: '0.75rem' }}>
                    No stored versions yet — the first apply against this backend creates one.
                  </p>
                )}

                {versions !== null && versions.length > 0 && (
                  <ul
                    style={{
                      listStyle: 'none',
                      padding: 0,
                      margin: '0.75rem 0 0',
                      fontSize: '0.8rem',
                      display: 'grid',
                      gap: '0.35rem',
                    }}
                  >
                    {versions.map((version) => (
                      <li
                        key={version.version_id}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: '0.75rem',
                        }}
                      >
                        <span>
                          <code>{version.version_id}</code>{' '}
                          <span style={{ color: 'var(--foreground-muted)' }}>
                            {version.last_modified} · {version.size} bytes
                            {version.latest ? ' · current' : ''}
                          </span>
                        </span>
                        {pendingRestore === version.version_id ? (
                          <span style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                            <button
                              onClick={() => void runStateAction('restore', version.version_id)}
                              disabled={stateAnyBusy}
                              style={buttonStyle(stateAnyBusy, true)}
                              title="Replace the current state with this version"
                            >
                              {stateBusy === 'restore' ? 'Restoring…' : 'Confirm restore'}
                            </button>
                            <button
                              onClick={() => setPendingRestore(null)}
                              disabled={stateAnyBusy}
                              style={buttonStyle(stateAnyBusy)}
                            >
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button
                            onClick={() => setPendingRestore(version.version_id)}
                            disabled={stateAnyBusy || version.latest}
                            style={buttonStyle(stateAnyBusy || version.latest)}
                            title={
                              version.latest
                                ? 'Already the current state'
                                : 'Select this version, then confirm to make it the current state'
                            }
                          >
                            Restore
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                  Release a stuck lock
                </h3>
                <p
                  style={{
                    fontSize: '0.8rem',
                    color: 'var(--foreground-muted)',
                    marginBottom: '0.5rem',
                  }}
                >
                  A run killed mid-apply never releases its lock, and every later run then fails
                  with its id. Paste that id here. Only do this when no apply is actually
                  running — breaking a live lock is how two runs end up writing the same state.
                </p>
                <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
                  <input
                    aria-label="Lock ID"
                    style={{ ...fieldStyle, maxWidth: 320 }}
                    placeholder="Lock ID from the Terraform error"
                    value={lockId}
                    onChange={(e) => setLockId(e.target.value)}
                  />
                  <button
                    onClick={() => void runStateAction('unlock')}
                    disabled={stateAnyBusy || !lockId.trim()}
                    style={buttonStyle(stateAnyBusy || !lockId.trim())}
                  >
                    {stateBusy === 'unlock' ? 'Unlocking…' : 'Force unlock'}
                  </button>
                </div>
              </div>

              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
                To back state up, or to move it back to a local file when the backend itself is
                unreachable, use <code>nyxgpt cloud state backup</code> and{' '}
                <code>nyxgpt cloud state local</code>.
              </p>
            </div>
          )}
        </div>

        <div style={boxStyle}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: '0.75rem',
            }}
          >
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Deployment</h2>
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 999,
                background: deployStatus?.deployed ? '#22c55e' : 'var(--background)',
                color: deployStatus?.deployed ? 'white' : 'var(--foreground-muted)',
                border: deployStatus?.deployed ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {deployStatus?.deployed ? 'deployed' : 'not deployed'}
            </span>
          </div>

          <p
            style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '1rem' }}
          >
            What <code>nyxgpt cloud deploy</code> last put on the instance: a{' '}
            <strong>published</strong> nyxGPT release (never a copy of a repository), the
            observability profiles it enabled, and whether the SSH tunnel that is the only
            access path is currently open. Re-running the deploy reconciles rather than
            duplicating.
          </p>

          {deployError && (
            <div style={{ marginBottom: '1rem' }}>
              <ErrorMessage message={deployError} onRetry={loadDeployStatus} />
            </div>
          )}

          {deployMessage && (
            <div
              style={{
                marginBottom: '1rem',
                padding: '0.75rem 1rem',
                borderRadius: '0.375rem',
                background: 'var(--background)',
                border: '1px solid #22c55e',
                fontSize: '0.875rem',
              }}
            >
              {deployMessage}
            </div>
          )}

          <ul
            style={{ listStyle: 'none', padding: 0, margin: '0 0 1rem 0', fontSize: '0.875rem' }}
          >
            <Row label="Installed version" value={deployStatus?.version ?? ''} />
            <Row label="Instance" value={deployStatus?.instance_id ?? ''} />
            <Row label="Region" value={deployStatus?.region ?? ''} />
            <Row
              label="Observability profiles"
              value={(deployStatus?.profiles ?? []).join(', ')}
            />
            <Row
              label="Access tunnel"
              value={
                deployStatus?.tunnel.running ? `open (pid ${deployStatus.tunnel.pid})` : 'closed'
              }
            />
            <Row label="Stack health" value={healthLabel(deployStatus?.health)} />
          </ul>

          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
            <button
              onClick={() => void refreshDeployStatus()}
              disabled={deployBusy !== ''}
              style={buttonStyle(deployBusy !== '')}
              title="Re-read the deployment state and re-probe health. Changes nothing."
            >
              {deployBusy === 'refresh' ? 'Refreshing…' : 'Refresh status'}
            </button>
            {deployStatus?.tunnel.running ? (
              <button
                onClick={() => void runTunnel('stop')}
                disabled={deployBusy !== ''}
                style={buttonStyle(deployBusy !== '')}
              >
                {deployBusy === 'tunnel-stop' ? 'Closing…' : 'Close tunnel'}
              </button>
            ) : (
              <button
                onClick={() => void runTunnel('start')}
                disabled={deployBusy !== '' || !deployStatus?.deployed}
                style={buttonStyle(deployBusy !== '' || !deployStatus?.deployed)}
              >
                {deployBusy === 'tunnel' ? 'Opening…' : 'Open tunnel'}
              </button>
            )}
          </div>

          {deployStatus && Object.keys(deployStatus.urls).length > 0 && (
            <div style={{ marginTop: '1rem' }}>
              <h3 style={{ fontSize: '0.95rem', fontWeight: 600, marginBottom: '0.35rem' }}>
                URLs
              </h3>
              <p
                style={{
                  fontSize: '0.8rem',
                  color: 'var(--foreground-muted)',
                  marginBottom: '0.5rem',
                }}
              >
                Every one is a <code>localhost</code> address forwarded over the tunnel — there
                is no instance-facing URL, by design. They resolve only while the tunnel is
                open.
              </p>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
                {Object.entries(deployStatus.urls).map(([name, url]) => (
                  <li key={name} style={{ padding: '2px 0' }}>
                    <span style={{ color: 'var(--foreground-muted)' }}>{name}</span>{' '}
                    <code>{url}</code>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* --- Deploy history (P6-15, #3514) ---
            Written by `nyxgpt.cloud_deploy` itself, so a deploy run from the
            terminal appears here too -- a dashboard that only showed its own
            actions would be silent about the surface the owner's decision
            makes primary. */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Deploy history
          </h2>
          <p
            style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}
          >
            Every deploy, teardown and end-to-end smoke run of this cloud deployment, newest
            first, whether it was run from the CLI or elsewhere. A deploy that installed the
            stack but never went healthy is recorded as failed, as is a smoke run whose
            teardown left resources behind.
          </p>
          {deployStatus && deployStatus.history.length > 0 ? (
            <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.85rem' }}>
              {deployStatus.history.map((entry, index) => (
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
              Nothing has been deployed or torn down from this machine yet.
            </p>
          )}
        </div>

        {/* --- Lifecycle actions: pointers, not buttons (P6-15, #3514) ---
            Rendered from the backend's LIFECYCLE_COMMANDS so the page cannot
            drift from what the CLI actually accepts. Every one is a wrapped
            `nyxgpt` command -- CLAUDE.md forbids handing the user a raw
            docker/terraform/kubectl one. */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Lifecycle actions
          </h2>
          <p
            style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', marginBottom: '1rem' }}
          >
            Deploying and tearing down are not dashboard buttons. They create and delete real,
            billed infrastructure — a teardown deletes the instance and its root volume, and
            anything stored only on that box is lost — so they are run deliberately from a
            terminal:
          </p>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.875rem' }}>
            {[
              ['Deploy or redeploy the stack', deployStatus?.commands?.deploy],
              ['Tear the whole deployment down', deployStatus?.commands?.destroy],
              [
                'Run the end-to-end cloud test (deploys, verifies chat/RAG/observability, then tears down)',
                deployStatus?.commands?.smoke,
              ],
              ['Show this state from a terminal', deployStatus?.commands?.status],
              ['Re-allow SSH after your public IP changes', deployStatus?.commands?.allow_ip],
            ]
              .filter(([, command]) => Boolean(command))
              .map(([label, command]) => (
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

'use client';

// SRE/admin surface for the AWS substrate (P6-8, #3509) -- the dashboard
// counterpart of `nyxgpt cloud infra`. Both drive the same backend functions,
// so the access model (SSH on port 22 only, scoped to the owner's IP, never
// 0.0.0.0/0) is identical whichever surface provisions the instance.
//
// Provisioning is a slow, real-money operation, so this page is deliberately
// explicit: plan is always available, apply states what it will create, and
// destroy requires typing the word DESTROY.

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
  const [busy, setBusy] = useState<'' | 'plan' | 'apply' | 'destroy'>('');
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState('');
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
  const [versions, setVersions] = useState<StateVersion[] | null>(null);
  const [lockId, setLockId] = useState('');

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

  // Kept separate from loadStatus so a state-backend read failure never blanks
  // the substrate panel (and vice versa) -- they are independent subsystems.
  const loadStateStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/cloud/state', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
      setStateStatus(data as CloudStateStatus);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    void loadStateStatus();
  }, [loadStatus, loadStateStatus]);

  const runAction = useCallback(
    async (action: 'plan' | 'apply' | 'destroy') => {
      setBusy(action);
      setError(null);
      setMessage(null);
      try {
        // Empty strings mean "unchanged" -- the backend falls back to the
        // settings the last run saved, exactly as the CLI flags do.
        const body: Record<string, unknown> = {};
        for (const [key, value] of Object.entries(inputs)) {
          if (value.trim()) body[key] = value.trim();
        }
        if (action === 'destroy') body.confirm = true;

        const res = await fetch(`/api/v1/cloud/infra/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          cache: 'no-store',
        });
        const data = await res.json();
        if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));

        if (action === 'plan') {
          setMessage('Plan complete. Nothing was created — review it, then Apply.');
        } else if (action === 'apply') {
          const outputs = (data.outputs ?? {}) as Record<string, string>;
          setMessage(
            `Substrate applied. Instance ${outputs.instance_id ?? 'unknown'} in ${
              outputs.region ?? 'the configured region'
            }. Reach it with an SSH tunnel; if your public IP changes, run \`nyxgpt cloud allow-ip\`.`
          );
        } else {
          setMessage('Substrate destroyed.');
          setConfirmText('');
        }
        await loadStatus();
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy('');
      }
    },
    [inputs, loadStatus]
  );

  const runStateAction = useCallback(
    async (action: 'migrate' | 'unlock' | 'versions' | 'restore', versionId?: string) => {
      setStateBusy(action);
      setError(null);
      setMessage(null);
      try {
        if (action === 'versions') {
          const res = await fetch('/api/v1/cloud/state/versions', { cache: 'no-store' });
          const data = await res.json();
          if (!res.ok) throw new Error(errorText(data, `HTTP ${res.status}`));
          setVersions((data.versions ?? []) as StateVersion[]);
          return;
        }

        const body: Record<string, unknown> = {};
        if (action === 'unlock') body.lock_id = lockId.trim();
        if (action === 'restore') {
          body.version_id = versionId;
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
          setMessage(
            `Remote state active: s3://${backend.bucket}/${backend.key}, locked by DynamoDB table ${backend.table}. Concurrent applies now block instead of racing.`
          );
        } else if (action === 'unlock') {
          setMessage(`Released lock ${lockId.trim()}.`);
          setLockId('');
        } else {
          setMessage(
            `Restored version ${versionId} as the current state. Run a Plan to see what Terraform now believes differs from AWS.`
          );
          setVersions(null);
        }
        await loadStateStatus();
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setStateBusy('');
      }
    },
    [lockId, loadStateStatus]
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
          Provision and tear down the AWS substrate: a VPC, a public subnet, one SSH-only
          security group, and a single EC2 instance.
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
        <code>127.0.0.1</code> on the instance and are reached over an SSH tunnel. This page
        provisions the substrate; deploying the nyxGPT stack onto it is a separate step. The
        same operations are available as <code>nyxgpt cloud infra plan|apply|destroy|status</code>.
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
            Provisioning inputs
          </h2>
          <p
            style={{
              fontSize: '0.8rem',
              color: 'var(--foreground-muted)',
              marginBottom: '1rem',
            }}
          >
            Anything left blank keeps the value saved by the previous run. The SSH source
            defaults to this machine&apos;s current public IP.
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
              onClick={() => void runAction('plan')}
              disabled={anyBusy}
              style={buttonStyle(anyBusy)}
              title="Show what would change. Creates nothing."
            >
              {busy === 'plan' ? 'Planning…' : 'Plan'}
            </button>
            <button
              onClick={() => void runAction('apply')}
              disabled={anyBusy}
              style={buttonStyle(anyBusy)}
              title="Provision or reconcile the substrate"
            >
              {busy === 'apply' ? 'Applying…' : 'Apply'}
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
                  in the bucket, so the restore itself is reversible.
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
                        <button
                          onClick={() => void runStateAction('restore', version.version_id)}
                          disabled={stateAnyBusy || version.latest}
                          style={buttonStyle(stateAnyBusy || version.latest)}
                          title={
                            version.latest
                              ? 'Already the current state'
                              : 'Make this version the current state'
                          }
                        >
                          Restore
                        </button>
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

        <div style={{ ...boxStyle, borderColor: '#ef4444' }}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Destroy the substrate
          </h2>
          <p style={{ fontSize: '0.875rem', marginBottom: '0.75rem' }}>
            Deletes the instance and its root volume. Anything stored only on that box —
            models, Cassandra data, logs — is lost. Type <code>DESTROY</code> to enable the
            button.
          </p>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
            <input
              aria-label="Type DESTROY to confirm"
              style={{ ...fieldStyle, maxWidth: 200 }}
              placeholder="DESTROY"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
            />
            <button
              onClick={() => void runAction('destroy')}
              disabled={anyBusy || confirmText !== 'DESTROY'}
              style={buttonStyle(anyBusy || confirmText !== 'DESTROY', true)}
            >
              {busy === 'destroy' ? 'Destroying…' : 'Destroy'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

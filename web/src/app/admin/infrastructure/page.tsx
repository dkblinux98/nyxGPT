'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

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
    context: string;
    provisioned: boolean;
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

const boxStyle: React.CSSProperties = {
  padding: '1.5rem',
  backgroundColor: 'var(--background-secondary)',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
};

const MODE_LABELS: Record<DeploymentModeName, string> = {
  native: 'Native (Homebrew services + Cassandra container)',
  compose: 'Docker Compose',
  terraform: 'Terraform',
  kubernetes: 'Kubernetes',
  none: 'Nothing detected running',
};

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

  const loadStatus = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/infra/status', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setStatus(data);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading infrastructure status...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Infrastructure Status
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          What&apos;s actually running, honestly reported for every local deployment mode.
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
        is. The AWS substrate is provisioned separately — see{' '}
        <a href="/admin/cloud-infrastructure">AWS Cloud Infrastructure</a> or{' '}
        <code>nyxgpt cloud infra</code>. This page only reports the status of local deployments;
        installing and destroying local infrastructure is a <code>nyxgpt ops</code> CLI
        operation, not a web one.
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
                onClick={loadStatus}
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
                Cannot determine from this deployment mode — the Compose file isn&apos;t reachable
                from wherever this API process is running (e.g. a Terraform-managed container
                missing the docker-compose.yml bind mount).
              </p>
            ) : (
              <ComponentList components={status.compose} />
            )}
          </div>

          {/* --- Terraform --- */}
          <div style={boxStyle}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Terraform</h2>
              <span style={badgeStyle(status.terraform.deployed, !status.terraform.probe_available)}>
                {!status.terraform.probe_available
                  ? 'CANNOT DETERMINE'
                  : status.terraform.deployed
                    ? 'DEPLOYED'
                    : 'NOT DEPLOYED'}
              </span>
            </div>

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
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

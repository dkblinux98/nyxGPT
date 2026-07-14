'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type Color = 'blue' | 'green';

type ColorHealth = {
  healthy: boolean;
  message: string;
};

type HistoryEntry = {
  from: Color;
  to: Color;
  ts: number;
};

type DeployStatus = {
  namespace: string;
  active: Color;
  inactive: Color;
  colors: Record<Color, ColorHealth>;
  history: HistoryEntry[];
};

const COLOR_DOT: Record<Color, string> = {
  blue: '#3b82f6',
  green: '#22c55e',
};

export default function DeployPage() {
  const router = useRouter();
  const [status, setStatus] = useState<DeployStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/deploy/status', { cache: 'no-store' });
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
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function handleSwitch() {
    if (!status) return;
    const target = status.inactive;
    if (!confirm(`Switch traffic from ${status.active} to ${target}?`)) return;

    setSwitching(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/deploy/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: target }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || data.message || `HTTP ${res.status}`);
      }
      setActionMessage(data.message || `Switched to ${target}`);
      await loadStatus();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setActionError(msg);
    } finally {
      setSwitching(false);
    }
  }

  async function handleRollback() {
    if (!confirm('Roll back to the previously active color?')) return;

    setRollingBack(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/deploy/rollback', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || data.message || `HTTP ${res.status}`);
      }
      setActionMessage(data.message || 'Rolled back');
      await loadStatus();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setActionError(msg);
    } finally {
      setRollingBack(false);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading deployment status...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div
        style={{
          marginBottom: '2rem',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div>
          <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Blue/Green Deployment
          </h1>
          <p style={{ color: 'var(--foreground-muted)' }}>
            Zero-downtime cutover between two local nyxgpt-api instances (kind/minikube/k3s).
          </p>
        </div>
        <button
          onClick={() => router.push('/')}
          style={{
            padding: '0.5rem 1rem',
            backgroundColor: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            borderRadius: '0.375rem',
            cursor: 'pointer',
            fontSize: '0.875rem',
          }}
        >
          Back to Chat
        </button>
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
        Looking for the Compose-equivalent core stack (Ollama, Cassandra, API, web UI) provisioned
        via <code>terraform apply</code> / <code>terraform destroy</code> instead of{' '}
        <code>docker compose up</code>/<code>down</code>? See <code>docs/terraform.md</code> —
        local-only infrastructure as code, no cloud provider modules or cloud networking. The
        blue/green colors managed on this page are a separate, Kubernetes-specific deployment
        model (see <code>docs/kubernetes.md</code>).
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      {actionError && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={actionError} />
        </div>
      )}

      {actionMessage && !actionError && (
        <div
          style={{
            marginBottom: '1rem',
            padding: '0.75rem 1rem',
            borderRadius: '0.375rem',
            background: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            fontSize: '0.875rem',
          }}
        >
          {actionMessage}
        </div>
      )}

      {status && (
        <>
          <div style={{ display: 'grid', gap: '1rem', gridTemplateColumns: '1fr 1fr', marginBottom: '1.5rem' }}>
            {(['blue', 'green'] as Color[]).map((color) => {
              const health = status.colors[color];
              const isActive = status.active === color;
              return (
                <div
                  key={color}
                  style={{
                    padding: '1.5rem',
                    backgroundColor: 'var(--background-secondary)',
                    borderRadius: '0.5rem',
                    border: isActive ? `2px solid ${COLOR_DOT[color]}` : '1px solid var(--border-color)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                    <span
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: '50%',
                        background: COLOR_DOT[color],
                        display: 'inline-block',
                      }}
                    />
                    <h3 style={{ fontSize: '1.1rem', fontWeight: 'bold', textTransform: 'capitalize' }}>
                      {color}
                    </h3>
                    {isActive && (
                      <span
                        style={{
                          marginLeft: 'auto',
                          fontSize: '0.75rem',
                          fontWeight: 600,
                          padding: '2px 8px',
                          borderRadius: 999,
                          background: COLOR_DOT[color],
                          color: 'white',
                        }}
                      >
                        ACTIVE
                      </span>
                    )}
                  </div>
                  <p style={{ fontSize: '0.875rem', color: health.healthy ? '#22c55e' : '#ef4444' }}>
                    {health.healthy ? 'Healthy' : 'Unhealthy'}
                  </p>
                  <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                    {health.message}
                  </p>
                </div>
              );
            })}
          </div>

          <div style={{ display: 'flex', gap: '0.75rem', marginBottom: '2rem' }}>
            <button
              onClick={handleSwitch}
              disabled={switching || !status.colors[status.inactive]?.healthy}
              title={
                !status.colors[status.inactive]?.healthy
                  ? `${status.inactive} is unhealthy; fix it before switching`
                  : undefined
              }
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: !status.colors[status.inactive]?.healthy ? 'var(--background-secondary)' : '#3b82f6',
                color: !status.colors[status.inactive]?.healthy ? 'var(--foreground-muted)' : 'white',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: switching || !status.colors[status.inactive]?.healthy ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {switching ? 'Switching...' : `Switch to ${status.inactive}`}
            </button>
            <button
              onClick={handleRollback}
              disabled={rollingBack || status.history.length === 0}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                color: status.history.length === 0 ? 'var(--foreground-muted)' : 'var(--foreground)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: rollingBack || status.history.length === 0 ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {rollingBack ? 'Rolling back...' : 'Rollback'}
            </button>
            <button
              onClick={loadStatus}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: 'pointer',
                fontSize: '0.875rem',
              }}
            >
              Refresh
            </button>
          </div>

          <div>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Recent switches
            </h2>
            {status.history.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No switches recorded yet.
              </p>
            ) : (
              <ul style={{ listStyle: 'none', padding: 0, display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {[...status.history].reverse().map((entry, idx) => (
                  <li
                    key={`${entry.ts}-${idx}`}
                    style={{
                      fontSize: '0.875rem',
                      padding: '0.5rem 0.75rem',
                      background: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                    }}
                  >
                    {entry.from} → {entry.to} at {new Date(entry.ts * 1000).toLocaleString()}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}

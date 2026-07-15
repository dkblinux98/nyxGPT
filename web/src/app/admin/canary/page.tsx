'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type TrackHealth = {
  healthy: boolean;
  message: string;
};

type Metrics = {
  total_requests: number;
  error_rate_percent: number;
  p95_latency_ms: number;
};

type HistoryEntry = {
  action: string;
  weight_percent?: number;
  from_weight_percent?: number;
  ts: number;
};

type CanaryStatus = {
  namespace: string;
  active: boolean;
  weight_percent: number;
  stable: TrackHealth;
  canary: TrackHealth;
  metrics: Metrics;
  history: HistoryEntry[];
  available: boolean;
  unavailable_reason: string | null;
};

export default function CanaryPage() {
  const router = useRouter();
  const [status, setStatus] = useState<CanaryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [startWeight, setStartWeight] = useState(10);
  const [starting, setStarting] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/canary/status', { cache: 'no-store' });
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

  async function runAction(
    path: string,
    body: Record<string, unknown> | undefined,
    setBusy: (v: boolean) => void,
    fallbackMessage: string
  ) {
    setBusy(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || data.message || `HTTP ${res.status}`);
      }
      setActionMessage(data.message || fallbackMessage);
      await loadStatus();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setActionError(msg);
    } finally {
      setBusy(false);
    }
  }

  const handleStart = () =>
    runAction(
      '/api/v1/canary/start',
      { weight_percent: startWeight },
      setStarting,
      `Started canary rollout at ${startWeight}%`
    );

  const handleEvaluate = () =>
    runAction('/api/v1/canary/evaluate', undefined, setEvaluating, 'Evaluated canary metrics');

  const handlePromote = () => {
    if (!confirm('Promote the canary to a higher traffic share?')) return;
    return runAction('/api/v1/canary/promote', {}, setPromoting, 'Promoted canary');
  };

  const handleRollback = () => {
    if (!confirm('Roll back the canary rollout to 0% traffic?')) return;
    return runAction('/api/v1/canary/rollback', undefined, setRollingBack, 'Rolled back canary');
  };

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading canary status...</p>
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
            Canary Deployment
          </h1>
          <p style={{ color: 'var(--foreground-muted)' }}>
            Gradual weighted rollout between nyxgpt-api-stable and nyxgpt-api-canary, with
            metrics-based promotion and automatic rollback.
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
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              marginBottom: '1.5rem',
            }}
          >
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 10px',
                borderRadius: 999,
                background: status.active ? '#f59e0b' : 'var(--background-secondary)',
                color: status.active ? 'white' : 'var(--foreground-muted)',
                border: status.active ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {status.active ? `ROLLOUT IN PROGRESS — ${status.weight_percent}%` : 'IDLE'}
            </span>
            <span style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
              namespace: {status.namespace}
            </span>
          </div>

          {!status.available && (
            <div
              style={{
                marginBottom: '1.5rem',
                padding: '1rem 1.5rem',
                borderRadius: '0.5rem',
                background: 'var(--background-secondary)',
                border: '1px solid #f59e0b',
                fontSize: '0.875rem',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '1rem',
              }}
            >
              <span>
                <strong>Not available in this deployment mode.</strong>{' '}
                {status.unavailable_reason}
              </span>
              <button
                onClick={loadStatus}
                style={{
                  padding: '0.4rem 0.75rem',
                  backgroundColor: 'var(--background)',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  cursor: 'pointer',
                  fontSize: '0.8rem',
                  flexShrink: 0,
                }}
              >
                Refresh
              </button>
            </div>
          )}

          {status.available && (
          <>
          <div
            style={{
              display: 'grid',
              gap: '1rem',
              gridTemplateColumns: '1fr 1fr',
              marginBottom: '1.5rem',
            }}
          >
            {(
              [
                { key: 'stable', label: 'Stable', health: status.stable },
                { key: 'canary', label: 'Canary', health: status.canary },
              ] as const
            ).map(({ key, label, health }) => (
              <div
                key={key}
                style={{
                  padding: '1.5rem',
                  backgroundColor: 'var(--background-secondary)',
                  borderRadius: '0.5rem',
                  border: '1px solid var(--border-color)',
                }}
              >
                <h3 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                  {label}
                </h3>
                <p style={{ fontSize: '0.875rem', color: health.healthy ? '#22c55e' : '#ef4444' }}>
                  {health.healthy ? 'Healthy' : 'Unhealthy'}
                </p>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                  {health.message}
                </p>
              </div>
            ))}
          </div>

          <div
            style={{
              padding: '1rem 1.5rem',
              marginBottom: '1.5rem',
              backgroundColor: 'var(--background-secondary)',
              borderRadius: '0.5rem',
              border: '1px solid var(--border-color)',
              display: 'flex',
              gap: '2rem',
              flexWrap: 'wrap',
            }}
          >
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>Requests</div>
              <div style={{ fontSize: '1.1rem', fontWeight: 600 }}>
                {status.metrics.total_requests}
              </div>
            </div>
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>Error rate</div>
              <div style={{ fontSize: '1.1rem', fontWeight: 600 }}>
                {status.metrics.error_rate_percent.toFixed(2)}%
              </div>
            </div>
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>p95 latency</div>
              <div style={{ fontSize: '1.1rem', fontWeight: 600 }}>
                {status.metrics.p95_latency_ms.toFixed(0)}ms
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
            {!status.active && (
              <>
                <label style={{ fontSize: '0.875rem', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  Start at
                  <input
                    type="number"
                    min={1}
                    max={99}
                    value={startWeight}
                    onChange={(e) => setStartWeight(Number(e.target.value))}
                    style={{
                      width: '4rem',
                      padding: '0.35rem 0.5rem',
                      borderRadius: '0.375rem',
                      border: '1px solid var(--border-color)',
                      background: 'var(--background)',
                      color: 'var(--foreground)',
                    }}
                  />
                  %
                </label>
                <button
                  onClick={handleStart}
                  disabled={starting}
                  style={{
                    padding: '0.5rem 1rem',
                    backgroundColor: '#3b82f6',
                    color: 'white',
                    border: 'none',
                    borderRadius: '0.375rem',
                    cursor: starting ? 'not-allowed' : 'pointer',
                    fontSize: '0.875rem',
                    fontWeight: '600',
                  }}
                >
                  {starting ? 'Starting...' : 'Start canary'}
                </button>
              </>
            )}
            {status.active && (
              <>
                <button
                  onClick={handleEvaluate}
                  disabled={evaluating}
                  style={{
                    padding: '0.5rem 1rem',
                    backgroundColor: 'var(--background-secondary)',
                    border: '1px solid var(--border-color)',
                    borderRadius: '0.375rem',
                    cursor: evaluating ? 'not-allowed' : 'pointer',
                    fontSize: '0.875rem',
                    fontWeight: '600',
                  }}
                >
                  {evaluating ? 'Evaluating...' : 'Evaluate metrics'}
                </button>
                <button
                  onClick={handlePromote}
                  disabled={promoting}
                  style={{
                    padding: '0.5rem 1rem',
                    backgroundColor: '#22c55e',
                    color: 'white',
                    border: 'none',
                    borderRadius: '0.375rem',
                    cursor: promoting ? 'not-allowed' : 'pointer',
                    fontSize: '0.875rem',
                    fontWeight: '600',
                  }}
                >
                  {promoting ? 'Promoting...' : 'Promote'}
                </button>
                <button
                  onClick={handleRollback}
                  disabled={rollingBack}
                  style={{
                    padding: '0.5rem 1rem',
                    backgroundColor: '#ef4444',
                    color: 'white',
                    border: 'none',
                    borderRadius: '0.375rem',
                    cursor: rollingBack ? 'not-allowed' : 'pointer',
                    fontSize: '0.875rem',
                    fontWeight: '600',
                  }}
                >
                  {rollingBack ? 'Rolling back...' : 'Rollback'}
                </button>
              </>
            )}
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
          </>
          )}

          <div>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Recent actions
            </h2>
            {status.history.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No canary actions recorded yet.
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
                    {entry.action}
                    {entry.weight_percent !== undefined ? ` → ${entry.weight_percent}%` : ''}
                    {entry.from_weight_percent !== undefined ? ` (from ${entry.from_weight_percent}%)` : ''}
                    {' at '}
                    {new Date(entry.ts * 1000).toLocaleString()}
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

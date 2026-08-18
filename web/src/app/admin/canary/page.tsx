'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';
import ObservabilityCredentialsHint from '../../../components/ObservabilityCredentialsHint';
import { exploreQueryUrl } from '../../../lib/grafanaExplore';
import { apiErrorText, errorMessage } from '../../../lib/apiError';

type TrackState = 'not_deployed' | 'unhealthy' | 'healthy' | 'error';

type TrackHealth = {
  state: TrackState;
  message: string;
  version: string;
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
  version?: string;
  ts: number;
};

type CanaryStatus = {
  namespace: string;
  component?: string;
  active: boolean;
  weight_percent: number;
  // The pool is elastic (#3833): `pool_replicas` is what the running rollout
  // grew it to, `resting_replicas` is what promote/rollback will hand back.
  // Both are 0 when no rollout is in progress.
  pool_replicas?: number;
  resting_replicas?: number;
  stable: TrackHealth;
  canary: TrackHealth;
  metrics: Metrics;
  history: HistoryEntry[];
  available: boolean;
  unavailable_reason: string | null;
  mode: string;
  mode_supported: boolean;
  mode_message: string | null;
};

const COMPONENTS = [
  { key: 'api', label: 'api' },
  { key: 'web', label: 'web' },
] as const;
type Component = (typeof COMPONENTS)[number]['key'];

const TRACK_STATE_LABEL: Record<TrackState, string> = {
  not_deployed: 'Not deployed',
  unhealthy: 'Unhealthy',
  healthy: 'Healthy',
  error: 'Error',
};

const TRACK_STATE_COLOR: Record<TrackState, string> = {
  not_deployed: 'var(--foreground-muted)',
  unhealthy: '#ef4444',
  healthy: '#22c55e',
  error: '#ef4444',
};

type MonitoringStatus = {
  active: boolean;
  grafana_ui_url: string;
};

type LogAggregationStatus = {
  active: boolean;
  grafana_explore_url: string;
};

const CANARY_LOKI_QUERY =
  '{job="nyxgpt"} |= `canary:` |~ `deploying|Deployed|starting|started|promoting|Promoted|rolling back|rolled back|regression`';

export default function CanaryPage() {
  const [component, setComponent] = useState<Component>('api');
  const [status, setStatus] = useState<CanaryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [startWeight, setStartWeight] = useState(10);
  const [deploying, setDeploying] = useState(false);
  const [starting, setStarting] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [logAggregation, setLogAggregation] = useState<LogAggregationStatus | null>(null);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/canary/status?component=${component}`, {
        cache: 'no-store',
      });
      const data = await res.json();
      if (!res.ok) {
        // The API's envelope is `{"error": {"message", ...}}` -- reading
        // `data.error` directly rendered the card as "[object Object]" (#3831).
        throw new Error(apiErrorText(data, `HTTP ${res.status}`));
      }
      setStatus(data);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [component]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    let cancelled = false;
    async function loadObservabilityLinks() {
      try {
        const [monitoringRes, logAggRes] = await Promise.all([
          fetch('/api/v1/monitoring', { cache: 'no-store' }),
          fetch('/api/v1/log-aggregation', { cache: 'no-store' }),
        ]);
        if (!cancelled && monitoringRes.ok) setMonitoring(await monitoringRes.json());
        if (!cancelled && logAggRes.ok) setLogAggregation(await logAggRes.json());
      } catch {
        // Observability links are a bonus, not critical -- swallow errors silently.
      }
    }
    void loadObservabilityLinks();
    return () => {
      cancelled = true;
    };
  }, []);

  async function runAction(
    path: string,
    body: Record<string, unknown>,
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
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(apiErrorText(data, `HTTP ${res.status}`));
      }
      setActionMessage(data.message || fallbackMessage);
      await loadStatus();
    } catch (e: unknown) {
      setActionError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const handleDeploy = () => {
    if (!confirm('Build the current checkout and deploy it to canary only (stable is untouched)?'))
      return;
    return runAction(
      '/api/v1/canary/deploy',
      { component },
      setDeploying,
      'Deployed current version to canary'
    );
  };

  // The fallback deliberately does NOT name a weight: replica counts are
  // integers, so the rollout may have to round the requested weight to the
  // closest split its pool can express (#3833). The server's own message says
  // what it actually landed on, and runAction prefers it.
  const handleStart = () =>
    runAction(
      '/api/v1/canary/start',
      { weight_percent: startWeight, component },
      setStarting,
      'Started canary rollout'
    );

  const handleEvaluate = () =>
    runAction(
      '/api/v1/canary/evaluate',
      { component },
      setEvaluating,
      'Evaluated canary metrics'
    );

  const handlePromote = () => {
    if (!confirm('Promote the canary to a higher traffic share?')) return;
    return runAction('/api/v1/canary/promote', { component }, setPromoting, 'Promoted canary');
  };

  const handleRollback = () => {
    if (!confirm('Roll back the canary rollout to 0% traffic?')) return;
    return runAction(
      '/api/v1/canary/rollback',
      { component },
      setRollingBack,
      'Rolled back canary'
    );
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
          <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
            Deploy a new version to nyxgpt-{component}-canary, gate a gradual weighted rollout on
            live metrics, then promote it to nyxgpt-{component}-stable (or roll back). The sole
            deployment model since blue/green was retired -- see docs/kubernetes.md.
          </p>
          <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to Admin Dashboard
          </a>
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          gap: '0.5rem',
          marginBottom: '1.5rem',
          borderBottom: '1px solid var(--border-color)',
        }}
      >
        {COMPONENTS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setComponent(key)}
            style={{
              padding: '0.5rem 1rem',
              border: 'none',
              borderBottom: component === key ? '2px solid #3b82f6' : '2px solid transparent',
              background: 'transparent',
              color: component === key ? 'var(--foreground)' : 'var(--foreground-muted)',
              fontWeight: component === key ? 600 : 400,
              cursor: 'pointer',
              fontSize: '0.9rem',
              textTransform: 'capitalize',
            }}
          >
            {label}
          </button>
        ))}
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

      {(monitoring?.active || logAggregation?.active) && (
        <div
          style={{
            marginBottom: '1.5rem',
            padding: '0.75rem 1rem',
            borderRadius: '0.375rem',
            background: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            fontSize: '0.8rem',
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: '0.4rem' }}>Observability</div>
          <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '0.4rem' }}>
            {monitoring?.active && (
              <a
                href={`${monitoring.grafana_ui_url}/d/nyxgpt-canary`}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: '#0066cc' }}
              >
                Canary Rollout dashboard (Grafana) ↗
              </a>
            )}
            {logAggregation?.active && (
              <a
                href={exploreQueryUrl(logAggregation.grafana_explore_url, CANARY_LOKI_QUERY)}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: '#0066cc' }}
              >
                Canary events (Grafana Explore / Loki) ↗
              </a>
            )}
          </div>
          {/* Both links land on Grafana's login form (#3718). */}
          <ObservabilityCredentialsHint services="Grafana" />
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
            {/* A rollout borrows replicas rather than living off a standing
                pool (#3833) -- show what it borrowed and what it gives back,
                so an inflated pool is never a mystery on the cluster. Both
                numbers come from the server together or not at all (a
                pre-#3833 server sends neither), so the badge is gated on
                both rather than inventing a resting count the server never
                reported. */}
            {status.active && !!status.pool_replicas && !!status.resting_replicas && (
              <span style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
                pool: {status.pool_replicas} replicas (stable rests at {status.resting_replicas})
              </span>
            )}
          </div>

          {!status.mode_supported && (
            <div
              style={{
                marginBottom: '1.5rem',
                padding: '1rem 1.5rem',
                borderRadius: '0.5rem',
                background: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                fontSize: '0.875rem',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '1rem',
              }}
            >
              <span>
                <strong>Canary doesn&apos;t apply to the current deployment mode ({status.mode}).</strong>{' '}
                {status.mode_message}
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

          {status.mode_supported && !status.available && (
            <div
              style={{
                marginBottom: '1.5rem',
                padding: '1rem 1.5rem',
                borderRadius: '0.5rem',
                background: 'var(--background-secondary)',
                border: '1px solid #ef4444',
                fontSize: '0.875rem',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '1rem',
              }}
            >
              <span>
                <strong>Kubernetes is unreachable.</strong> {status.unavailable_reason}
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

          {status.mode_supported && status.available && (
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
                <p style={{ fontSize: '0.875rem', color: TRACK_STATE_COLOR[health.state] }}>
                  {TRACK_STATE_LABEL[health.state]}
                </p>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                  {health.message}
                </p>
                {health.version && (
                  <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                    version: <code>{health.version}</code>
                  </p>
                )}
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

          {(() => {
            const pairNotReady = status.stable.state !== 'healthy' || status.canary.state === 'error';
            const notReadyHint =
              status.canary.state === 'error' ? status.canary.message : status.stable.message;
            return pairNotReady ? (
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
                Rollout controls are disabled until the stable/canary pair is up: {notReadyHint}
              </p>
            ) : null;
          })()}

          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
            <button
              onClick={handleDeploy}
              disabled={deploying || status.stable.state !== 'healthy' || status.canary.state === 'error'}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor:
                  deploying || status.stable.state !== 'healthy' || status.canary.state === 'error'
                    ? 'not-allowed'
                    : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
              title="Build the current checkout and deploy it to the canary Deployment only; traffic weighting is a separate step"
            >
              {deploying ? 'Deploying...' : 'Deploy current version to canary'}
            </button>
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
                  disabled={starting || status.stable.state !== 'healthy' || status.canary.state === 'error'}
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
                <span style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                  Starting grows the pool for the rollout and hands the replicas back on
                  promote or rollback; weights that the pool cannot express exactly are
                  rounded, and the result says to what.
                </span>
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
                  disabled={promoting || status.canary.state !== 'healthy'}
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
                  title={
                    status.canary.state !== 'healthy'
                      ? 'Refusing to shift more traffic to a canary that is not healthy'
                      : undefined
                  }
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
                    {entry.version ? ` (${entry.version})` : ''}
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

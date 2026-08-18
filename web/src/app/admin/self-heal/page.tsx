'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';
import ObservabilityCredentialsHint from '../../../components/ObservabilityCredentialsHint';
import { exploreQueryUrl } from '../../../lib/grafanaExplore';

type Component = {
  service: string;
  container: string;
  state: string;
  health: string;
  healthy: boolean;
  source: string;
  desired?: boolean;
  note?: string;
  // #3812: false when this component's real state could not be determined
  // from here at all (the Compose probe could not run). Never rendered as a
  // state -- "unknown" is its own third case alongside present and absent.
  known?: boolean;
  restart_count?: number;
  giving_up?: boolean;
};

type HealEvent = {
  ts: number;
  service: string;
  reason: string;
  action: string;
  ok: boolean;
  restart_count: number;
  message: string;
  evidence?: Record<string, unknown>;
};

type DetectedMode = 'native' | 'compose' | 'terraform' | 'kubernetes' | 'none';

type SelfHealStatus = {
  enabled: boolean;
  mode: DetectedMode;
  compose_probe_available: boolean;
  // Why the Compose survey could not run, when it could not (#3812), e.g.
  // "`docker compose ps` exited 125: permission denied while trying to
  // connect to the Docker daemon socket ...".
  compose_probe_reason?: string;
  components: Component[];
  unhealthy_count: number;
  unknown_count?: number;
  events: HealEvent[];
};

const MODE_LABELS: Record<DetectedMode, string> = {
  native: 'Native (Homebrew services + Cassandra container)',
  compose: 'Docker Compose',
  terraform: 'Terraform',
  kubernetes: 'Kubernetes',
  none: 'Nothing detected running',
};

type MonitoringStatus = {
  active: boolean;
  grafana_ui_url: string;
};

type LogAggregationStatus = {
  active: boolean;
  grafana_explore_url: string;
};

const SELF_HEAL_LOKI_QUERY = '{job="nyxgpt"} |= `self-heal:` |~ `restart|heal pass|giving up|recovered`';

export default function SelfHealPage() {
  const [status, setStatus] = useState<SelfHealStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [healingService, setHealingService] = useState<string | null>(null);
  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [logAggregation, setLogAggregation] = useState<LogAggregationStatus | null>(null);

  const loadStatus = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/self-heal/status', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setStatus(data);
      setLastUpdated(Date.now());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const interval = setInterval(loadStatus, 10000);
    return () => clearInterval(interval);
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

  async function handleToggle(current: SelfHealStatus) {
    setToggling(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/self-heal/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !current.enabled }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setActionMessage(data.enabled ? 'Self-heal enabled' : 'Self-heal disabled');
      await loadStatus();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setToggling(false);
    }
  }

  async function handleHealNow(service?: string) {
    if (service) {
      if (!confirm(`Restart "${service}" now?`)) return;
      setHealingService(service);
    } else {
      if (!confirm('Restart every unhealthy component now?')) return;
      setHealingService('*');
    }
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/self-heal/heal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(service ? { service } : {}),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      const healed = data.healed as HealEvent[];
      setActionMessage(
        healed.length === 0
          ? 'Nothing to heal (all checked components are healthy)'
          : `Restarted: ${healed.map((h) => h.service).join(', ')}`
      );
      await loadStatus();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setHealingService(null);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading self-heal status...</p>
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
            Self-Heal
          </h1>
          <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
            Watches the core app components (API, web UI, Ollama, Cassandra) -- whether they run
            natively (the default local-first setup), under Docker Compose, via Terraform, or as
            Kubernetes Pods -- plus any running observability containers, and automatically
            restarts anything unhealthy or stopped.
          </p>
          <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to Admin Dashboard
          </a>
        </div>
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
                href={`${monitoring.grafana_ui_url}/d/nyxgpt-self-healing`}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: '#0066cc' }}
              >
                Self-Healing dashboard (Grafana) ↗
              </a>
            )}
            {logAggregation?.active && (
              <a
                href={exploreQueryUrl(logAggregation.grafana_explore_url, SELF_HEAL_LOKI_QUERY)}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: '#0066cc' }}
              >
                Self-heal events (Grafana Explore / Loki) ↗
              </a>
            )}
          </div>
          {/* Both links land on Grafana's login form (#3718). */}
          <ObservabilityCredentialsHint services="Grafana" />
        </div>
      )}

      {status && (
        <>
          <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', marginBottom: '1rem' }}>
            Detected mode: <strong>{MODE_LABELS[status.mode]}</strong>
            {status.mode === 'kubernetes' &&
              " -- self-heal watches the Deployments' Pods and deletes a stuck/unhealthy one " +
                "(the Deployment's controller recreates it), on top of -- not instead of -- " +
                "kubelet's own liveness-probe restarts and the canary rollout's auto-rollback."}
          </p>

          {!status.compose_probe_available && (
            <p
              style={{
                fontSize: '0.875rem',
                color: 'var(--foreground-muted)',
                marginBottom: '1rem',
                padding: '0.5rem 0.75rem',
                borderRadius: '0.375rem',
                border: '1px solid var(--border-color)',
              }}
            >
              Observability tier: <strong>cannot determine from here</strong>. The Compose
              survey could not be run from wherever this API process lives, so the
              Grafana/Loki/Jaeger/GlitchTip rows below mean &quot;can&apos;t check&quot;, not
              &quot;not running&quot; — they are excluded from the unhealthy count and are not
              auto-healed.
              {status.compose_probe_reason && (
                <>
                  {' '}
                  Reason: <code>{status.compose_probe_reason}</code>. Check it yourself with{' '}
                  <code>nyxgpt ops status</code>; a permission-denied/daemon-unreachable error
                  usually means the service&apos;s session predates its <code>docker</code> group
                  membership, which <code>nyxgpt ops restart all</code> re-establishes.
                </>
              )}
            </p>
          )}

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              marginBottom: '1.5rem',
              flexWrap: 'wrap',
            }}
          >
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 10px',
                borderRadius: 999,
                background: status.enabled ? '#22c55e' : 'var(--background-secondary)',
                color: status.enabled ? 'white' : 'var(--foreground-muted)',
                border: status.enabled ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {status.enabled ? 'AUTO-HEAL ON' : 'AUTO-HEAL OFF'}
            </span>
            {status.unhealthy_count > 0 && (
              <span
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  padding: '2px 10px',
                  borderRadius: 999,
                  background: '#ef4444',
                  color: 'white',
                }}
              >
                {status.unhealthy_count} unhealthy
              </span>
            )}
            {/* Counted and coloured apart from "unhealthy" on purpose (#3812):
                a component nobody could query is not evidence that anything is
                broken, and showing it in red is how eleven healthy services
                got reported as an outage. */}
            {(status.unknown_count ?? 0) > 0 && (
              <span
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  padding: '2px 10px',
                  borderRadius: 999,
                  background: 'var(--background-secondary)',
                  color: 'var(--foreground-muted)',
                  border: '1px solid var(--border-color)',
                }}
                title="These components could not be queried from here, so their state is unknown -- not down. See the banner above for why."
              >
                {status.unknown_count} unknown
              </span>
            )}
            <button
              onClick={() => handleToggle(status)}
              disabled={toggling}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: status.enabled ? '#ef4444' : '#22c55e',
                color: 'white',
                border: 'none',
                borderRadius: '0.375rem',
                cursor: toggling ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {toggling
                ? 'Updating...'
                : status.enabled
                  ? 'Disable auto-heal'
                  : 'Enable auto-heal'}
            </button>
            <button
              onClick={() => handleHealNow()}
              disabled={healingService !== null}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: healingService !== null ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {healingService === '*' ? 'Healing...' : 'Heal all unhealthy now'}
            </button>
            <button
              onClick={loadStatus}
              disabled={refreshing}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: refreshing ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
              }}
            >
              {refreshing ? 'Refreshing...' : 'Refresh'}
            </button>
            {lastUpdated && (
              <span style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                Last updated {new Date(lastUpdated).toLocaleTimeString()}
              </span>
            )}
          </div>

          <div style={{ marginBottom: '1.5rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Components
            </h2>
            {status.components.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No components found. Run <code>nyxgpt ops install</code> to set up the core app
                components (or <code>nyxgpt ops observability</code> for monitoring/logging) and
                see live component health here.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {status.components.map((c) => (
                  <div
                    key={c.service}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0.75rem 1rem',
                      backgroundColor: 'var(--background-secondary)',
                      borderRadius: '0.5rem',
                      border: '1px solid var(--border-color)',
                    }}
                  >
                    <div>
                      <span style={{ fontWeight: 600 }}>{c.service}</span>
                      <span
                        style={{
                          marginLeft: '0.5rem',
                          fontSize: '0.7rem',
                          padding: '1px 6px',
                          borderRadius: 999,
                          background: 'var(--background)',
                          border: '1px solid var(--border-color)',
                          color: 'var(--foreground-muted)',
                        }}
                      >
                        {c.source ?? 'compose'}
                      </span>
                      <span
                        style={{
                          marginLeft: '0.75rem',
                          fontSize: '0.8rem',
                          color:
                            c.known === false
                              ? 'var(--foreground-muted)'
                              : c.desired === false
                                ? 'var(--foreground-muted)'
                                : c.state === 'absent'
                                  ? '#f59e0b'
                                  : c.healthy
                                    ? '#22c55e'
                                    : '#ef4444',
                        }}
                      >
                        {c.known === false
                          ? 'Unknown'
                          : c.desired === false
                            ? 'Disabled'
                            : c.state === 'absent'
                              ? 'Absent'
                              : c.healthy
                                ? 'Healthy'
                                : 'Unhealthy'}
                      </span>
                      <span
                        style={{
                          marginLeft: '0.5rem',
                          fontSize: '0.75rem',
                          color: 'var(--foreground-muted)',
                        }}
                      >
                        {c.known === false
                          ? 'state could not be determined from here — not counted unhealthy'
                          : c.desired === false
                            ? 'profile disabled in config, not auto-healed'
                            : c.state === 'absent'
                              ? 'enabled in config, no container running'
                              : `state=${c.state}${c.health ? ` health=${c.health}` : ''}`}
                      </span>
                      {c.giving_up && (
                        <span
                          style={{
                            marginLeft: '0.5rem',
                            fontSize: '0.7rem',
                            padding: '1px 6px',
                            borderRadius: 999,
                            background: '#ef4444',
                            color: 'white',
                          }}
                          title="Auto-heal exhausted its consecutive-restart budget for this component and stopped retrying -- use the heal-now button to try again, or investigate why restarts aren't fixing it."
                        >
                          gave up after {c.restart_count} restart{c.restart_count === 1 ? '' : 's'}
                        </span>
                      )}
                      {c.note && (
                        <div
                          style={{
                            marginTop: '0.25rem',
                            fontSize: '0.7rem',
                            color: 'var(--foreground-muted)',
                            fontStyle: 'italic',
                          }}
                        >
                          {c.note}
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => handleHealNow(c.service)}
                      disabled={healingService !== null}
                      style={{
                        padding: '0.35rem 0.75rem',
                        backgroundColor: 'var(--background)',
                        border: '1px solid var(--border-color)',
                        borderRadius: '0.375rem',
                        cursor: healingService !== null ? 'not-allowed' : 'pointer',
                        fontSize: '0.8rem',
                      }}
                    >
                      {healingService === c.service ? 'Healing...' : 'Heal now'}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Recent heal events
            </h2>
            {status.events.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No heal events recorded yet.
              </p>
            ) : (
              <ul
                style={{
                  listStyle: 'none',
                  padding: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '0.5rem',
                }}
              >
                {[...status.events].reverse().map((event, idx) => (
                  <li
                    key={`${event.ts}-${idx}`}
                    style={{
                      fontSize: '0.875rem',
                      padding: '0.5rem 0.75rem',
                      background: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                    }}
                  >
                    <span style={{ color: event.ok ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                      {event.ok ? 'OK' : 'FAILED'}
                    </span>{' '}
                    {event.service}: {event.message} ({event.reason}, restart #
                    {event.restart_count}) at {new Date(event.ts * 1000).toLocaleString()}
                    {event.evidence && Object.keys(event.evidence).length > 0 && (
                      <details style={{ marginTop: '0.375rem' }}>
                        <summary
                          style={{ cursor: 'pointer', color: 'var(--foreground-muted)', fontSize: '0.8125rem' }}
                        >
                          Probe evidence
                        </summary>
                        <pre
                          style={{
                            margin: '0.375rem 0 0',
                            padding: '0.5rem',
                            fontSize: '0.75rem',
                            background: 'var(--background)',
                            border: '1px solid var(--border-color)',
                            borderRadius: '0.25rem',
                            overflowX: 'auto',
                            whiteSpace: 'pre-wrap',
                          }}
                        >
                          {JSON.stringify(event.evidence, null, 2)}
                        </pre>
                      </details>
                    )}
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

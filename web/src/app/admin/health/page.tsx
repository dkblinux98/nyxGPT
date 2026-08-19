'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';
import RequiredModelsPanel from '../../../components/RequiredModelsPanel';
import UsageAnalyticsSection from './UsageAnalyticsSection';
import ResourceMetrics from './ResourceMetrics';

type ServiceStatus = {
  status: string;
  uptime_s: number;
};

type DependencyCheck = {
  name: string;
  ok: boolean;
  detail: string;
  applicable: boolean;
};

type SelfHealComponent = {
  service: string;
  state: string;
  health: string;
  healthy: boolean;
  desired?: boolean;
  // `known: false` means the probe could not determine this component's
  // state -- "unknown" is its own third case alongside present and absent
  // (#3812). `healthy: false` on such a row means "not established as
  // healthy", never "down".
  known?: boolean;
  note?: string;
};

type SelfHealStatus = {
  enabled: boolean;
  components: SelfHealComponent[];
  unhealthy_count: number;
  unknown_count?: number;
  compose_probe_reason?: string;
};

type ResourceMetricsSummary = {
  memory: { rss_mb: number; percent: number };
  cpu: { process_percent: number };
  disk: { percent: number };
  queue: { depth: number };
  errors: { rate_percent: number };
} | null;

type Alert = {
  severity: 'warning' | 'critical';
  message: string;
  source: 'grafana' | 'local';
};

type HealthData = {
  service: ServiceStatus;
  dependencies: DependencyCheck[];
  resource_metrics: ResourceMetricsSummary;
  alerts: Alert[];
  alerts_source: 'grafana' | 'local';
};

const cardStyle: React.CSSProperties = {
  padding: '1.25rem',
  border: '1px solid var(--border)',
  borderRadius: 8,
  background: 'var(--background)',
};

const sectionTitleStyle: React.CSSProperties = {
  margin: 0,
  marginBottom: '1rem',
  fontSize: '1.1rem',
};

// `unknown` is deliberately neither green nor red (#3812): "nobody could ask"
// is not a verdict in either direction, and rendering it as one is exactly how
// eleven running, healthy containers were reported as an outage.
function StatusBadge({
  ok,
  label,
  tone,
  title,
}: {
  ok: boolean;
  label: string;
  tone?: 'ok' | 'error' | 'unknown';
  title?: string;
}) {
  const resolved = tone ?? (ok ? 'ok' : 'error');
  const background =
    resolved === 'unknown'
      ? 'var(--background-secondary)'
      : resolved === 'ok'
        ? 'var(--success-bg)'
        : 'var(--error-bg)';
  const color =
    resolved === 'unknown'
      ? 'var(--muted-foreground)'
      : resolved === 'ok'
        ? 'var(--success-text)'
        : 'var(--error-text)';
  const dot = resolved === 'unknown' ? '#9ca3af' : resolved === 'ok' ? '#22c55e' : '#dc3545';
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        background,
        color,
        border: resolved === 'unknown' ? '1px solid var(--border)' : 'none',
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: dot,
        }}
      />
      {label}
    </span>
  );
}

function formatUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0 || days > 0) parts.push(`${hours}h`);
  if (minutes > 0 || hours > 0 || days > 0) parts.push(`${minutes}m`);
  parts.push(`${secs}s`);
  return parts.join(' ');
}

export default function AdminHealthPage() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selfHeal, setSelfHeal] = useState<SelfHealStatus | null>(null);

  const loadHealth = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/admin/health', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load system health: HTTP ${res.status}`);
      const data = await res.json();
      setHealth(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSelfHeal = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/self-heal/status', { cache: 'no-store' });
      if (!res.ok) return;
      setSelfHeal(await res.json());
    } catch {
      // Self-heal component detail is a supplement to this page, not its
      // core data -- a failed fetch here shouldn't block the rest of the
      // system health view from rendering.
    }
  }, []);

  useEffect(() => {
    loadHealth();
    const interval = setInterval(loadHealth, 15000);
    return () => clearInterval(interval);
  }, [loadHealth]);

  useEffect(() => {
    loadSelfHeal();
    const interval = setInterval(loadSelfHeal, 15000);
    return () => clearInterval(interval);
  }, [loadSelfHeal]);

  return (
    <main style={{ padding: '2rem', maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>System Health</h1>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      {/* Section anchors -- the consolidated screen (#3413) folds Service
          Health, Usage Analytics, and Resource Metrics onto one page, so
          these jump links stand in for the three destinations that used to
          be separate pages/tabs. */}
      <nav
        aria-label="System Health sections"
        style={{ display: 'flex', gap: 16, marginBottom: '1.5rem', fontSize: 13 }}
      >
        <a href="#service-health" style={{ color: '#0066cc' }}>
          Service Health
        </a>
        <a href="#usage-analytics" style={{ color: '#0066cc' }}>
          Usage Analytics
        </a>
        <a href="#resource-metrics" style={{ color: '#0066cc' }}>
          Resource Metrics
        </a>
      </nav>

      <section id="service-health" aria-label="Service health" style={{ marginBottom: '2rem' }}>
        {loading && !health ? (
          <LoadingSpinner label="Loading system health..." />
        ) : error ? (
          <ErrorMessage title="Failed to load system health" message={error} onRetry={loadHealth} retrying={loading} />
        ) : (
          // `health` is guaranteed non-null here: loadHealth's try/catch always
          // sets either `health` or `error` before `finally` clears `loading`,
          // so `!loading && !error && !health` can never occur. The assertion
          // below documents that invariant instead of adding an unreachable
          // `: null` fallback branch.
          (() => {
            const h = health as HealthData;
            return (
              <div style={{ display: 'grid', gap: '1.5rem', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))' }}>
                {/* Service Health Status */}
                <section style={cardStyle} aria-label="Service health status">
                <h2 style={sectionTitleStyle}>Service Status</h2>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <StatusBadge
                    ok={h.service.status === 'ok'}
                    label={`Service: ${h.service.status}`}
                  />
                  <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                    Uptime: <strong>{formatUptime(h.service.uptime_s)}</strong>
                  </div>
                </div>
              </section>

              {/* Self-Heal Components -- surfaces the same live component
                  set the Self-Heal page shows (fetched from the same
                  /api/v1/self-heal/status endpoint), so this page can never
                  read "all clear" while Self-Heal shows a named component
                  unhealthy (#3575). Dependency Checks below covers a
                  different, narrower set (Ollama/Cassandra reachability) --
                  this card is the full self-heal-monitored component set. */}
              <section style={cardStyle} aria-label="Self-heal components">
                <h2 style={sectionTitleStyle}>Self-Heal Components</h2>
                {!selfHeal ? (
                  <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>
                    Self-heal status unavailable.
                  </p>
                ) : (
                  // Three states, same as the Self-Heal panel (#3812): a
                  // component the probe could not query is neither healthy
                  // nor unhealthy, so it must not feed either verdict. Before
                  // this, an unqueryable probe left `unhealthy_count` at 0 and
                  // this card read a green "All components healthy" over rows
                  // it was simultaneously listing as failures.
                  (() => {
                    const shown = selfHeal.components.filter((c) => c.desired !== false);
                    const unknown = shown.filter((c) => c.known === false);
                    const unhealthy = shown.filter((c) => c.known !== false && !c.healthy);
                    const unknownCount = selfHeal.unknown_count ?? unknown.length;
                    return (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {selfHeal.unhealthy_count > 0 ? (
                          <StatusBadge ok={false} label={`${selfHeal.unhealthy_count} unhealthy`} />
                        ) : unknownCount > 0 ? (
                          <StatusBadge
                            ok={false}
                            tone="unknown"
                            label={`${unknownCount} unknown -- cannot determine from here`}
                            title="These components could not be queried from here, so their state is unknown -- not down, and not established as healthy."
                          />
                        ) : (
                          <StatusBadge ok label="All components healthy" />
                        )}
                        {selfHeal.compose_probe_reason && unknownCount > 0 && (
                          <p style={{ margin: 0, fontSize: 12, color: 'var(--muted-foreground)' }}>
                            Reason: <code>{selfHeal.compose_probe_reason}</code>
                          </p>
                        )}
                        {unhealthy.map((c) => (
                          <div key={c.service} style={{ fontSize: 13 }}>
                            <strong>{c.service}</strong>
                            <span style={{ color: 'var(--muted-foreground)' }}>
                              {' '}
                              -- state={c.state}
                              {c.health ? ` health=${c.health}` : ''}
                            </span>
                          </div>
                        ))}
                        {unknown.map((c) => (
                          <div key={c.service} style={{ fontSize: 13, color: 'var(--muted-foreground)' }}>
                            <strong>{c.service}</strong> -- unknown (state could not be determined
                            from here)
                          </div>
                        ))}
                        <a href="/admin/self-heal" style={{ color: '#0066cc', fontSize: 13 }}>
                          Self-Heal details →
                        </a>
                      </div>
                    );
                  })()
                )}
              </section>

              {/* Required Models -- Dependency Checks below reports whether
                  Ollama answers; this reports whether it holds the models the
                  first chat (and the first RAG-enabled) message needs, which a
                  reachable-but-empty Ollama does not (#3824). */}
              <RequiredModelsPanel />

              {/* Dependency Checks */}
              <section style={cardStyle} aria-label="Dependency checks">
                <h2 style={sectionTitleStyle}>Dependency Checks</h2>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {h.dependencies.map((dep) => (
                    <div key={dep.name} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <StatusBadge
                        ok={dep.ok}
                        label={`${dep.name}: ${dep.applicable ? (dep.ok ? 'healthy' : 'unreachable') : 'not applicable'}`}
                      />
                      <span style={{ fontSize: 12, color: 'var(--muted-foreground)' }}>{dep.detail}</span>
                    </div>
                  ))}
                </div>
              </section>

              {/* Resource Utilization */}
              <section style={cardStyle} aria-label="Resource utilization">
                <h2 style={sectionTitleStyle}>Resource Utilization</h2>
                {h.resource_metrics ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 14 }}>
                    <div>
                      Memory: <strong>{h.resource_metrics.memory.rss_mb.toFixed(0)} MB</strong> (
                      {h.resource_metrics.memory.percent.toFixed(1)}%)
                    </div>
                    <div>
                      CPU (process): <strong>{h.resource_metrics.cpu.process_percent.toFixed(1)}%</strong>
                    </div>
                    <div>
                      Disk: <strong>{h.resource_metrics.disk.percent.toFixed(1)}%</strong>
                    </div>
                    <div>
                      Queue depth: <strong>{h.resource_metrics.queue.depth}</strong>
                    </div>
                    <div>
                      Error rate: <strong>{h.resource_metrics.errors.rate_percent.toFixed(1)}%</strong>
                    </div>
                  </div>
                ) : (
                  <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>Resource metrics unavailable.</p>
                )}
                <div style={{ marginTop: '1rem', fontSize: 13 }}>
                  <a href="#resource-metrics" style={{ color: '#0066cc' }}>Full metrics →</a>
                </div>
              </section>

              {/* Alert Indicators */}
              <section style={cardStyle} aria-label="Alert indicators">
                <h2 style={sectionTitleStyle}>Alerts</h2>
                <p style={{ margin: '-0.5rem 0 1rem', fontSize: 12, color: 'var(--muted-foreground)' }}>
                  {h.alerts_source === 'grafana'
                    ? 'Live from Grafana alerting -- the source of truth.'
                    : 'Local estimate (Grafana monitoring is disabled or unreachable).'}
                </p>
                {h.alerts.length === 0 ? (
                  <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>No active alerts.</p>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {h.alerts.map((alert, idx) => (
                      <div
                        key={idx}
                        role="alert"
                        style={{
                          padding: '10px 12px',
                          borderRadius: 6,
                          fontSize: 13,
                          background: alert.severity === 'critical' ? 'var(--error-bg)' : 'var(--info-bg)',
                          color: alert.severity === 'critical' ? 'var(--error-text)' : 'inherit',
                          border: `1px solid ${alert.severity === 'critical' ? '#ffcccc' : 'var(--border)'}`,
                        }}
                      >
                        <strong style={{ textTransform: 'uppercase', fontSize: 11 }}>{alert.severity}</strong>
                        {' — '}
                        {alert.message}
                      </div>
                    ))}
                  </div>
                )}
              </section>
              </div>
            );
          })()
        )}
      </section>

      {/* Usage Analytics -- relocated from the retired /admin/analytics
          route (#3413), which was the last remaining entry point after
          #3396 removed it from the chat menu. */}
      <section id="usage-analytics" aria-label="Usage analytics" style={{ marginBottom: '2rem' }}>
        <h2 style={sectionTitleStyle}>Usage Analytics</h2>
        <UsageAnalyticsSection />
      </section>

      {/* Resource Metrics -- the history-backed component from #3352,
          relocated from Settings' Resource Usage tab (#3413), whose only
          home is now this screen. */}
      <section id="resource-metrics" aria-label="Resource metrics">
        <h2 style={sectionTitleStyle}>Resource Metrics</h2>
        <ResourceMetrics />
      </section>
    </main>
  );
}

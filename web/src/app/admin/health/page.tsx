'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

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

type ResourceMetrics = {
  memory: { rss_mb: number; percent: number };
  cpu: { process_percent: number };
  queue: { depth: number };
  errors: { rate_percent: number };
} | null;

type Alert = {
  severity: 'warning' | 'critical';
  message: string;
};

type HealthData = {
  service: ServiceStatus;
  dependencies: DependencyCheck[];
  resource_metrics: ResourceMetrics;
  alerts: Alert[];
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

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        background: ok ? 'var(--success-bg)' : 'var(--error-bg)',
        color: ok ? 'var(--success-text)' : 'var(--error-text)',
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: ok ? '#22c55e' : '#dc3545',
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

  useEffect(() => {
    loadHealth();
    const interval = setInterval(loadHealth, 15000);
    return () => clearInterval(interval);
  }, [loadHealth]);

  return (
    <main style={{ padding: '2rem', maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>System Health</h1>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

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
                      CPU: <strong>{h.resource_metrics.cpu.process_percent.toFixed(1)}%</strong>
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
                  <a href="/settings" style={{ color: '#0066cc' }}>Full metrics →</a>
                </div>
              </section>

              {/* Alert Indicators */}
              <section style={cardStyle} aria-label="Alert indicators">
                <h2 style={sectionTitleStyle}>Alerts</h2>
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
    </main>
  );
}

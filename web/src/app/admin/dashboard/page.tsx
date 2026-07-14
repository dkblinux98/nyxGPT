'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type OverviewData = {
  info: {
    ollama_base_url: string;
    default_model: string;
    rag_enabled: boolean;
  };
  resource_metrics: {
    memory: { rss_mb: number; percent: number };
    cpu: { process_percent: number };
    queue: { depth: number };
  } | null;
  deploy: { active?: string; inactive?: string; error?: string } & Record<string, unknown>;
  canary: { active?: boolean; error?: string } & Record<string, unknown>;
  observability: {
    monitoring: boolean;
    tracing: boolean;
    error_tracking: boolean;
    log_aggregation: boolean;
  };
  auth_enabled: boolean;
};

type ActivityEvent = {
  ts: number;
  action: string;
  detail: string;
};

type AccessData = {
  enabled: boolean;
  header: string;
  api_key_set: boolean;
  api_key_masked: string | null;
  api_key?: string;
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
        background: ok ? 'var(--success-bg)' : 'var(--muted)',
        color: ok ? 'var(--success-text)' : 'var(--muted-foreground)',
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: ok ? '#22c55e' : '#999',
        }}
      />
      {label}
    </span>
  );
}

export default function AdminDashboardPage() {
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState<string | null>(null);

  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState<string | null>(null);

  const [access, setAccess] = useState<AccessData | null>(null);
  const [accessLoading, setAccessLoading] = useState(true);
  const [accessError, setAccessError] = useState<string | null>(null);
  const [accessSaving, setAccessSaving] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [headerInput, setHeaderInput] = useState('X-API-Key');

  const loadOverview = useCallback(async () => {
    setOverviewLoading(true);
    setOverviewError(null);
    try {
      const res = await fetch('/api/v1/admin/overview', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load system status: HTTP ${res.status}`);
      const data = await res.json();
      setOverview(data);
    } catch (e: unknown) {
      setOverviewError(e instanceof Error ? e.message : String(e));
    } finally {
      setOverviewLoading(false);
    }
  }, []);

  const loadActivity = useCallback(async () => {
    setActivityLoading(true);
    setActivityError(null);
    try {
      const res = await fetch('/api/v1/admin/activity?limit=25', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load activity log: HTTP ${res.status}`);
      const data = await res.json();
      setActivity(data.events || []);
    } catch (e: unknown) {
      setActivityError(e instanceof Error ? e.message : String(e));
    } finally {
      setActivityLoading(false);
    }
  }, []);

  const loadAccess = useCallback(async () => {
    setAccessLoading(true);
    setAccessError(null);
    try {
      const res = await fetch('/api/v1/admin/access', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load access settings: HTTP ${res.status}`);
      const data = await res.json();
      setAccess(data);
      setHeaderInput(data.header || 'X-API-Key');
    } catch (e: unknown) {
      setAccessError(e instanceof Error ? e.message : String(e));
    } finally {
      setAccessLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOverview();
    loadActivity();
    loadAccess();
  }, [loadOverview, loadActivity, loadAccess]);

  async function updateAccess(payload: Record<string, unknown>) {
    setAccessSaving(true);
    setAccessError(null);
    try {
      const res = await fetch('/api/v1/admin/access', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setAccess(data);
      setHeaderInput(data.header || 'X-API-Key');
      if (data.api_key) {
        setRevealedKey(data.api_key);
      }
      loadActivity();
    } catch (e: unknown) {
      setAccessError(e instanceof Error ? e.message : String(e));
    } finally {
      setAccessSaving(false);
    }
  }

  function handleToggleAuth() {
    if (!access) return;
    updateAccess({ enabled: !access.enabled });
  }

  function handleSaveHeader() {
    if (!headerInput.trim()) return;
    updateAccess({ header: headerInput.trim() });
  }

  function handleRotateKey() {
    if (!confirm('Rotate the API key? Any client using the current key will stop working.')) {
      return;
    }
    updateAccess({ rotate: true });
  }

  function formatTimestamp(ts: number): string {
    return new Date(ts * 1000).toLocaleString();
  }

  return (
    <main style={{ padding: '2rem', maxWidth: 1200, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>Admin Dashboard</h1>
        <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Chat
        </a>
      </div>

      <div style={{ display: 'grid', gap: '1.5rem', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))' }}>
        {/* System Status Overview */}
        <section style={cardStyle} aria-label="System status overview">
          <h2 style={sectionTitleStyle}>System Status</h2>
          {overviewLoading ? (
            <LoadingSpinner label="Loading system status..." />
          ) : overviewError ? (
            <ErrorMessage title="Failed to load system status" message={overviewError} onRetry={loadOverview} retrying={overviewLoading} />
          ) : overview ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <StatusBadge ok={!overview.deploy.error} label={`Deploy: ${overview.deploy.active ?? 'unknown'}`} />
                <StatusBadge ok={!!overview.canary.active} label={overview.canary.active ? 'Canary: active' : 'Canary: idle'} />
                <StatusBadge ok={overview.observability.monitoring} label="Monitoring" />
                <StatusBadge ok={overview.observability.tracing} label="Tracing" />
                <StatusBadge ok={overview.observability.error_tracking} label="Error tracking" />
                <StatusBadge ok={overview.observability.log_aggregation} label="Log aggregation" />
                <StatusBadge ok={overview.auth_enabled} label={overview.auth_enabled ? 'Auth: enabled' : 'Auth: disabled'} />
              </div>
              {overview.resource_metrics && (
                <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                  Memory: <strong>{overview.resource_metrics.memory.rss_mb.toFixed(0)} MB</strong> ({overview.resource_metrics.memory.percent.toFixed(1)}%) · CPU:{' '}
                  <strong>{overview.resource_metrics.cpu.process_percent.toFixed(1)}%</strong> · Queue depth:{' '}
                  <strong>{overview.resource_metrics.queue.depth}</strong>
                </div>
              )}
              <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                Default model: <strong>{overview.info.default_model || 'Not set'}</strong> · RAG:{' '}
                <strong>{overview.info.rag_enabled ? 'enabled' : 'disabled'}</strong>
              </div>
              <div style={{ display: 'flex', gap: 12, fontSize: 13, marginTop: 4 }}>
                <a href="/admin/health" style={{ color: '#0066cc' }}>System Health →</a>
                <a href="/admin/deploy" style={{ color: '#0066cc' }}>Deployment →</a>
                <a href="/admin/canary" style={{ color: '#0066cc' }}>Canary →</a>
                <a href="/admin/analytics" style={{ color: '#0066cc' }}>Usage Analytics →</a>
                <a href="/settings" style={{ color: '#0066cc' }}>Full metrics →</a>
              </div>
            </div>
          ) : null}
        </section>

        {/* Configuration Management */}
        <section style={cardStyle} aria-label="Configuration management">
          <h2 style={sectionTitleStyle}>Configuration</h2>
          {overview ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 14 }}>
              <div>
                Ollama URL: <strong>{overview.info.ollama_base_url}</strong>
              </div>
              <div>
                Default model: <strong>{overview.info.default_model || 'Not set'}</strong>
              </div>
              <div>
                RAG: <strong>{overview.info.rag_enabled ? 'Enabled' : 'Disabled'}</strong>
              </div>
            </div>
          ) : (
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>No configuration loaded yet.</p>
          )}
          <div style={{ marginTop: '1rem' }}>
            <a
              href="/admin"
              style={{
                display: 'inline-block',
                padding: '8px 16px',
                background: '#0066cc',
                color: 'white',
                borderRadius: 6,
                textDecoration: 'none',
                fontSize: 14,
                fontWeight: 600,
              }}
            >
              Open Configuration Wizard
            </a>
          </div>
        </section>

        {/* Access / User Management */}
        <section style={cardStyle} aria-label="Access management">
          <h2 style={sectionTitleStyle}>Access Management</h2>
          {accessLoading ? (
            <LoadingSpinner label="Loading access settings..." />
          ) : accessError ? (
            <ErrorMessage title="Failed to load access settings" message={accessError} onRetry={loadAccess} retrying={accessLoading} />
          ) : access ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <p style={{ fontSize: 13, color: 'var(--muted-foreground)', margin: 0 }}>
                nyxGPT is a single-operator local system secured by a shared API key rather than
                per-user accounts. Manage that key here.
              </p>

              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, cursor: 'pointer' }}>
                <input type="checkbox" checked={access.enabled} disabled={accessSaving} onChange={handleToggleAuth} />
                API key authentication {access.enabled ? 'enabled' : 'disabled'}
              </label>

              <div style={{ fontSize: 14 }}>
                Current key:{' '}
                <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
                  {access.api_key_set ? access.api_key_masked : 'not set'}
                </code>
              </div>

              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <label htmlFor="auth-header" style={{ fontSize: 14 }}>
                  Header:
                </label>
                <input
                  id="auth-header"
                  type="text"
                  value={headerInput}
                  onChange={(e) => setHeaderInput(e.target.value)}
                  disabled={accessSaving}
                  style={{
                    padding: '6px 8px',
                    border: '1px solid var(--border)',
                    borderRadius: 6,
                    fontSize: 13,
                    background: 'var(--background)',
                    color: 'var(--foreground)',
                  }}
                />
                <button
                  onClick={handleSaveHeader}
                  disabled={accessSaving}
                  style={{
                    padding: '6px 12px',
                    background: 'var(--muted)',
                    border: '1px solid var(--border)',
                    borderRadius: 6,
                    fontSize: 13,
                    cursor: accessSaving ? 'not-allowed' : 'pointer',
                  }}
                >
                  Save
                </button>
              </div>

              <button
                onClick={handleRotateKey}
                disabled={accessSaving}
                style={{
                  padding: '8px 16px',
                  background: accessSaving ? '#ccc' : '#dc3545',
                  color: 'white',
                  border: 'none',
                  borderRadius: 6,
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: accessSaving ? 'not-allowed' : 'pointer',
                  alignSelf: 'flex-start',
                }}
              >
                {accessSaving ? 'Working...' : 'Rotate API Key'}
              </button>

              {revealedKey && (
                <div
                  role="status"
                  style={{
                    padding: '10px 12px',
                    borderRadius: 6,
                    fontSize: 13,
                    background: 'var(--success-bg)',
                    color: 'var(--success-text)',
                    border: '1px solid #90ee90',
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>New API key (shown once)</div>
                  <code style={{ wordBreak: 'break-all' }}>{revealedKey}</code>
                </div>
              )}
            </div>
          ) : null}
        </section>

        {/* Activity Log */}
        <section style={cardStyle} aria-label="Activity log">
          <h2 style={sectionTitleStyle}>Activity Log</h2>
          {activityLoading ? (
            <LoadingSpinner label="Loading activity..." />
          ) : activityError ? (
            <ErrorMessage title="Failed to load activity log" message={activityError} onRetry={loadActivity} retrying={activityLoading} />
          ) : activity.length === 0 ? (
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>No admin activity recorded yet.</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 320, overflowY: 'auto' }}>
              {[...activity].reverse().map((event, idx) => (
                <div
                  key={`${event.ts}-${idx}`}
                  style={{
                    padding: '8px 10px',
                    borderBottom: idx < activity.length - 1 ? '1px solid var(--border-light)' : 'none',
                    fontSize: 13,
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{event.action}</div>
                  <div style={{ color: 'var(--muted-foreground)' }}>{event.detail}</div>
                  <div style={{ color: 'var(--muted-foreground)', fontSize: 11 }}>{formatTimestamp(event.ts)}</div>
                </div>
              ))}
            </div>
          )}
          <div style={{ marginTop: '1rem', fontSize: 13 }}>
            <a href="/admin/logs" style={{ color: '#0066cc' }}>View raw application logs →</a>
          </div>
        </section>
      </div>
    </main>
  );
}

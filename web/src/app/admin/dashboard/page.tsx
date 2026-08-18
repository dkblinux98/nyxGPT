'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';
import QueryCacheStatsPanel from '../../../components/QueryCacheStatsPanel';
import ObservabilityCredentialsHint from '../../../components/ObservabilityCredentialsHint';
import { ADMIN_NAV, grafanaSreHomeUrl } from './nav';

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
  canary: { active?: boolean; error?: string } & Record<string, unknown>;
  self_heal: { enabled?: boolean; unhealthy_count?: number; error?: string } & Record<
    string,
    unknown
  >;
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

/** `{component: {keys, since}}` -- what a wizard save flagged as needing a restart, and why (#3407). */
type RestartPending = Record<string, { keys: string[]; since: number }>;

/** How many times to poll `restart-status` after triggering a restart before giving up (#3407). */
const RESTART_POLL_ATTEMPTS = 10;
const RESTART_POLL_INTERVAL_MS = 2000;

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

// ADMIN_NAV (the quick-nav destinations rendered as a card grid) lives in
// ./nav so this page file exports only its default component -- see nav.ts
// for why a named export here would break `next build`.

// Rendered alongside the `operation` group in Configuration, in the same
// tile grid, so the wizard isn't a visually distinct button next to a group
// of tiles.
const CONFIG_WIZARD_TILE = {
  href: '/admin',
  label: 'Configuration Wizard',
  description: 'Edit every config.ini section, with apply-on-save reload',
};

const navTileStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  padding: '10px 12px',
  borderRadius: 8,
  border: '1px solid var(--border)',
  background: 'var(--background)',
  textDecoration: 'none',
  transition: 'border-color 0.15s ease, background 0.15s ease',
};

const inlineLinkStyle: React.CSSProperties = {
  color: 'var(--link)',
  textDecoration: 'underline',
  whiteSpace: 'nowrap',
};

function NavTile({
  dest,
}: {
  dest: { href: string; label: string; description: string; external?: boolean };
}) {
  return (
    <a
      href={dest.href}
      title={dest.description}
      style={navTileStyle}
      {...(dest.external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--link)';
        e.currentTarget.style.background = 'var(--muted)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--border)';
        e.currentTarget.style.background = 'var(--background)';
      }}
    >
      <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--foreground)' }}>
        {dest.label}
        {dest.external ? ' ↗' : ''}
      </span>
      <span style={{ fontSize: 12, color: 'var(--muted-foreground)' }}>{dest.description}</span>
    </a>
  );
}

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
          background: ok ? '#22c55e' : 'var(--muted-foreground)',
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

  const [restartPending, setRestartPending] = useState<RestartPending>({});
  const [restartAction, setRestartAction] = useState<'idle' | 'running' | 'succeeded' | 'failed'>(
    'idle'
  );
  const [restartActionError, setRestartActionError] = useState<string | null>(null);

  // Grafana's own URL, used only to build the SRE Overview tile's target --
  // it launches the Grafana single pane of glass in a new tab (#3411)
  // instead of an in-app nyxGPT page. Falls back to nav.ts's same default
  // (`grafana_ui_url` unconfigured) until this loads.
  const [grafanaUiUrl, setGrafanaUiUrl] = useState<string | null>(null);

  const loadMonitoring = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/monitoring', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (data.grafana_ui_url) setGrafanaUiUrl(data.grafana_ui_url);
    } catch {
      // Best-effort: the SRE Overview tile just keeps its default target.
    }
  }, []);

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

  const loadRestartStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/infra/restart-status', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      setRestartPending(data.pending || {});
    } catch {
      // Best-effort: the banner just stays at its last-known state on failure.
    }
  }, []);

  useEffect(() => {
    loadOverview();
    loadActivity();
    loadAccess();
    loadRestartStatus();
    loadMonitoring();
  }, [loadOverview, loadActivity, loadAccess, loadRestartStatus, loadMonitoring]);

  /**
   * Triggers the mode-aware restart-required flow (#3407) and polls
   * `restart-status` until the pending flag clears (success) or the poll
   * budget runs out (treated as failure -- the operator can retry or check
   * manually). The restart itself runs off-thread on the backend, so there
   * is no synchronous result to read from the POST response.
   */
  async function handleRestartRequired() {
    setRestartAction('running');
    setRestartActionError(null);
    try {
      const res = await fetch('/api/v1/infra/restart-required', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data?.detail || `HTTP ${res.status}`);
      }
    } catch (e: unknown) {
      setRestartAction('failed');
      setRestartActionError(e instanceof Error ? e.message : String(e));
      return;
    }

    for (let attempt = 0; attempt < RESTART_POLL_ATTEMPTS; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RESTART_POLL_INTERVAL_MS));
      try {
        const res = await fetch('/api/v1/infra/restart-status', { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          const pending = (data.pending || {}) as RestartPending;
          setRestartPending(pending);
          if (Object.keys(pending).length === 0) {
            setRestartAction('succeeded');
            loadOverview();
            return;
          }
        }
      } catch {
        // A connection error mid-restart (e.g. the api component itself
        // restarting) is expected -- keep polling rather than failing early.
      }
    }
    setRestartAction('failed');
    setRestartActionError('Restart did not complete in time -- check status and retry.');
  }

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
    // This handler is only ever wired to the auth checkbox, which is rendered
    // solely inside the `access ? (...) : null` branch below, so `access` is
    // always non-null here. Asserting that instead of an unreachable early
    // return keeps the branch coverage honest about actual code paths.
    updateAccess({ enabled: !(access as AccessData).enabled });
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
        <a href="/" style={inlineLinkStyle}>
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
          ) : (
            // `overview` is guaranteed non-null here: loadOverview's
            // try/catch/finally always sets either `overview` or
            // `overviewError` before clearing `overviewLoading`, so
            // `!overviewLoading && !overviewError && !overview` can never
            // occur. Asserting that documents the invariant instead of
            // adding an unreachable `: null` fallback branch.
            (() => {
              const ov = overview as OverviewData;
              return (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    <StatusBadge ok={!!ov.canary.active} label={ov.canary.active ? 'Canary: active' : 'Canary: idle'} />
                    <StatusBadge
                      ok={!!ov.self_heal?.enabled && !ov.self_heal?.unhealthy_count}
                      label={
                        ov.self_heal?.enabled
                          ? `Self-heal: on${ov.self_heal.unhealthy_count ? ` (${ov.self_heal.unhealthy_count} unhealthy)` : ''}`
                          : 'Self-heal: off'
                      }
                    />
                    <StatusBadge ok={ov.observability.monitoring} label="Monitoring" />
                    <StatusBadge ok={ov.observability.tracing} label="Tracing" />
                    <StatusBadge ok={ov.observability.error_tracking} label="Error tracking" />
                    <StatusBadge ok={ov.observability.log_aggregation} label="Log aggregation" />
                    <StatusBadge ok={ov.auth_enabled} label={ov.auth_enabled ? 'Auth: enabled' : 'Auth: disabled'} />
                  </div>
                  {ov.resource_metrics && (
                    <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                      Memory: <strong>{ov.resource_metrics.memory.rss_mb.toFixed(0)} MB</strong> ({ov.resource_metrics.memory.percent.toFixed(1)}%) · CPU (process):{' '}
                      <strong>{ov.resource_metrics.cpu.process_percent.toFixed(1)}%</strong> · Queue depth:{' '}
                      <strong>{ov.resource_metrics.queue.depth}</strong>
                    </div>
                  )}
                  <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                    Default model: <strong>{ov.info.default_model || 'Not set'}</strong> · RAG:{' '}
                    <strong>{ov.info.rag_enabled ? 'enabled' : 'disabled'}</strong>
                  </div>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                      gap: 8,
                      marginTop: 4,
                    }}
                  >
                    {ADMIN_NAV.filter((dest) => dest.group === 'observation').map((dest) => (
                      <NavTile
                        key={dest.label}
                        dest={
                          dest.external && grafanaUiUrl
                            ? { ...dest, href: grafanaSreHomeUrl(grafanaUiUrl) }
                            : dest
                        }
                      />
                    ))}
                  </div>
                  {/* The SRE Overview tile above opens Grafana, which asks for
                      a login the API is not allowed to hand out (#3718). */}
                  <ObservabilityCredentialsHint />
                </div>
              );
            })()
          )}
        </section>

        {/* Configuration Management, with the restart-required banner (#3407)
            stacked directly above it -- same width, outside the card. */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          {Object.keys(restartPending).length > 0 && (
            <div
              role="alert"
              aria-label="Restart required"
              style={{
                padding: '1rem',
                borderRadius: 8,
                border: '1px solid var(--link)',
                background: 'var(--info-bg)',
              }}
            >
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6 }}>
                Restart required
              </div>
              <div style={{ fontSize: 13, color: 'var(--muted-foreground)', marginBottom: 10 }}>
                Saved configuration changes need a restart to take effect:{' '}
                {Object.entries(restartPending)
                  .map(([component, detail]) => `${component} (${detail.keys.join(', ')})`)
                  .join('; ')}
                .
              </div>
              <button
                onClick={handleRestartRequired}
                disabled={restartAction === 'running'}
                aria-busy={restartAction === 'running'}
                style={{
                  padding: '8px 16px',
                  background: restartAction === 'running' ? '#ccc' : '#0066cc',
                  color: 'white',
                  border: 'none',
                  borderRadius: 6,
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: restartAction === 'running' ? 'not-allowed' : 'pointer',
                }}
              >
                {restartAction === 'running' ? 'Restarting…' : 'Restart now'}
              </button>
              {restartAction === 'failed' && restartActionError && (
                <div style={{ color: 'var(--error-text)', fontSize: 13, marginTop: 8 }}>
                  {restartActionError}
                </div>
              )}
            </div>
          )}

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
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                gap: 8,
                marginTop: '1rem',
              }}
            >
              {[...ADMIN_NAV.filter((dest) => dest.group === 'operation'), CONFIG_WIZARD_TILE].map((dest) => (
                <NavTile key={dest.href} dest={dest} />
              ))}
            </div>
            {/* Where credential entry used to be offered (#3805): text, not a
                control. The Guided Secrets and AWS Credentials screens were
                removed because a browser is a worse surface for a credential
                than a terminal, and because reaching this dashboard already
                required the secrets they collected. Same pointer pattern as
                the cloud surface (#3514). */}
            <p
              style={{
                marginTop: '1rem',
                marginBottom: 0,
                fontSize: 13,
                color: 'var(--muted-foreground)',
              }}
            >
              Secrets and cloud credentials are entered from the terminal, not here — masked
              entry writes straight to <code>config.ini</code>, <code>~/.aws/credentials</code>{' '}
              or the OS keychain, with no browser process or HTTP request in between. Run{' '}
              <code>nyxgpt secrets setup</code> for the guided secrets,{' '}
              <code>nyxgpt ops secrets-sync</code> to push them to GitHub Actions, and{' '}
              <code>nyxgpt cloud credentials-setup</code> for AWS identity. The Configuration
              Wizard above still edits every non-credential setting.
            </p>
          </section>
        </div>

        {/* Access / User Management */}
        <section style={cardStyle} aria-label="Access management">
          <h2 style={sectionTitleStyle}>Access Management</h2>
          {accessLoading ? (
            <LoadingSpinner label="Loading access settings..." />
          ) : accessError ? (
            <ErrorMessage title="Failed to load access settings" message={accessError} onRetry={loadAccess} retrying={accessLoading} />
          ) : (
            // `access` is guaranteed non-null here: loadAccess's
            // try/catch/finally always sets either `access` or
            // `accessError` before clearing `accessLoading`, so
            // `!accessLoading && !accessError && !access` can never occur.
            // Asserting that documents the invariant instead of adding an
            // unreachable `: null` fallback branch.
            (() => {
              const acc = access as AccessData;
              return (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <p style={{ fontSize: 13, color: 'var(--muted-foreground)', margin: 0 }}>
                    nyxGPT is a single-operator local system secured by a shared API key rather than
                    per-user accounts. Manage that key here.
                  </p>

                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, cursor: 'pointer' }}>
                    <input type="checkbox" checked={acc.enabled} disabled={accessSaving} onChange={handleToggleAuth} />
                    API key authentication {acc.enabled ? 'enabled' : 'disabled'}
                  </label>

                  <div style={{ fontSize: 14 }}>
                    Current key:{' '}
                    <code
                      style={{
                        background: 'var(--code-bg)',
                        padding: '2px 6px',
                        borderRadius: 4,
                        wordBreak: 'break-all',
                        overflowWrap: 'anywhere',
                      }}
                    >
                      {acc.api_key_set ? acc.api_key_masked : 'not set'}
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
              );
            })()
          )}
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
        </section>

        {/* Query Cache Statistics */}
        <QueryCacheStatsPanel />
      </div>
    </main>
  );
}

'use client';

import { useEffect, useState } from 'react';

type ErrorTrackingStatus = {
  enabled: boolean;
  active: boolean;
  dsn: string;
  environment: string;
  glitchtip_ui_url: string;
};

type TestEventResult = {
  ok: boolean;
  message: string;
};

const codeStyle = {
  background: 'var(--code-bg)',
  padding: '2px 6px',
  borderRadius: 4,
};

export default function ErrorTrackingPanel() {
  const [status, setStatus] = useState<ErrorTrackingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TestEventResult | null>(null);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      try {
        const res = await fetch('/api/v1/error-tracking', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to fetch error tracking status: HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) {
          setStatus(data);
          setError(null);
        }
      } catch (e: unknown) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    }

    loadStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  async function sendTestEvent() {
    setSending(true);
    setTestResult(null);
    try {
      const res = await fetch('/api/v1/error-tracking/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: 'nyxGPT error tracking test event',
          url: '/admin/observability',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 202 && data.status === 'accepted') {
        setTestResult({ ok: true, message: 'Delivered -- check the GlitchTip UI for this event.' });
      } else if (res.status === 503 || data.status === 'inactive') {
        setTestResult({
          ok: false,
          message: data.detail || 'Error tracking is inactive -- the test event was not sent.',
        });
      } else {
        setTestResult({ ok: false, message: `Unexpected response: HTTP ${res.status}` });
      }
    } catch (e: unknown) {
      setTestResult({
        ok: false,
        message: e instanceof Error ? e.message : 'Network error sending test event',
      });
    } finally {
      setSending(false);
    }
  }

  return (
    <div
      style={{
        marginTop: '1rem',
        padding: '1rem',
        border: '1px solid var(--border)',
        borderRadius: 6,
        background: 'var(--background)',
        fontSize: 13,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Error Tracking</div>

      {error && (
        <p role="alert" style={{ margin: '0 0 0.75rem 0', color: 'var(--error-text)' }}>
          {error}
        </p>
      )}

      {!error && !status && <p style={{ margin: 0, color: '#666' }}>Loading error tracking status…</p>}

      {status && !status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Error tracking is disabled. Unhandled backend exceptions and web UI errors can be
            reported to a <strong>self-hosted</strong> GlitchTip instance -- no data ever leaves
            this machine, and nothing talks to Sentry&apos;s own SaaS. nyxGPT reports via the{' '}
            <strong>Python</strong> <code style={codeStyle}>sentry_sdk</code> (see{' '}
            <code style={codeStyle}>src/nyxgpt/error_tracking.py</code>) -- if GlitchTip&apos;s own
            onboarding screen shows Node.js/<code>@sentry/node</code> setup instructions, ignore
            them, that&apos;s not this integration.
          </p>
          <ol style={{ margin: '0 0 0.75rem 0', paddingLeft: '1.25rem', color: '#666' }}>
            <li style={{ marginBottom: 4 }}>
              Start the local tracker:{' '}
              <code style={codeStyle}>docker compose --profile errors up</code>
            </li>
            <li style={{ marginBottom: 4 }}>
              Register the first account at{' '}
              <code style={codeStyle}>http://localhost:8080</code>. Its confirmation email goes to
              the container&apos;s console log, not a real inbox -- read it with{' '}
              <code style={codeStyle}>nyxgpt ops logs glitchtip</code> (no raw{' '}
              <code style={codeStyle}>docker</code> command needed) and open the confirmation link
              it prints.
            </li>
            <li style={{ marginBottom: 4 }}>
              Create a project in the GlitchTip UI and copy its DSN -- it&apos;ll look like{' '}
              <code style={codeStyle}>http://&lt;key&gt;@localhost:8080/&lt;id&gt;</code>.
            </li>
            <li style={{ marginBottom: 4 }}>
              Paste it as-is into <code style={codeStyle}>[error_tracking] dsn</code> in{' '}
              <code style={codeStyle}>~/.nyxGPT/config.ini</code> (native) or{' '}
              <code style={codeStyle}>docker/config.docker.ini</code> (Compose), and set{' '}
              <code style={codeStyle}>enabled = true</code>. The{' '}
              <code style={codeStyle}>localhost</code> host is fine either way -- nyxGPT
              automatically rewrites it to the Compose service name when the API is
              containerized, so there&apos;s no host to edit by hand.
            </li>
            <li>Restart the API (config.ini isn&apos;t hot-reloaded for this setting).</li>
          </ol>
        </>
      )}

      {status && status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Error tracking is active for environment <code>{status.environment}</code>. Browse
            reported exceptions in the local GlitchTip UI:
          </p>
          <a
            href={status.glitchtip_ui_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: '#0066cc', fontSize: 13 }}
          >
            Open GlitchTip UI ↗
          </a>
        </>
      )}

      {status && (
        <div style={{ marginTop: '0.75rem', paddingTop: '0.75rem', borderTop: '1px solid var(--border)' }}>
          <button
            type="button"
            onClick={sendTestEvent}
            disabled={sending}
            style={{
              fontSize: 12,
              padding: '4px 10px',
              borderRadius: 4,
              border: '1px solid var(--border)',
              background: 'var(--code-bg)',
              cursor: sending ? 'default' : 'pointer',
            }}
          >
            {sending ? 'Sending…' : 'Send test event'}
          </button>
          {testResult && (
            <p
              role="status"
              style={{
                margin: '0.5rem 0 0 0',
                color: testResult.ok ? 'var(--success-text)' : 'var(--error-text)',
              }}
            >
              {testResult.ok ? '✓' : '✗'} {testResult.message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

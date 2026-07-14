'use client';

import { useEffect, useState } from 'react';

type ErrorTrackingStatus = {
  enabled: boolean;
  active: boolean;
  dsn: string;
  environment: string;
  glitchtip_ui_url: string;
};

export default function ErrorTrackingPanel() {
  const [status, setStatus] = useState<ErrorTrackingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

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
            this machine, and nothing talks to Sentry&apos;s own SaaS.
          </p>
          <p style={{ margin: 0, color: '#666' }}>
            To enable: start the local tracker with{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              docker compose --profile errors up
            </code>
            , create a project in its UI, then set{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              [error_tracking] enabled = true
            </code>{' '}
            and that project&apos;s <code>dsn</code> in{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              ~/.nyxGPT/config.ini
            </code>
            .
          </p>
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
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';

type MonitoringStatus = {
  enabled: boolean;
  active: boolean;
  grafana_ui_url: string;
  prometheus_ui_url: string;
};

export default function GrafanaPanel() {
  const [status, setStatus] = useState<MonitoringStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      try {
        const res = await fetch('/api/v1/monitoring', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to fetch monitoring status: HTTP ${res.status}`);
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
      <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Monitoring Dashboards</div>

      {error && (
        <p role="alert" style={{ margin: '0 0 0.75rem 0', color: 'var(--error-text)' }}>
          {error}
        </p>
      )}

      {!error && !status && <p style={{ margin: 0, color: '#666' }}>Loading monitoring status…</p>}

      {status && !status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            System overview, RAG performance, and API metrics dashboards can be viewed in a{' '}
            <strong>local-only</strong> Grafana instance, backed by a Prometheus server that
            scrapes the <code>/metrics</code> endpoint above -- nothing is sent to an
            external/cloud monitoring service.
          </p>
          <p style={{ margin: 0, color: '#666' }}>
            To enable: start the local stack with{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              docker compose --profile monitoring up
            </code>{' '}
            and set{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              [monitoring] enabled = true
            </code>{' '}
            in{' '}
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
            Monitoring is active. Browse the pre-provisioned dashboards in the local Grafana UI,
            or query raw metrics directly in the local Prometheus UI:
          </p>
          <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
            <a
              href={status.grafana_ui_url}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: '#0066cc', fontSize: 13 }}
            >
              Open Grafana ↗
            </a>
            <a
              href={status.prometheus_ui_url}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: '#0066cc', fontSize: 13 }}
            >
              Open Prometheus ↗
            </a>
          </div>
        </>
      )}
    </div>
  );
}

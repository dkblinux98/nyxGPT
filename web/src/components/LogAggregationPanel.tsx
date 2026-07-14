'use client';

import { useEffect, useState } from 'react';

type LogAggregationStatus = {
  enabled: boolean;
  active: boolean;
  grafana_explore_url: string;
};

export default function LogAggregationPanel() {
  const [status, setStatus] = useState<LogAggregationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      try {
        const res = await fetch('/api/v1/log-aggregation', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to fetch log aggregation status: HTTP ${res.status}`);
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
      <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Log Aggregation</div>

      {error && (
        <p role="alert" style={{ margin: '0 0 0.75rem 0', color: 'var(--error-text)' }}>
          {error}
        </p>
      )}

      {!error && !status && <p style={{ margin: 0, color: '#666' }}>Loading log aggregation status…</p>}

      {status && !status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Log files under <code>~/.nyxGPT/logs</code> can be searched centrally in Grafana,
            shipped by promtail into a <strong>local-only</strong> Loki instance -- nothing is
            sent to an external/cloud log service.
          </p>
          <p style={{ margin: 0, color: '#666' }}>
            To enable: set <code>[log_aggregation] enabled = true</code> in{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              ~/.nyxGPT/config.ini
            </code>{' '}
            and start the local stack with{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              docker compose --profile logging up
            </code>
            .
          </p>
        </>
      )}

      {status && status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Log aggregation is active. Search and filter logs in the Grafana Logs Explorer
            dashboard, backed by Loki:
          </p>
          <a
            href={status.grafana_explore_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: '#0066cc', fontSize: 13 }}
          >
            Open Grafana Explore ↗
          </a>
        </>
      )}
    </div>
  );
}

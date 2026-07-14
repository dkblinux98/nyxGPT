'use client';

import { useEffect, useState } from 'react';

type TracingStatus = {
  enabled: boolean;
  active: boolean;
  service_name: string;
  otlp_endpoint: string;
  jaeger_ui_url: string;
};

export default function TracingPanel() {
  const [status, setStatus] = useState<TracingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      try {
        const res = await fetch('/api/v1/tracing', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to fetch tracing status: HTTP ${res.status}`);
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
      <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Distributed Tracing</div>

      {error && (
        <p role="alert" style={{ margin: '0 0 0.75rem 0', color: 'var(--error-text)' }}>
          {error}
        </p>
      )}

      {!error && !status && <p style={{ margin: 0, color: '#666' }}>Loading tracing status…</p>}

      {status && !status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Tracing is disabled. Requests, RAG retrieval, Ollama calls, and Cassandra queries can
            be traced end-to-end with OpenTelemetry, exported to a <strong>local-only</strong>{' '}
            Jaeger instance -- no data ever leaves this machine.
          </p>
          <p style={{ margin: 0, color: '#666' }}>
            To enable: set <code>[tracing] enabled = true</code> in{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              ~/.nyxGPT/config.ini
            </code>{' '}
            and start the local collector with{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              docker compose --profile tracing up
            </code>
            .
          </p>
        </>
      )}

      {status && status.active && (
        <>
          <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
            Tracing is active. Spans for service <code>{status.service_name}</code> are exported
            to a local OTel collector at <code>{status.otlp_endpoint}</code>. Browse traces in the
            local Jaeger UI:
          </p>
          <a
            href={status.jaeger_ui_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: '#0066cc', fontSize: 13 }}
          >
            Open Jaeger UI ↗
          </a>
        </>
      )}
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';

type CuratedView = {
  label: string;
  hint: string;
  url: string;
};

type TracingStatus = {
  enabled: boolean;
  active: boolean;
  reachable: boolean | null;
  service_name: string;
  otlp_endpoint: string;
  jaeger_ui_url: string;
  curated_views: CuratedView[];
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
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              nyxgpt ops install
            </code>{' '}
            starts the local collector and flips this on automatically. If you skipped it (
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              --skip-observability
            </code>
            ) or need to re-run it, use{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              nyxgpt ops observability
            </code>{' '}
            -- no raw <code>docker</code> command needed.
          </p>
        </>
      )}

      {status && status.active && status.reachable === false && (
        <p role="alert" style={{ margin: '0 0 0.75rem 0', color: 'var(--error-text)' }}>
          Tracing is enabled, but nothing is listening at <code>{status.otlp_endpoint}</code> --
          spans for service <code>{status.service_name}</code> are being silently dropped and
          Jaeger will stay empty. Confirm the otel-collector container is running and publishes
          that port to the host (<code>nyxgpt ops doctor</code> will also flag this).
        </p>
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

          {status.curated_views && status.curated_views.length > 0 && (
            <div style={{ marginTop: '0.75rem' }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>Curated trace views</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {status.curated_views.map((view) => (
                  <div key={view.label}>
                    <a
                      href={view.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: '#0066cc', fontSize: 13 }}
                    >
                      {view.label} ↗
                    </a>
                    <div style={{ color: '#666', fontSize: 12 }}>{view.hint}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

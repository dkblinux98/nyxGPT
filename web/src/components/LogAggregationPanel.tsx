'use client';

import { useEffect, useState } from 'react';

type CuratedQuery = {
  label: string;
  hint: string;
  query: string;
};

// Build a Grafana Explore deep link that opens with the given LogQL query
// already loaded against the provisioned Loki datasource (uid "loki", pinned
// in docker/grafana/provisioning/datasources). Grafana's Explore state is
// URL-encodable (the `panes` param, stable since Grafana 10), which is what
// lets these curated queries be one click instead of copy/paste — Grafana has
// no file-provisioning for Explore saved queries. Exported for direct testing.
export function exploreQueryUrl(exploreBase: string, query: string): string {
  const panes = {
    nyx: {
      datasource: 'loki',
      queries: [
        { refId: 'A', expr: query, queryType: 'range', datasource: { type: 'loki', uid: 'loki' } },
      ],
      range: { from: 'now-1h', to: 'now' },
    },
  };
  const url = new URL(exploreBase);
  url.searchParams.set('schemaVersion', '1');
  if (!url.searchParams.has('orgId')) {
    url.searchParams.set('orgId', '1');
  }
  url.searchParams.set('panes', JSON.stringify(panes));
  return url.toString();
}

type LogAggregationStatus = {
  enabled: boolean;
  active: boolean;
  grafana_explore_url: string;
  curated_queries: CuratedQuery[];
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
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              nyxgpt ops install
            </code>{' '}
            starts the local stack and flips this on automatically. If you skipped it (
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

          {status.curated_queries && status.curated_queries.length > 0 && (
            <div style={{ marginTop: '0.75rem' }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>Curated queries</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {status.curated_queries.map((q) => (
                  <div key={q.label}>
                    <div style={{ fontSize: 13 }}>
                      {q.label}{' '}
                      <a
                        href={exploreQueryUrl(status.grafana_explore_url, q.query)}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: '#0066cc', fontSize: 12 }}
                      >
                        Open in Explore ↗
                      </a>
                    </div>
                    <div style={{ color: '#666', fontSize: 12, marginBottom: 2 }}>{q.hint}</div>
                    <code
                      style={{
                        display: 'block',
                        background: 'var(--code-bg)',
                        padding: '2px 6px',
                        borderRadius: 4,
                        fontSize: 12,
                        overflowWrap: 'anywhere',
                      }}
                    >
                      {q.query}
                    </code>
                  </div>
                ))}
              </div>
              <p style={{ margin: '0.5rem 0 0 0', color: '#666', fontSize: 12 }}>
                Each link opens Grafana Explore (Loki datasource) with the query already loaded.
                They are also available as panels on the &quot;Operational Logs&quot; dashboard.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}

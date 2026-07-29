'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type ModelUsage = {
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
};

type DayUsage = {
  date: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
};

type UsageSummary = {
  total_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  session_count: number;
  by_model: ModelUsage[];
  by_day: DayUsage[];
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

function StatTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div
      style={{
        padding: '1rem',
        border: '1px solid var(--border)',
        borderRadius: 8,
        background: 'var(--background)',
        textAlign: 'center',
      }}
    >
      <div style={{ fontSize: 24, fontWeight: 700 }}>{value}</div>
      <div style={{ fontSize: 13, color: 'var(--muted-foreground)', marginTop: 4 }}>{label}</div>
    </div>
  );
}

function formatNumber(n: number): string {
  return n.toLocaleString();
}

// Chat/RAG usage-over-time content, relocated from the retired
// `/admin/analytics` route onto the System Health screen (#3413) -- this is
// now the only home for usage analytics, so it renders as a section rather
// than its own page (no heading/back-link of its own; the host page
// supplies those).
export default function UsageAnalyticsSection() {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<'json' | 'csv' | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const loadSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/analytics/usage', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load usage analytics: HTTP ${res.status}`);
      const data = await res.json();
      setSummary(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  async function handleExport(format: 'json' | 'csv') {
    setExporting(format);
    setExportError(null);
    try {
      const res = await fetch(`/api/v1/analytics/export?format=${format}`);
      if (!res.ok) throw new Error(`Export failed: HTTP ${res.status}`);

      const contentDisposition = res.headers.get('Content-Disposition');
      let filename = `usage_report.${format}`;
      if (contentDisposition) {
        const match = contentDisposition.match(/filename="(.+?)"/);
        if (match) filename = match[1];
      }

      const blob = await res.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } finally {
        window.URL.revokeObjectURL(downloadUrl);
      }
    } catch (e: unknown) {
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(null);
    }
  }

  const maxDayRequests = summary ? Math.max(1, ...summary.by_day.map((d) => d.requests)) : 1;

  if (loading && !summary) {
    return <LoadingSpinner label="Loading usage analytics..." />;
  }

  if (error) {
    return (
      <ErrorMessage title="Failed to load usage analytics" message={error} onRetry={loadSummary} retrying={loading} />
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
      {/* Summary stat tiles */}
      <section aria-label="Usage totals">
        <div style={{ display: 'grid', gap: '1rem', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))' }}>
          <StatTile label="Total requests" value={formatNumber(summary!.total_requests)} />
          <StatTile label="Total tokens" value={formatNumber(summary!.total_tokens)} />
          <StatTile label="Prompt tokens" value={formatNumber(summary!.total_prompt_tokens)} />
          <StatTile label="Completion tokens" value={formatNumber(summary!.total_completion_tokens)} />
          <StatTile label="Sessions" value={formatNumber(summary!.session_count)} />
        </div>
      </section>

      <div style={{ display: 'grid', gap: '1.5rem', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))' }}>
        {/* Model usage breakdown */}
        <section style={cardStyle} aria-label="Model usage breakdown">
          <h3 style={sectionTitleStyle}>Model Usage</h3>
          {summary!.by_model.length === 0 ? (
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>No usage recorded yet.</p>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--border)' }}>
                  <th style={{ padding: '6px 4px' }}>Model</th>
                  <th style={{ padding: '6px 4px' }}>Requests</th>
                  <th style={{ padding: '6px 4px' }}>Prompt</th>
                  <th style={{ padding: '6px 4px' }}>Completion</th>
                </tr>
              </thead>
              <tbody>
                {summary!.by_model.map((m) => (
                  <tr key={m.model} style={{ borderBottom: '1px solid var(--border-light)' }}>
                    <td style={{ padding: '6px 4px' }}>{m.model}</td>
                    <td style={{ padding: '6px 4px' }}>{formatNumber(m.requests)}</td>
                    <td style={{ padding: '6px 4px' }}>{formatNumber(m.prompt_tokens)}</td>
                    <td style={{ padding: '6px 4px' }}>{formatNumber(m.completion_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* Requests over time */}
        <section style={cardStyle} aria-label="Requests over time">
          <h3 style={sectionTitleStyle}>Requests by Day</h3>
          {summary!.by_day.length === 0 ? (
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14 }}>No usage recorded yet.</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {summary!.by_day.map((d) => (
                <div key={d.date} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                  <span style={{ width: 90, color: 'var(--muted-foreground)' }}>{d.date}</span>
                  <div style={{ flex: 1, background: 'var(--muted)', borderRadius: 4, height: 14 }}>
                    <div
                      style={{
                        width: `${(d.requests / maxDayRequests) * 100}%`,
                        background: '#0066cc',
                        height: '100%',
                        borderRadius: 4,
                      }}
                    />
                  </div>
                  <span style={{ width: 32, textAlign: 'right' }}>{d.requests}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Export reports */}
      <section style={cardStyle} aria-label="Export reports">
        <h3 style={sectionTitleStyle}>Export Report</h3>
        <p style={{ fontSize: 13, color: 'var(--muted-foreground)', marginTop: 0 }}>
          Download the recorded usage events (session, model, token counts, and duration per request).
        </p>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            onClick={() => handleExport('json')}
            disabled={exporting !== null}
            style={{
              padding: '8px 16px',
              background: exporting === 'json' ? '#ccc' : '#0066cc',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor: exporting !== null ? 'not-allowed' : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            {exporting === 'json' ? 'Exporting…' : 'Export JSON'}
          </button>
          <button
            onClick={() => handleExport('csv')}
            disabled={exporting !== null}
            style={{
              padding: '8px 16px',
              background: exporting === 'csv' ? '#ccc' : 'var(--muted)',
              color: exporting === 'csv' ? 'white' : 'var(--foreground)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              cursor: exporting !== null ? 'not-allowed' : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            {exporting === 'csv' ? 'Exporting…' : 'Export CSV'}
          </button>
        </div>
        {exportError && (
          <div style={{ marginTop: 12 }}>
            <ErrorMessage title="Export failed" message={exportError} onRetry={() => handleExport('json')} retrying={false} />
          </div>
        )}
      </section>
    </div>
  );
}

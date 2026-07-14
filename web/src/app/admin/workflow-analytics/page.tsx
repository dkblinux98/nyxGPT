'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type WorkflowBreakdown = {
  workflow: string;
  runs: number;
  success_rate: number;
  avg_duration_s: number;
  failures: number;
};

type WorkflowStats = {
  window_days: number;
  total_runs: number;
  success_rate: number;
  avg_duration_s: number;
  failures: number;
  by_workflow: WorkflowBreakdown[];
  top_failing: WorkflowBreakdown[];
};

type WorkflowRun = {
  run_id: number;
  workflow_name: string;
  status: string;
  conclusion: string | null;
  branch: string | null;
  issue_number: number | null;
  url: string | null;
  created_at: number;
  duration_s: number | null;
};

type WorkflowAnalyticsData = {
  available: boolean;
  collected?: boolean;
  reason?: string;
  stats: WorkflowStats | null;
  recent_runs: WorkflowRun[];
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

function formatDuration(seconds: number | null): string {
  if (seconds === null) return '-';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString();
}

export default function WorkflowAnalyticsPage() {
  const [data, setData] = useState<WorkflowAnalyticsData | null>(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/admin/workflow-analytics?days=${days}&limit=50`, {
        cache: 'no-store',
      });
      if (!res.ok) throw new Error(`Failed to load workflow analytics: HTTP ${res.status}`);
      const body = await res.json();
      setData(body);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  return (
    <main style={{ padding: '2rem', maxWidth: 1200, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>CI Workflow Analytics</h1>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      {loading && !data ? (
        <LoadingSpinner label="Loading workflow analytics..." />
      ) : error ? (
        <ErrorMessage title="Failed to load workflow analytics" message={error} onRetry={loadData} retrying={loading} />
      ) : data && !data.available ? (
        <div style={cardStyle}>
          <p style={{ margin: 0, color: 'var(--muted-foreground)' }}>
            Workflow analytics are unavailable ({data.reason ?? 'unknown reason'}).
          </p>
        </div>
      ) : data && !data.collected ? (
        <div style={cardStyle}>
          <p style={{ margin: 0, marginBottom: 8 }}>No workflow run history has been collected yet.</p>
          <p style={{ margin: 0, color: 'var(--muted-foreground)', fontSize: 14 }}>
            Run{' '}
            <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
              ./scripts/collect_workflow_logs.sh collect --repo OWNER/NAME
            </code>{' '}
            to populate the local history store, then refresh this page.
          </p>
        </div>
      ) : data && data.stats ? (
        <>
          <div style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: 8 }}>
            <label htmlFor="days-window" style={{ fontSize: 14, fontWeight: 600 }}>
              Window:
            </label>
            <select
              id="days-window"
              value={days}
              onChange={(e) => setDays(parseInt(e.target.value, 10))}
              style={{
                padding: '6px 10px',
                border: '1px solid var(--border)',
                borderRadius: 6,
                fontSize: 14,
                background: 'var(--background)',
                color: 'var(--foreground)',
              }}
            >
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>

          <div style={{ display: 'grid', gap: '1.5rem', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', marginBottom: '1.5rem' }}>
            <section style={cardStyle} aria-label="Summary">
              <h2 style={sectionTitleStyle}>Summary (last {data.stats.window_days} days)</h2>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 14 }}>
                <div>
                  Total runs: <strong>{data.stats.total_runs}</strong>
                </div>
                <div>
                  Success rate: <strong>{data.stats.success_rate}%</strong>
                </div>
                <div>
                  Avg duration: <strong>{formatDuration(data.stats.avg_duration_s)}</strong>
                </div>
                <div>
                  Failures: <strong>{data.stats.failures}</strong>
                </div>
              </div>
            </section>

            <section style={cardStyle} aria-label="Top failing workflows">
              <h2 style={sectionTitleStyle}>Top Failing Workflows</h2>
              {data.stats.top_failing.length === 0 ? (
                <p style={{ color: 'var(--muted-foreground)', fontSize: 14, margin: 0 }}>No failures in this window.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 14 }}>
                  {data.stats.top_failing.slice(0, 5).map((w) => (
                    <div key={w.workflow}>
                      {w.workflow}: <strong>{w.failures}</strong> failures ({w.success_rate}% success)
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>

          <section style={{ ...cardStyle, marginBottom: '1.5rem' }} aria-label="Per-workflow breakdown">
            <h2 style={sectionTitleStyle}>Per-Workflow Breakdown</h2>
            {data.stats.by_workflow.length === 0 ? (
              <p style={{ color: 'var(--muted-foreground)', fontSize: 14, margin: 0 }}>No runs in this window.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)', textAlign: 'left' }}>
                      <th style={{ padding: '8px 4px' }}>Workflow</th>
                      <th style={{ padding: '8px 4px' }}>Runs</th>
                      <th style={{ padding: '8px 4px' }}>Success %</th>
                      <th style={{ padding: '8px 4px' }}>Avg Duration</th>
                      <th style={{ padding: '8px 4px' }}>Failures</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.stats.by_workflow.map((w) => (
                      <tr key={w.workflow} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '8px 4px' }}>{w.workflow}</td>
                        <td style={{ padding: '8px 4px' }}>{w.runs}</td>
                        <td style={{ padding: '8px 4px' }}>{w.success_rate}%</td>
                        <td style={{ padding: '8px 4px' }}>{formatDuration(w.avg_duration_s)}</td>
                        <td style={{ padding: '8px 4px' }}>{w.failures}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section style={cardStyle} aria-label="Recent runs">
            <h2 style={sectionTitleStyle}>Recent Runs</h2>
            {data.recent_runs.length === 0 ? (
              <p style={{ color: 'var(--muted-foreground)', fontSize: 14, margin: 0 }}>No runs stored yet.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)', textAlign: 'left' }}>
                      <th style={{ padding: '8px 4px' }}>Workflow</th>
                      <th style={{ padding: '8px 4px' }}>Status</th>
                      <th style={{ padding: '8px 4px' }}>Branch</th>
                      <th style={{ padding: '8px 4px' }}>Issue</th>
                      <th style={{ padding: '8px 4px' }}>Duration</th>
                      <th style={{ padding: '8px 4px' }}>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_runs.map((run) => (
                      <tr key={run.run_id} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '8px 4px' }}>
                          {run.url ? (
                            <a href={run.url} target="_blank" rel="noreferrer" style={{ color: '#0066cc' }}>
                              {run.workflow_name}
                            </a>
                          ) : (
                            run.workflow_name
                          )}
                        </td>
                        <td style={{ padding: '8px 4px' }}>{run.conclusion ?? run.status}</td>
                        <td style={{ padding: '8px 4px' }}>{run.branch ?? '-'}</td>
                        <td style={{ padding: '8px 4px' }}>{run.issue_number ?? '-'}</td>
                        <td style={{ padding: '8px 4px' }}>{formatDuration(run.duration_s)}</td>
                        <td style={{ padding: '8px 4px' }}>{formatDate(run.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}
    </main>
  );
}

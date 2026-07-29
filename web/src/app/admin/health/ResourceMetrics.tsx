'use client';

import { useEffect, useState, useRef, type CSSProperties } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type MetricsData = {
  memory: {
    rss_mb: number;
    vms_mb: number;
    percent: number;
    available_mb: number;
  };
  cpu: {
    process_percent: number;
    system_percent: number;
  };
  latency: {
    avg_ms: number;
    p50_ms: number;
    p95_ms: number;
    p99_ms: number;
  };
  queue: {
    depth: number;
    total_requests: number;
  };
};

// Mirrors the point shape returned by GET /api/v1/metrics/history -- server-side
// samples taken once a minute and persisted, not client-accumulated snapshots.
type HistoryPoint = {
  ts: number; // seconds since epoch
  memory_rss_mb: number;
  memory_percent: number;
  cpu_process_percent: number;
  cpu_system_percent: number;
  avg_latency_ms: number;
  p99_latency_ms: number;
  queue_depth: number;
};

type HistoryResponse = {
  range: TimeRange;
  points: HistoryPoint[];
  sample_interval_seconds: number;
  requested_window_seconds: number;
  earliest_available_ts: number | null;
  history_available_seconds: number;
};

type TimeRange = '1h' | '24h' | '7d';

const RANGE_LABELS: Record<TimeRange, string> = {
  '1h': 'Last Hour',
  '24h': 'Last 24 Hours',
  '7d': 'Last 7 Days',
};

// How often to re-fetch the server-side history series while auto-refresh is
// on. The backend only samples once a minute, so there's no point polling
// history faster than that -- the 5s /api/metrics poll still animates the
// "current" tiles and the live head of the chart in between.
const HISTORY_REFRESH_MS = 60000;

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function formatPointTime(ts: number, range: TimeRange): string {
  const date = new Date(ts * 1000);
  return range === '1h' ? date.toLocaleTimeString() : date.toLocaleString();
}

export default function ResourceMetrics() {
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchMetrics = async () => {
    try {
      const res = await fetch('/api/metrics');
      if (!res.ok) {
        throw new Error(`Failed to fetch metrics: HTTP ${res.status}`);
      }
      const data = await res.json();
      setMetrics(data);
      setError(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const fetchHistory = async (range: TimeRange) => {
    try {
      const res = await fetch(`/api/v1/metrics/history?range=${range}`);
      if (!res.ok) {
        throw new Error(`Failed to fetch metrics history: HTTP ${res.status}`);
      }
      const data: HistoryResponse = await res.json();
      setHistory(data);
      setHistoryError(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setHistoryError(msg);
    }
  };

  useEffect(() => {
    fetchMetrics();

    if (autoRefresh) {
      intervalRef.current = setInterval(fetchMetrics, 5000); // Refresh every 5 seconds
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [autoRefresh]);

  useEffect(() => {
    fetchHistory(timeRange);

    let historyIntervalId: ReturnType<typeof setInterval> | null = null;
    if (autoRefresh) {
      historyIntervalId = setInterval(() => fetchHistory(timeRange), HISTORY_REFRESH_MS);
    }

    return () => {
      if (historyIntervalId) {
        clearInterval(historyIntervalId);
      }
    };
  }, [timeRange, autoRefresh]);

  // The displayed series is the persisted server-side history for the
  // selected window, with the live 5s snapshot appended as a transient point
  // so the chart's head keeps animating between minute-cadence history
  // refreshes. This point is never itself persisted.
  const displayedPoints: HistoryPoint[] = history ? [...history.points] : [];
  if (autoRefresh && metrics) {
    displayedPoints.push({
      ts: Date.now() / 1000,
      memory_rss_mb: metrics.memory.rss_mb,
      memory_percent: metrics.memory.percent,
      cpu_process_percent: metrics.cpu.process_percent,
      cpu_system_percent: metrics.cpu.system_percent,
      avg_latency_ms: metrics.latency.avg_ms,
      p99_latency_ms: metrics.latency.p99_ms,
      queue_depth: metrics.queue.depth,
    });
  }

  const exportData = (metrics: MetricsData, format: 'csv' | 'json') => {
    if (format === 'json') {
      const data = {
        current: metrics,
        range: timeRange,
        history_available_seconds: history?.history_available_seconds ?? 0,
        historical: displayedPoints,
        exported_at: new Date().toISOString(),
      };
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `metrics-${timeRange}-${Date.now()}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } else {
      // CSV format
      let csv =
        'timestamp,memory_rss_mb,memory_percent,cpu_process,cpu_system,avg_latency_ms,p99_latency_ms,queue_depth\n';
      displayedPoints.forEach((d) => {
        csv += `${new Date(d.ts * 1000).toISOString()},${d.memory_rss_mb},${d.memory_percent},${d.cpu_process_percent},${d.cpu_system_percent},${d.avg_latency_ms},${d.p99_latency_ms},${d.queue_depth}\n`;
      });
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `metrics-${timeRange}-${Date.now()}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    }
  };

  if (loading && !metrics) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <LoadingSpinner />
      </div>
    );
  }

  if (error) {
    return <ErrorMessage message={error} />;
  }

  if (!metrics) {
    return <ErrorMessage message="No metrics data available" />;
  }

  const getWarningLevel = (
    metric: 'memory_percent' | 'cpu_process' | 'cpu_system' | 'latency_p99',
    value: number
  ): 'normal' | 'warning' | 'critical' => {
    switch (metric) {
      case 'memory_percent':
        return value > 90 ? 'critical' : value > 75 ? 'warning' : 'normal';
      case 'cpu_process':
        return value > 80 ? 'critical' : value > 60 ? 'warning' : 'normal';
      case 'cpu_system':
        return value > 90 ? 'critical' : value > 75 ? 'warning' : 'normal';
      case 'latency_p99':
        return value > 1000 ? 'critical' : value > 500 ? 'warning' : 'normal';
    }
  };

  const getWarningColor = (level: 'normal' | 'warning' | 'critical'): string => {
    switch (level) {
      case 'critical':
        return '#ef4444';
      case 'warning':
        return '#f59e0b';
      default:
        return '#10b981';
    }
  };

  const buttonStyle = (active: boolean): CSSProperties => ({
    background: active ? 'var(--button)' : 'transparent',
    color: active ? 'var(--button-text)' : 'var(--text)',
    border: '1px solid var(--border)',
    borderRadius: 6,
    padding: '8px 16px',
    cursor: 'pointer',
    fontSize: 14,
  });

  return (
    <div>
      {/* Controls */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 24,
          padding: 16,
          background: 'var(--card-bg)',
          borderRadius: 8,
          border: '1px solid var(--border)',
        }}
      >
        <div style={{ display: 'flex', gap: 8 }}>
          {(Object.keys(RANGE_LABELS) as TimeRange[]).map((range) => (
            <button key={range} onClick={() => setTimeRange(range)} style={buttonStyle(timeRange === range)}>
              {RANGE_LABELS[range]}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              style={{ cursor: 'pointer' }}
            />
            <span style={{ fontSize: 14 }}>Auto-refresh</span>
          </label>

          <button
            onClick={() => exportData(metrics, 'csv')}
            style={{
              background: 'var(--button)',
              color: 'var(--button-text)',
              border: 'none',
              borderRadius: 6,
              padding: '8px 16px',
              cursor: 'pointer',
              fontSize: 14,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--button)')}
          >
            Export CSV
          </button>

          <button
            onClick={() => exportData(metrics, 'json')}
            style={{
              background: 'var(--button)',
              color: 'var(--button-text)',
              border: 'none',
              borderRadius: 6,
              padding: '8px 16px',
              cursor: 'pointer',
              fontSize: 14,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--button)')}
          >
            Export JSON
          </button>
        </div>
      </div>

      {/* Metrics Grid */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
          gap: 24,
          marginBottom: 24,
        }}
      >
        {/* Memory Usage */}
        <div
          style={{
            padding: 20,
            background: 'var(--card-bg)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}
        >
          <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>Memory Usage</h3>
          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Process (RSS)</span>
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: getWarningColor(getWarningLevel('memory_percent', metrics.memory.percent)),
                }}
              >
                {metrics.memory.rss_mb.toFixed(1)} MB
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Virtual (VMS)</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.memory.vms_mb.toFixed(1)} MB</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Memory %</span>
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: getWarningColor(getWarningLevel('memory_percent', metrics.memory.percent)),
                }}
              >
                {metrics.memory.percent.toFixed(1)}%
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Available</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.memory.available_mb.toFixed(1)} MB</span>
            </div>
          </div>
        </div>

        {/* CPU Utilization */}
        <div
          style={{
            padding: 20,
            background: 'var(--card-bg)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}
        >
          <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>CPU Utilization</h3>
          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Process</span>
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: getWarningColor(getWarningLevel('cpu_process', metrics.cpu.process_percent)),
                }}
              >
                {metrics.cpu.process_percent.toFixed(1)}%
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>System</span>
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: getWarningColor(getWarningLevel('cpu_system', metrics.cpu.system_percent)),
                }}
              >
                {metrics.cpu.system_percent.toFixed(1)}%
              </span>
            </div>
          </div>
        </div>

        {/* Request Latency */}
        <div
          style={{
            padding: 20,
            background: 'var(--card-bg)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}
        >
          <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>Request Latency</h3>
          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Average</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.latency.avg_ms.toFixed(1)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>P50 (Median)</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.latency.p50_ms.toFixed(1)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>P95</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.latency.p95_ms.toFixed(1)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>P99</span>
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: getWarningColor(getWarningLevel('latency_p99', metrics.latency.p99_ms)),
                }}
              >
                {metrics.latency.p99_ms.toFixed(1)} ms
              </span>
            </div>
          </div>
        </div>

        {/* Queue Depth */}
        <div
          style={{
            padding: 20,
            background: 'var(--card-bg)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}
        >
          <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>Queue Status</h3>
          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Current Depth</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.queue.depth}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>Total Requests</span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{metrics.queue.total_requests}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Historical Charts */}
      <div
        style={{
          padding: 20,
          background: 'var(--card-bg)',
          borderRadius: 8,
          border: '1px solid var(--border)',
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            marginBottom: 16,
            flexWrap: 'wrap',
            gap: 8,
          }}
        >
          <h3 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>
            Historical Trends -- {RANGE_LABELS[timeRange]}
          </h3>
          {history && (
            <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              {history.history_available_seconds < history.requested_window_seconds
                ? `Only ${formatDuration(history.history_available_seconds)} of history available (server-side sampling just started collecting data)`
                : `Showing the full ${formatDuration(history.requested_window_seconds)} window`}
              , sampled every {formatDuration(history.sample_interval_seconds)}
            </span>
          )}
        </div>

        {historyError && (
          <div style={{ marginBottom: 16 }}>
            <ErrorMessage message={`Failed to load history: ${historyError}`} />
          </div>
        )}

        {displayedPoints.length === 0 ? (
          <div style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
            No history yet for this window. Server-side sampling records a new point once a minute --
            check back shortly.
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
            <div>
              <h4 style={{ fontSize: 14, color: 'var(--text-secondary)', marginBottom: 8 }}>Memory Trend</h4>
              <div style={{ fontSize: 12, fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
                {displayedPoints.slice(-20).map((d, i) => (
                  <div key={i}>
                    {formatPointTime(d.ts, timeRange)}: {d.memory_rss_mb.toFixed(1)} MB ({d.memory_percent.toFixed(1)}
                    %)
                  </div>
                ))}
              </div>
            </div>

            <div>
              <h4 style={{ fontSize: 14, color: 'var(--text-secondary)', marginBottom: 8 }}>CPU Trend</h4>
              <div style={{ fontSize: 12, fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
                {displayedPoints.slice(-20).map((d, i) => (
                  <div key={i}>
                    {formatPointTime(d.ts, timeRange)}: Process {d.cpu_process_percent.toFixed(1)}% / System{' '}
                    {d.cpu_system_percent.toFixed(1)}%
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

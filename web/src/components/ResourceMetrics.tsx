'use client';

import { useEffect, useState } from 'react';
import LoadingSpinner from './LoadingSpinner';
import ErrorMessage from './ErrorMessage';

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

type TimeRange = '1h' | '24h' | '7d';

export default function ResourceMetrics() {
  const [metrics, setMetrics] = useState<MetricsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [timeRange, setTimeRange] = useState<TimeRange>('1h');

  async function loadMetrics() {
    setLoading(true);
    try {
      const res = await fetch('/api/v1/metrics', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to fetch metrics: HTTP ${res.status}`);
      const data = await res.json();
      setMetrics(data);
      setError(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadMetrics();
  }, []);

  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      loadMetrics();
    }, 5000); // Refresh every 5 seconds

    return () => clearInterval(interval);
  }, [autoRefresh]);

  function exportMetrics(metrics: MetricsData, format: 'json' | 'csv') {
    let content: string;
    let filename: string;
    let mimeType: string;

    if (format === 'json') {
      content = JSON.stringify(metrics, null, 2);
      filename = `nyxgpt-metrics-${new Date().toISOString()}.json`;
      mimeType = 'application/json';
    } else {
      // CSV format
      const rows = [
        ['Metric Category', 'Metric', 'Value'],
        ['Memory', 'RSS (MB)', metrics.memory.rss_mb.toFixed(2)],
        ['Memory', 'VMS (MB)', metrics.memory.vms_mb.toFixed(2)],
        ['Memory', 'Percent', metrics.memory.percent.toFixed(2)],
        ['Memory', 'Available (MB)', metrics.memory.available_mb.toFixed(2)],
        ['CPU', 'Process Percent', metrics.cpu.process_percent.toFixed(2)],
        ['CPU', 'System Percent', metrics.cpu.system_percent.toFixed(2)],
        ['Latency', 'Average (ms)', metrics.latency.avg_ms.toFixed(2)],
        ['Latency', 'P50 (ms)', metrics.latency.p50_ms.toFixed(2)],
        ['Latency', 'P95 (ms)', metrics.latency.p95_ms.toFixed(2)],
        ['Latency', 'P99 (ms)', metrics.latency.p99_ms.toFixed(2)],
        ['Queue', 'Depth', metrics.queue.depth.toString()],
        ['Queue', 'Total Requests', metrics.queue.total_requests.toString()],
      ];
      content = rows.map((row) => row.join(',')).join('\n');
      filename = `nyxgpt-metrics-${new Date().toISOString()}.csv`;
      mimeType = 'text/csv';
    }

    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function getMemoryUsageLevel(percent: number): 'normal' | 'warning' | 'critical' {
    if (percent > 80) return 'critical';
    if (percent > 60) return 'warning';
    return 'normal';
  }

  function getCpuUsageLevel(percent: number): 'normal' | 'warning' | 'critical' {
    if (percent > 80) return 'critical';
    if (percent > 60) return 'warning';
    return 'normal';
  }

  function getLatencyLevel(p95_ms: number): 'normal' | 'warning' | 'critical' {
    if (p95_ms > 1000) return 'critical';
    if (p95_ms > 500) return 'warning';
    return 'normal';
  }

  function getLevelColor(level: 'normal' | 'warning' | 'critical'): string {
    switch (level) {
      case 'critical':
        return '#ff4444';
      case 'warning':
        return '#ffaa00';
      default:
        return '#44ff44';
    }
  }

  function getLevelBgColor(level: 'normal' | 'warning' | 'critical'): string {
    switch (level) {
      case 'critical':
        return 'rgba(255, 68, 68, 0.1)';
      case 'warning':
        return 'rgba(255, 170, 0, 0.1)';
      default:
        return 'rgba(68, 255, 68, 0.1)';
    }
  }

  if (loading && !metrics) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 200 }}>
        <LoadingSpinner size="large" label="Loading metrics..." />
      </div>
    );
  }

  if (error && !metrics) {
    return (
      <ErrorMessage
        title="Failed to load resource metrics"
        message={error}
        onRetry={loadMetrics}
        retrying={loading}
      />
    );
  }

  if (!metrics) return null;

  const memoryLevel = getMemoryUsageLevel(metrics.memory.percent);
  const cpuLevel = getCpuUsageLevel(metrics.cpu.process_percent);
  const latencyLevel = getLatencyLevel(metrics.latency.p95_ms);

  return (
    <div style={{ width: '100%' }}>
      {/* Header with controls */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '1.5rem',
          flexWrap: 'wrap',
          gap: '1rem',
        }}
      >
        <div>
          <h2 style={{ margin: 0, fontSize: '1.2rem' }}>Resource Usage</h2>
          <p style={{ margin: '4px 0 0 0', fontSize: 14, color: '#666' }}>
            Real-time system metrics and performance monitoring
          </p>
        </div>

        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
          <label
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 14,
              padding: '6px 12px',
              border: '1px solid var(--border)',
              borderRadius: 6,
              background: 'var(--background)',
              cursor: 'pointer',
            }}
          >
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              style={{ cursor: 'pointer' }}
            />
            Auto-refresh (5s)
          </label>

          <button
            onClick={() => exportMetrics(metrics, 'json')}
            style={{
              padding: '6px 12px',
              fontSize: 14,
              background: 'var(--background)',
              color: 'var(--foreground)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              cursor: 'pointer',
            }}
          >
            Export JSON
          </button>

          <button
            onClick={() => exportMetrics(metrics, 'csv')}
            style={{
              padding: '6px 12px',
              fontSize: 14,
              background: 'var(--background)',
              color: 'var(--foreground)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              cursor: 'pointer',
            }}
          >
            Export CSV
          </button>

          <button
            onClick={loadMetrics}
            disabled={loading}
            style={{
              padding: '6px 12px',
              fontSize: 14,
              background: '#0066cc',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor: loading ? 'not-allowed' : 'pointer',
              opacity: loading ? 0.6 : 1,
            }}
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Metrics Grid */}
      <div style={{ display: 'grid', gap: '1rem', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
        {/* Memory Metrics */}
        <div
          style={{
            padding: '1.5rem',
            border: '1px solid var(--border)',
            borderRadius: 8,
            background: getLevelBgColor(memoryLevel),
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
            <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600 }}>Memory Usage</h3>
            <div
              style={{
                width: 12,
                height: 12,
                borderRadius: '50%',
                background: getLevelColor(memoryLevel),
              }}
              title={`Status: ${memoryLevel}`}
            />
          </div>

          <div style={{ marginBottom: '0.75rem' }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: getLevelColor(memoryLevel) }}>
              {metrics.memory.percent.toFixed(1)}%
            </div>
            <div style={{ fontSize: 12, color: '#666' }}>Process memory usage</div>
          </div>

          <div style={{ display: 'grid', gap: '0.5rem', fontSize: 13 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>RSS:</span>
              <span style={{ fontWeight: 600 }}>{metrics.memory.rss_mb.toFixed(1)} MB</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>VMS:</span>
              <span style={{ fontWeight: 600 }}>{metrics.memory.vms_mb.toFixed(1)} MB</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>Available:</span>
              <span style={{ fontWeight: 600 }}>{metrics.memory.available_mb.toFixed(0)} MB</span>
            </div>
          </div>
        </div>

        {/* CPU Metrics */}
        <div
          style={{
            padding: '1.5rem',
            border: '1px solid var(--border)',
            borderRadius: 8,
            background: getLevelBgColor(cpuLevel),
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
            <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600 }}>CPU Usage</h3>
            <div
              style={{
                width: 12,
                height: 12,
                borderRadius: '50%',
                background: getLevelColor(cpuLevel),
              }}
              title={`Status: ${cpuLevel}`}
            />
          </div>

          <div style={{ marginBottom: '0.75rem' }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: getLevelColor(cpuLevel) }}>
              {metrics.cpu.process_percent.toFixed(1)}%
            </div>
            <div style={{ fontSize: 12, color: '#666' }}>Process CPU usage</div>
          </div>

          <div style={{ display: 'grid', gap: '0.5rem', fontSize: 13 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>System CPU:</span>
              <span style={{ fontWeight: 600 }}>{metrics.cpu.system_percent.toFixed(1)}%</span>
            </div>
          </div>
        </div>

        {/* Latency Metrics */}
        <div
          style={{
            padding: '1.5rem',
            border: '1px solid var(--border)',
            borderRadius: 8,
            background: getLevelBgColor(latencyLevel),
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
            <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600 }}>Request Latency</h3>
            <div
              style={{
                width: 12,
                height: 12,
                borderRadius: '50%',
                background: getLevelColor(latencyLevel),
              }}
              title={`Status: ${latencyLevel}`}
            />
          </div>

          <div style={{ marginBottom: '0.75rem' }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: getLevelColor(latencyLevel) }}>
              {metrics.latency.p95_ms.toFixed(1)}ms
            </div>
            <div style={{ fontSize: 12, color: '#666' }}>P95 latency</div>
          </div>

          <div style={{ display: 'grid', gap: '0.5rem', fontSize: 13 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>Average:</span>
              <span style={{ fontWeight: 600 }}>{metrics.latency.avg_ms.toFixed(1)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>P50:</span>
              <span style={{ fontWeight: 600 }}>{metrics.latency.p50_ms.toFixed(1)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>P99:</span>
              <span style={{ fontWeight: 600 }}>{metrics.latency.p99_ms.toFixed(1)} ms</span>
            </div>
          </div>
        </div>

        {/* Queue Metrics */}
        <div
          style={{
            padding: '1.5rem',
            border: '1px solid var(--border)',
            borderRadius: 8,
            background: 'var(--background)',
          }}
        >
          <div style={{ marginBottom: '1rem' }}>
            <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600 }}>Queue Status</h3>
          </div>

          <div style={{ marginBottom: '0.75rem' }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: 'var(--foreground)' }}>
              {metrics.queue.depth}
            </div>
            <div style={{ fontSize: 12, color: '#666' }}>Current queue depth</div>
          </div>

          <div style={{ display: 'grid', gap: '0.5rem', fontSize: 13 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: '#666' }}>Total requests:</span>
              <span style={{ fontWeight: 600 }}>{metrics.queue.total_requests.toLocaleString()}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Observability dashboards live under Admin -> Observability */}
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
        <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Observability</div>
        <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
          Local-only Grafana, Prometheus, Jaeger, and GlitchTip dashboards -- including the
          Prometheus scrape endpoint for this API&apos;s <code>/metrics</code> -- have moved to
          their own page.
        </p>
        <a href="/admin/observability" style={{ color: '#0066cc', fontSize: 13 }}>
          Open Observability →
        </a>
      </div>

      {/* Threshold indicators */}
      <div
        style={{
          marginTop: '1rem',
          padding: '1rem',
          border: '1px solid var(--border)',
          borderRadius: 6,
          background: 'var(--background)',
          fontSize: 12,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Status Indicators:</div>
        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#44ff44' }} />
            <span style={{ color: '#666' }}>Normal (&lt; 60%)</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#ffaa00' }} />
            <span style={{ color: '#666' }}>Warning (60-80%)</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#ff4444' }} />
            <span style={{ color: '#666' }}>Critical (&gt; 80%)</span>
          </div>
        </div>
      </div>
    </div>
  );
}

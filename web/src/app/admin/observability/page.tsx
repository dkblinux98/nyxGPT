'use client';

import GrafanaPanel from '../../../components/GrafanaPanel';
import DashboardCatalog from '../../../components/DashboardCatalog';
import LogAggregationPanel from '../../../components/LogAggregationPanel';
import TracingPanel from '../../../components/TracingPanel';
import ErrorTrackingPanel from '../../../components/ErrorTrackingPanel';

export default function ObservabilityPage() {
  return (
    <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>SRE Overview</h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          The single entry point for every SRE tool: local-only Grafana dashboards (app
          functionality and what&apos;s being self-healed), Loki log queries, Jaeger traces, and
          GlitchTip error tracking -- nothing here is ever sent to an external/cloud service.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      <div
        style={{
          padding: '1rem',
          border: '1px solid var(--border)',
          borderRadius: 6,
          background: 'var(--background)',
          fontSize: 13,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Prometheus Endpoint</div>
        <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
          The nyxGPT API exposes a <code>/metrics</code> endpoint (same host/port as the rest of
          the API, unauthenticated like <code>/health</code>) for Prometheus to scrape request
          counts, latency histograms, error rates, and business metrics (chat/RAG usage). Point
          your Prometheus server&apos;s scrape config at:
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <code
            style={{
              background: 'var(--code-bg)',
              padding: '4px 8px',
              borderRadius: 4,
              fontSize: 12,
            }}
          >
            &lt;nyxgpt-api-host&gt;/metrics
          </code>
          <a
            href="/api/prometheus-metrics"
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: '#0066cc', fontSize: 13 }}
          >
            View current metrics ↗
          </a>
        </div>
      </div>

      {/* Monitoring dashboards (Grafana + Prometheus) */}
      <GrafanaPanel />

      {/* Direct links into every provisioned-as-code Grafana dashboard */}
      <DashboardCatalog />

      {/* Log aggregation (Loki + promtail) */}
      <LogAggregationPanel />

      {/* Distributed tracing (Jaeger) */}
      <TracingPanel />

      {/* Error tracking (GlitchTip) */}
      <ErrorTrackingPanel />
    </main>
  );
}

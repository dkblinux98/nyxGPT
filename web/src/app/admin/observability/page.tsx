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

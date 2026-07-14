'use client';

export default function GrafanaPanel() {
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
      <div style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Monitoring Dashboards</div>
      <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
        System overview, RAG performance, and API metrics dashboards are pre-provisioned in a{' '}
        <strong>local-only</strong> Grafana instance, backed by a Prometheus server that scrapes
        the <code>/metrics</code> endpoint above -- nothing is sent to an external/cloud
        monitoring service.
      </p>
      <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
        To enable: start the local stack with{' '}
        <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
          docker compose --profile monitoring up
        </code>
        .
      </p>
      <a
        href="http://localhost:3001"
        target="_blank"
        rel="noopener noreferrer"
        style={{ color: '#0066cc', fontSize: 13 }}
      >
        Open Grafana ↗
      </a>
    </div>
  );
}

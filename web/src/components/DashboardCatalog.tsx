'use client';

import { useEffect, useState } from 'react';

type MonitoringStatus = {
  enabled: boolean;
  active: boolean;
  grafana_ui_url: string;
  prometheus_ui_url: string;
};

type DashboardEntry = {
  uid: string;
  label: string;
};

type DashboardGroup = {
  title: string;
  dashboards: DashboardEntry[];
};

// Mirrors docker/grafana/dashboards/*.json -- keep uids/labels in sync with
// the "uid"/"title" fields provisioned there.
const DASHBOARD_GROUPS: DashboardGroup[] = [
  {
    title: 'App functionality',
    dashboards: [
      { uid: 'nyxgpt-system-overview', label: 'System Overview' },
      { uid: 'nyxgpt-api-metrics', label: 'API Metrics' },
      { uid: 'nyxgpt-rag-performance', label: 'RAG Performance' },
      { uid: 'nyxgpt-resource-usage', label: 'Resource Usage' },
    ],
  },
  {
    title: 'Self-healing & deployment',
    dashboards: [
      { uid: 'nyxgpt-self-healing', label: 'Self-Healing' },
      { uid: 'nyxgpt-deployment', label: 'Blue-Green Deployment' },
      { uid: 'nyxgpt-canary', label: 'Canary Rollout' },
    ],
  },
  {
    title: 'Logs',
    dashboards: [
      { uid: 'nyxgpt-logs-explorer', label: 'Logs Explorer' },
      { uid: 'nyxgpt-operational-logs', label: 'Operational Logs' },
    ],
  },
];

export default function DashboardCatalog() {
  const [status, setStatus] = useState<MonitoringStatus | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadStatus() {
      try {
        const res = await fetch('/api/v1/monitoring', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setStatus(data);
      } catch {
        // GrafanaPanel above already surfaces monitoring status errors.
      }
    }

    loadStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!status || !status.active) return null;

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
      <div style={{ fontWeight: 600, marginBottom: '0.75rem' }}>Dashboard Catalog</div>
      <p style={{ margin: '0 0 0.75rem 0', color: '#666' }}>
        Every dashboard below is provisioned as code (no manual import) and reachable directly in
        Grafana:
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        {DASHBOARD_GROUPS.map((group) => (
          <div key={group.title}>
            <div style={{ fontWeight: 600, marginBottom: 4, fontSize: 12, color: '#666' }}>
              {group.title}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {group.dashboards.map((dashboard) => (
                <a
                  key={dashboard.uid}
                  href={`${status.grafana_ui_url}/d/${dashboard.uid}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    padding: '4px 10px',
                    borderRadius: 4,
                    border: '1px solid var(--border)',
                    background: 'var(--code-bg)',
                    color: '#0066cc',
                    fontSize: 12,
                    textDecoration: 'none',
                  }}
                >
                  {dashboard.label} ↗
                </a>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

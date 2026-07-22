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
  description: string;
};

type DashboardGroup = {
  title: string;
  dashboards: DashboardEntry[];
};

// Mirrors docker/grafana/dashboards/*.json -- keep uids/labels in sync with
// the "uid"/"title" fields provisioned there. Rendered as the same tile grid
// as the admin dashboard's quick-nav (ADMIN_NAV in admin/dashboard/page.tsx):
// visible one-line description, echoed as a hover tooltip, no arrow
// decoration, same-tab navigation.
export const DASHBOARD_GROUPS: DashboardGroup[] = [
  {
    title: 'App functionality',
    dashboards: [
      {
        uid: 'nyxgpt-system-overview',
        label: 'System Overview',
        description: 'Request rates, errors, and latency at a glance',
      },
      {
        uid: 'nyxgpt-api-metrics',
        label: 'API Metrics',
        description: 'Endpoint-level traffic and latency detail',
      },
      {
        uid: 'nyxgpt-rag-performance',
        label: 'RAG Performance',
        description: 'Retrieval and ingest pipeline metrics',
      },
      {
        uid: 'nyxgpt-resource-usage',
        label: 'Resource Usage',
        description: 'Process memory, CPU, and queue depth',
      },
    ],
  },
  {
    title: 'Self-healing & deployment',
    dashboards: [
      {
        uid: 'nyxgpt-self-healing',
        label: 'Self-Healing',
        description: 'Watchdog restarts and recovery events',
      },
      {
        uid: 'nyxgpt-deployment',
        label: 'Blue-Green Deployment',
        description: 'Deploy switches and rollbacks',
      },
      {
        uid: 'nyxgpt-canary',
        label: 'Canary Rollout',
        description: 'Rollout progress, evaluation, and promotion',
      },
    ],
  },
  {
    title: 'Logs',
    dashboards: [
      {
        uid: 'nyxgpt-logs-explorer',
        label: 'Logs Explorer',
        description: 'Free search across all shipped logs',
      },
      {
        uid: 'nyxgpt-operational-logs',
        label: 'Operational Logs',
        description: 'Self-heal, deploy, and canary log panels',
      },
    ],
  },
];

const tileStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  padding: '10px 12px',
  borderRadius: 8,
  border: '1px solid var(--border)',
  background: 'var(--background)',
  textDecoration: 'none',
  transition: 'border-color 0.15s ease, background 0.15s ease',
};

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
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                gap: 8,
              }}
            >
              {group.dashboards.map((dashboard) => (
                <a
                  key={dashboard.uid}
                  href={`${status.grafana_ui_url}/d/${dashboard.uid}`}
                  title={dashboard.description}
                  style={tileStyle}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = 'var(--link)';
                    e.currentTarget.style.background = 'var(--muted)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = 'var(--border)';
                    e.currentTarget.style.background = 'var(--background)';
                  }}
                >
                  <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--foreground)' }}>
                    {dashboard.label}
                  </span>
                  <span style={{ fontSize: 12, color: 'var(--muted-foreground)' }}>
                    {dashboard.description}
                  </span>
                </a>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

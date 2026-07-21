import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import ObservabilityPage from '../../../src/app/admin/observability/page';

const mockMonitoringActive = {
  enabled: true,
  active: true,
  grafana_ui_url: 'http://localhost:3001',
  prometheus_ui_url: 'http://localhost:9090',
};

const mockMonitoringDisabled = {
  enabled: false,
  active: false,
  grafana_ui_url: 'http://localhost:3001',
  prometheus_ui_url: 'http://localhost:9090',
};

const mockTracingDisabled = {
  enabled: false,
  active: false,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318/v1/traces',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [],
};

const mockTracingActive = {
  enabled: true,
  active: true,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318/v1/traces',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [
    {
      label: 'Chat requests',
      hint: 'Filter by operation: POST /api/v1/chat',
      url: 'http://localhost:16686/search?service=nyxgpt-api&lookback=1h',
    },
    {
      label: 'RAG query',
      hint: 'Filter by operation: POST /api/v1/rag/query',
      url: 'http://localhost:16686/search?service=nyxgpt-api&lookback=1h',
    },
  ],
};

const mockErrorTrackingDisabled = {
  enabled: false,
  active: false,
  dsn: '',
  environment: 'development',
  glitchtip_ui_url: 'http://localhost:8080',
};

const mockLogAggregationDisabled = {
  enabled: false,
  active: false,
  grafana_explore_url: 'http://localhost:3001/explore',
};

function mockAllDisabled() {
  server.use(
    http.get('/api/v1/monitoring', () => HttpResponse.json(mockMonitoringDisabled)),
    http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
    http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled)),
    http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
  );
}

describe('ObservabilityPage', () => {
  it('renders the heading, subtitle, and back link', async () => {
    mockAllDisabled();
    render(<ObservabilityPage />);

    expect(screen.getByRole('heading', { name: 'SRE Overview' })).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('does not render Prometheus scrape-config plumbing', async () => {
    mockAllDisabled();
    render(<ObservabilityPage />);

    // The stack is provisioned by nyxgpt ops; operators never point a
    // Prometheus scrape config anywhere, and the raw /metrics text dump is
    // not an operator surface. Grafana dashboards are the metrics UI.
    expect(screen.queryByText('Prometheus Endpoint')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View current metrics/i })).not.toBeInTheDocument();
  });

  it('renders Grafana and Prometheus links from the Monitoring Dashboards card when active', async () => {
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.json(mockMonitoringActive)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled)),
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );

    render(<ObservabilityPage />);

    const grafanaLink = await screen.findByRole('link', { name: /Open Grafana/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001');

    const prometheusLink = screen.getByRole('link', { name: /Open Prometheus/i });
    expect(prometheusLink).toHaveAttribute('href', 'http://localhost:9090');
  });

  it('renders the Dashboard Catalog with links into every provisioned dashboard when monitoring is active', async () => {
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.json(mockMonitoringActive)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled)),
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );

    render(<ObservabilityPage />);

    const resourceUsageLink = await screen.findByRole('link', { name: /Resource Usage/i });
    expect(resourceUsageLink).toHaveAttribute(
      'href',
      'http://localhost:3001/d/nyxgpt-resource-usage'
    );
    const selfHealLink = screen.getByRole('link', { name: /Self-Healing/i });
    expect(selfHealLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-self-healing');
    const operationalLogsLink = screen.getByRole('link', { name: /Operational Logs/i });
    expect(operationalLogsLink).toHaveAttribute(
      'href',
      'http://localhost:3001/d/nyxgpt-operational-logs'
    );
  });

  it('renders the Distributed Tracing, Error Tracking, and Log Aggregation cards', async () => {
    mockAllDisabled();
    render(<ObservabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Distributed Tracing')).toBeInTheDocument();
    });
    expect(screen.getByText('Error Tracking')).toBeInTheDocument();
    expect(screen.getByText('Log Aggregation')).toBeInTheDocument();
  });

  it('renders curated Jaeger trace views when tracing is active', async () => {
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.json(mockMonitoringDisabled)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingActive)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled)),
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );

    render(<ObservabilityPage />);

    const chatLink = await screen.findByRole('link', { name: /Chat requests/i });
    expect(chatLink).toHaveAttribute(
      'href',
      'http://localhost:16686/search?service=nyxgpt-api&lookback=1h'
    );
    expect(screen.getByText(/Filter by operation: POST \/api\/v1\/chat/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /RAG query/i })).toBeInTheDocument();
  });
});

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

    expect(screen.getByRole('heading', { name: 'Observability' })).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('renders the Prometheus scrape endpoint card', async () => {
    mockAllDisabled();
    render(<ObservabilityPage />);

    expect(screen.getByText('Prometheus Endpoint')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /View current metrics/i });
    expect(link).toHaveAttribute('href', '/api/prometheus-metrics');
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

  it('renders the Distributed Tracing, Error Tracking, and Log Aggregation cards', async () => {
    mockAllDisabled();
    render(<ObservabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Distributed Tracing')).toBeInTheDocument();
    });
    expect(screen.getByText('Error Tracking')).toBeInTheDocument();
    expect(screen.getByText('Log Aggregation')).toBeInTheDocument();
  });
});

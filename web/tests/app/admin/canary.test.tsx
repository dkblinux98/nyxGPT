import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CanaryPage from '../../../src/app/admin/canary/page';

const mockStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { healthy: true, message: 'ok' },
  canary: { healthy: true, message: 'ok' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
  history: [],
  available: true,
  unavailable_reason: null,
};

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

const mockLogAggregationActive = {
  enabled: true,
  active: true,
  grafana_explore_url: 'http://localhost:3001/explore',
};

const mockLogAggregationDisabled = {
  enabled: false,
  active: false,
  grafana_explore_url: 'http://localhost:3001/explore',
};

function mockObservability(monitoring: object, logAggregation: object) {
  server.use(
    http.get('/api/v1/monitoring', () => HttpResponse.json(monitoring)),
    http.get('/api/v1/log-aggregation', () => HttpResponse.json(logAggregation))
  );
}

describe('CanaryPage', () => {
  it('renders the standardized back-nav link instead of a Back to Chat button', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
    expect(screen.queryByRole('button', { name: /back to chat/i })).not.toBeInTheDocument();
  });

  it('does not show observability links when monitoring and log aggregation are inactive', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Canary Rollout dashboard/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Canary events/i)).not.toBeInTheDocument();
  });

  it('links to the Canary Grafana dashboard and the Loki saved query when active', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringActive, mockLogAggregationActive);

    render(<CanaryPage />);

    const grafanaLink = await screen.findByRole('link', { name: /Canary Rollout dashboard/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-canary');

    const lokiLink = screen.getByRole('link', { name: /Canary events/i });
    expect(lokiLink).toHaveAttribute('href', 'http://localhost:3001/explore');

    expect(screen.getByText(/canary:/)).toBeInTheDocument();
  });
});

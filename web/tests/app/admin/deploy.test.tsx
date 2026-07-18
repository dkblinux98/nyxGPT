import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse, delay } from 'msw';
import { server } from '../../mocks/server';
import DeployPage from '../../../src/app/admin/deploy/page';

const mockStatus = {
  namespace: 'nyxgpt',
  active: 'blue',
  inactive: 'green',
  colors: {
    blue: { healthy: true, message: 'ok' },
    green: { healthy: true, message: 'ok' },
  },
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

describe('DeployPage', () => {
  it('renders the standardized back-nav link instead of a Back to Chat button', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Blue/Green Deployment' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
    expect(screen.queryByRole('button', { name: /back to chat/i })).not.toBeInTheDocument();
  });

  it('does not show observability links when monitoring and log aggregation are inactive', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Blue/Green Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Blue\/Green Deployment dashboard/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Deploy events/i)).not.toBeInTheDocument();
  });

  it('links to the Deployment Grafana dashboard and the Loki saved query when active', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringActive, mockLogAggregationActive);

    render(<DeployPage />);

    const grafanaLink = await screen.findByRole('link', {
      name: /Blue\/Green Deployment dashboard/i,
    });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-deployment');

    const lokiLink = screen.getByRole('link', { name: /Deploy events/i });
    expect(lokiLink).toHaveAttribute('href', 'http://localhost:3001/explore');

    expect(screen.getByText(/deploy:/)).toBeInTheDocument();
  });
});

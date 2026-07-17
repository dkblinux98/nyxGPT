import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SelfHealPage from '../../../src/app/admin/self-heal/page';

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
  }),
}));

const mockStatus = {
  enabled: false,
  components: [
    { service: 'api', container: 'nyxgpt-api-1', state: 'running', health: 'healthy', healthy: true },
    { service: 'web', container: 'nyxgpt-web-1', state: 'exited', health: '', healthy: false },
  ],
  unhealthy_count: 1,
  events: [
    {
      ts: 1768300800,
      service: 'web',
      reason: 'state=exited health=n/a',
      action: 'restart',
      ok: true,
      restart_count: 1,
      message: 'Restarted web',
    },
  ],
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

describe('SelfHealPage', () => {
  it('renders component health and recent events', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByText('api')).toBeInTheDocument();
    });
    expect(screen.getByText('web')).toBeInTheDocument();
    expect(screen.getByText('1 unhealthy')).toBeInTheDocument();
    expect(screen.getByText(/Restarted web/)).toBeInTheDocument();
  });

  it('does not show observability links when monitoring and log aggregation are inactive', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByText('api')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Self-Healing dashboard/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Self-heal events/i)).not.toBeInTheDocument();
  });

  it('links to the Self-Healing Grafana dashboard and the Loki saved query when active', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringActive, mockLogAggregationActive);

    render(<SelfHealPage />);

    const grafanaLink = await screen.findByRole('link', { name: /Self-Healing dashboard/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-self-healing');

    const lokiLink = screen.getByRole('link', { name: /Self-heal events/i });
    expect(lokiLink).toHaveAttribute('href', 'http://localhost:3001/explore');

    expect(screen.getByText(/self-heal:/)).toBeInTheDocument();
  });
});

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import DeployPage from '../../../src/app/admin/deploy/page';
import { exploreQueryUrl } from '../../../src/lib/grafanaExplore';

const DEPLOY_LOKI_QUERY = '{job="nyxgpt"} |= `deploy:` |~ `switched|switching|rollback|refusing`';

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

const mockStatusWithHistory = {
  namespace: 'nyxgpt',
  active: 'blue',
  inactive: 'green',
  colors: {
    blue: { healthy: true, message: 'ok' },
    green: { healthy: false, message: 'crash looping' },
  },
  history: [
    { from: 'green', to: 'blue', ts: 1768300000 },
    { from: 'blue', to: 'green', ts: 1768300100 },
  ],
  available: true,
  unavailable_reason: null,
};

const mockUnavailableStatus = {
  namespace: 'nyxgpt',
  active: 'blue',
  inactive: 'green',
  colors: {
    blue: { healthy: true, message: 'ok' },
    green: { healthy: true, message: 'ok' },
  },
  history: [],
  available: false,
  unavailable_reason: 'Not running on Kubernetes',
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
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

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

  it('links to the Deployment Grafana dashboard and deep-links the Loki query into Explore', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringActive, mockLogAggregationActive);

    render(<DeployPage />);

    const grafanaLink = await screen.findByRole('link', {
      name: /Blue\/Green Deployment dashboard/i,
    });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-deployment');

    const lokiLink = screen.getByRole('link', { name: /Deploy events/i });
    const expectedHref = exploreQueryUrl('http://localhost:3001/explore', DEPLOY_LOKI_QUERY);
    expect(lokiLink).toHaveAttribute('href', expectedHref);
    const panes = JSON.parse(new URL(expectedHref).searchParams.get('panes')!);
    expect(panes.nyx.queries[0].expr).toBe(DEPLOY_LOKI_QUERY);
  });

  it('ignores non-ok observability responses without throwing', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => new HttpResponse(null, { status: 500 })),
      http.get('/api/v1/log-aggregation', () => new HttpResponse(null, { status: 500 }))
    );

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Blue/Green Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Blue\/Green Deployment dashboard/i)).not.toBeInTheDocument();
  });

  it('swallows rejected observability requests silently', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.error()),
      http.get('/api/v1/log-aggregation', () => HttpResponse.error())
    );

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Blue/Green Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Blue\/Green Deployment dashboard/i)).not.toBeInTheDocument();
  });

  it('shows the unavailable banner and allows refreshing', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockUnavailableStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByText(/Not available in this deployment mode/)).toBeInTheDocument();
    });
    expect(screen.getByText('Not running on Kubernetes')).toBeInTheDocument();

    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.queryByText(/Not available in this deployment mode/)).not.toBeInTheDocument();
    });
  });

  it('walks every load-status error branch, then falls back to String(e) on a non-Error rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json({ error: 'deploy offline' }, { status: 500 })));
    render(<DeployPage />);
    await waitFor(() => {
      expect(screen.getByText('deploy offline')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json({ detail: 'store unreachable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('store unreachable')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json({}, { status: 503 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 503')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('network gremlin'));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('network gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('renders color cards, disables switching to an unhealthy color, and disables rollback with no history', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatusWithHistory)));

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByText('crash looping')).toBeInTheDocument();
    });
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();

    const switchButton = screen.getByRole('button', { name: /switch to green/i });
    expect(switchButton).toBeDisabled();
    expect(switchButton).toHaveAttribute('title', 'green is unhealthy; fix it before switching');

    expect(screen.getByText(/green → blue at/)).toBeInTheDocument();
    expect(screen.getByText(/blue → green at/)).toBeInTheDocument();
  });

  it('switches traffic, respects a declined confirmation, and rolls back', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByText('No switches recorded yet.')).toBeInTheDocument();
    });

    // Rollback is disabled while history is empty.
    expect(screen.getByRole('button', { name: /rollback/i })).toBeDisabled();

    // Declined confirmation -- no request fired.
    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    expect(screen.getByText('No switches recorded yet.')).toBeInTheDocument();

    // Confirmed switch
    global.confirm = vi.fn().mockReturnValue(true);
    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/deploy/switch', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({});
      }),
      http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatusWithHistory))
    );
    await user.click(screen.getByRole('button', { name: /switch to green/i }));

    await waitFor(() => {
      expect(screen.getByText('Switched to green')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ to: 'green' });

    // Declined rollback confirmation
    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    expect(screen.getByText('Switched to green')).toBeInTheDocument();

    // Confirmed rollback
    global.confirm = vi.fn().mockReturnValue(true);
    server.use(http.post('/api/v1/deploy/rollback', () => HttpResponse.json({ message: 'Rolled back to blue' })));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('Rolled back to blue')).toBeInTheDocument();
    });
  });

  it('rolls back with the default message when the response omits one', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatusWithHistory)));
    const user = userEvent.setup();

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rollback/i })).toBeEnabled();
    });

    server.use(http.post('/api/v1/deploy/rollback', () => HttpResponse.json({})));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('Rolled back')).toBeInTheDocument();
    });
  });

  it('walks every action-error fallback branch and a non-Error action rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatusWithHistory)));
    const user = userEvent.setup();

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rollback/i })).toBeEnabled();
    });

    // data.error branch
    server.use(http.post('/api/v1/deploy/rollback', () => HttpResponse.json({ error: 'rollback failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('rollback failed')).toBeInTheDocument();
    });

    // data.detail branch (error absent)
    server.use(http.post('/api/v1/deploy/rollback', () => HttpResponse.json({ detail: 'no prior state' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('no prior state')).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.post('/api/v1/deploy/rollback', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    // Non-Error rejection
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('action gremlin'));
    await user.click(screen.getByRole('button', { name: /rollback/i }));
    await waitFor(() => {
      expect(screen.getByText('action gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('walks every switch-error fallback branch and a non-Error switch rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /switch to green/i })).toBeEnabled();
    });

    // data.error branch
    server.use(http.post('/api/v1/deploy/switch', () => HttpResponse.json({ error: 'switch failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    await waitFor(() => {
      expect(screen.getByText('switch failed')).toBeInTheDocument();
    });

    // data.detail branch (error absent)
    server.use(http.post('/api/v1/deploy/switch', () => HttpResponse.json({ detail: 'target unhealthy' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    await waitFor(() => {
      expect(screen.getByText('target unhealthy')).toBeInTheDocument();
    });

    // data.message branch (error, detail absent)
    server.use(http.post('/api/v1/deploy/switch', () => HttpResponse.json({ message: 'switch refused' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    await waitFor(() => {
      expect(screen.getByText('switch refused')).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.post('/api/v1/deploy/switch', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    // Non-Error rejection
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('switch gremlin'));
    await user.click(screen.getByRole('button', { name: /switch to green/i }));
    await waitFor(() => {
      expect(screen.getByText('switch gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });
});

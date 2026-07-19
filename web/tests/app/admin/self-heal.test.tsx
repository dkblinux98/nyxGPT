import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SelfHealPage from '../../../src/app/admin/self-heal/page';

const pushMock = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: pushMock,
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
    {
      ts: 1768300900,
      service: 'api',
      reason: 'state=running health=unhealthy',
      action: 'restart',
      ok: false,
      restart_count: 3,
      message: 'Giving up after 3 attempts',
    },
  ],
};

const mockEmptyStatus = {
  enabled: true,
  components: [],
  unhealthy_count: 0,
  events: [],
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
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
    pushMock.mockClear();
  });

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
    expect(screen.getByText(/Giving up after 3 attempts/)).toBeInTheDocument();
    expect(screen.getByText('OK')).toBeInTheDocument();
    expect(screen.getByText('FAILED')).toBeInTheDocument();
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

  it('ignores non-ok and rejected observability responses without throwing', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => new HttpResponse(null, { status: 500 })),
      http.get('/api/v1/log-aggregation', () => new HttpResponse(null, { status: 500 }))
    );

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByText('api')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Self-Healing dashboard/i)).not.toBeInTheDocument();
  });

  it('swallows rejected observability requests silently', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.error()),
      http.get('/api/v1/log-aggregation', () => HttpResponse.error())
    );

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByText('api')).toBeInTheDocument();
    });
    expect(screen.queryByText(/Self-Healing dashboard/i)).not.toBeInTheDocument();
  });

  it('shows the empty states for no components and no events', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockEmptyStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByText('AUTO-HEAL ON')).toBeInTheDocument();
    });
    expect(screen.queryByText(/unhealthy$/)).not.toBeInTheDocument();
    expect(screen.getByText(/No Docker Compose containers found/)).toBeInTheDocument();
    expect(screen.getByText('No heal events recorded yet.')).toBeInTheDocument();
  });

  it('navigates back to the admin dashboard', async () => {
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /back to admin dashboard/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /back to admin dashboard/i }));
    expect(pushMock).toHaveBeenCalledWith('/admin/dashboard');
  });

  it('walks every load-status error branch, then falls back to String(e) on a non-Error rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json({ error: 'self-heal offline' }, { status: 500 })));
    render(<SelfHealPage />);
    await waitFor(() => {
      expect(screen.getByText('self-heal offline')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json({ detail: 'store unreachable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('store unreachable')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json({}, { status: 503 })));
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

  it('toggles auto-heal on and off', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /enable auto-heal/i })).toBeInTheDocument();
    });

    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/self-heal/toggle', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({ enabled: true });
      }),
      http.get('/api/v1/self-heal/status', () => HttpResponse.json({ ...mockStatus, enabled: true }))
    );
    await user.click(screen.getByRole('button', { name: /enable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('Self-heal enabled')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ enabled: true });
    expect(screen.getByText('AUTO-HEAL ON')).toBeInTheDocument();

    server.use(
      http.post('/api/v1/self-heal/toggle', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus))
    );
    await user.click(screen.getByRole('button', { name: /disable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('Self-heal disabled')).toBeInTheDocument();
    });
  });

  it('walks every toggle-error fallback branch and a non-Error toggle rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /enable auto-heal/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/toggle', () => HttpResponse.json({ error: 'toggle failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /enable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('toggle failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/toggle', () => HttpResponse.json({ detail: 'daemon unavailable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /enable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('daemon unavailable')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/toggle', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /enable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('toggle gremlin'));
    await user.click(screen.getByRole('button', { name: /enable auto-heal/i }));
    await waitFor(() => {
      expect(screen.getByText('toggle gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('heals a single component, honoring a declined confirmation', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^heal now$/i })[0]).toBeInTheDocument();
    });

    // Declined confirmation
    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getAllByRole('button', { name: /^heal now$/i })[1]);
    expect(screen.queryByText(/Restarted:/)).not.toBeInTheDocument();

    // Confirmed
    global.confirm = vi.fn().mockReturnValue(true);
    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/self-heal/heal', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({
          healed: [{ service: 'web', ok: true, ts: 1768301000, reason: 'manual', action: 'restart', restart_count: 2, message: 'Restarted web' }],
        });
      })
    );
    await user.click(screen.getAllByRole('button', { name: /^heal now$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('Restarted: web')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ service: 'web' });
  });

  it('heals all unhealthy components, honoring a declined confirmation, and reports nothing-to-heal', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /heal all unhealthy now/i })).toBeInTheDocument();
    });

    // Declined confirmation
    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    expect(screen.queryByText(/Nothing to heal/)).not.toBeInTheDocument();

    // Confirmed, nothing healed
    global.confirm = vi.fn().mockReturnValue(true);
    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/self-heal/heal', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({ healed: [] });
      })
    );
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    await waitFor(() => {
      expect(screen.getByText('Nothing to heal (all checked components are healthy)')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({});
  });

  it('walks every heal-error fallback branch and a non-Error heal rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/self-heal/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<SelfHealPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /heal all unhealthy now/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/heal', () => HttpResponse.json({ error: 'heal failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    await waitFor(() => {
      expect(screen.getByText('heal failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/heal', () => HttpResponse.json({ detail: 'daemon busy' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    await waitFor(() => {
      expect(screen.getByText('daemon busy')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/self-heal/heal', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('heal gremlin'));
    await user.click(screen.getByRole('button', { name: /heal all unhealthy now/i }));
    await waitFor(() => {
      expect(screen.getByText('heal gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });
});

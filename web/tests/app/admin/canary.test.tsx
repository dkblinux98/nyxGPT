import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CanaryPage from '../../../src/app/admin/canary/page';
import { exploreQueryUrl } from '../../../src/lib/grafanaExplore';

const CANARY_LOKI_QUERY =
  '{job="nyxgpt"} |= `canary:` |~ `starting|started|promoting|promoted|rolling back|rolled back|regression`';

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

const mockActiveStatus = {
  namespace: 'nyxgpt',
  active: true,
  weight_percent: 25,
  stable: { healthy: true, message: 'ok' },
  canary: { healthy: false, message: 'elevated errors' },
  metrics: { total_requests: 120, error_rate_percent: 1.234, p95_latency_ms: 456.7 },
  history: [
    { action: 'started', weight_percent: 10, ts: 1768300000 },
    { action: 'promoted', weight_percent: 25, from_weight_percent: 10, ts: 1768300100 },
    { action: 'evaluated', ts: 1768300200 },
  ],
  available: true,
  unavailable_reason: null,
};

const mockUnavailableStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { healthy: true, message: 'ok' },
  canary: { healthy: true, message: 'ok' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
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

describe('CanaryPage', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

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

  it('links to the Canary Grafana dashboard and deep-links the Loki query into Explore', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringActive, mockLogAggregationActive);

    render(<CanaryPage />);

    const grafanaLink = await screen.findByRole('link', { name: /Canary Rollout dashboard/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-canary');

    const lokiLink = screen.getByRole('link', { name: /Canary events/i });
    const expectedHref = exploreQueryUrl('http://localhost:3001/explore', CANARY_LOKI_QUERY);
    expect(lokiLink).toHaveAttribute('href', expectedHref);
    const panes = JSON.parse(new URL(expectedHref).searchParams.get('panes')!);
    expect(panes.nyx.queries[0].expr).toBe(CANARY_LOKI_QUERY);
  });

  it('swallows observability fetch failures silently', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => HttpResponse.error()),
      http.get('/api/v1/log-aggregation', () => HttpResponse.error())
    );

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Canary Rollout dashboard/i)).not.toBeInTheDocument();
  });

  it('ignores non-ok observability responses without throwing', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    server.use(
      http.get('/api/v1/monitoring', () => new HttpResponse(null, { status: 500 })),
      http.get('/api/v1/log-aggregation', () => new HttpResponse(null, { status: 500 }))
    );

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Canary Rollout dashboard/i)).not.toBeInTheDocument();
  });

  it('shows the unavailable banner and allows refreshing', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockUnavailableStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText(/Not available in this deployment mode/)).toBeInTheDocument();
    });
    expect(screen.getByText('Not running on Kubernetes')).toBeInTheDocument();

    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.queryByText(/Not available in this deployment mode/)).not.toBeInTheDocument();
    });
  });

  it('walks every load-status error branch, then falls back to String(e) on a non-Error rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    // errorData.error branch
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json({ error: 'canary offline' }, { status: 500 })));
    render(<CanaryPage />);
    await waitFor(() => {
      expect(screen.getByText('canary offline')).toBeInTheDocument();
    });

    // errorData.detail branch (error absent)
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json({ detail: 'store unreachable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('store unreachable')).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json({}, { status: 503 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 503')).toBeInTheDocument();
    });

    // Non-Error rejection
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('network gremlin'));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('network gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('renders metrics, history entries, and starts a canary rollout at the chosen weight', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText('IDLE')).toBeInTheDocument();
    });
    expect(screen.getByText('No canary actions recorded yet.')).toBeInTheDocument();

    const weightInput = screen.getByDisplayValue('10');
    await user.clear(weightInput);
    await user.type(weightInput, '15');

    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/canary/start', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({});
      }),
      http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus))
    );

    await user.click(screen.getByRole('button', { name: /^start canary$/i }));

    await waitFor(() => {
      expect(screen.getByText('Started canary rollout at 15%')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ weight_percent: 15 });

    // Active state: rollout badge, metrics, and history entries (with and without from_weight_percent)
    expect(screen.getByText('ROLLOUT IN PROGRESS — 25%')).toBeInTheDocument();
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('1.23%')).toBeInTheDocument();
    expect(screen.getByText('457ms')).toBeInTheDocument();
    expect(screen.getByText('Unhealthy')).toBeInTheDocument();
    expect(screen.getByText('elevated errors')).toBeInTheDocument();
    expect(screen.getByText(/promoted → 25% \(from 10%\)/)).toBeInTheDocument();
    expect(screen.getByText(/started → 10%/)).toBeInTheDocument();
    expect(screen.getByText(/^evaluated at /)).toBeInTheDocument();
  });

  it('evaluates, promotes, and rolls back an active canary, honoring declined confirmations', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /evaluate metrics/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/canary/evaluate', () => HttpResponse.json({ message: 'Metrics look healthy' })));
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));
    await waitFor(() => {
      expect(screen.getByText('Metrics look healthy')).toBeInTheDocument();
    });

    // Declined promote confirmation -- no request fired, no message change
    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /^promote$/i }));
    expect(screen.getByText('Metrics look healthy')).toBeInTheDocument();

    // Declined rollback confirmation
    await user.click(screen.getByRole('button', { name: /^rollback$/i }));
    expect(screen.getByText('Metrics look healthy')).toBeInTheDocument();

    // Confirmed promote
    global.confirm = vi.fn().mockReturnValue(true);
    server.use(http.post('/api/v1/canary/promote', () => HttpResponse.json({})));
    await user.click(screen.getByRole('button', { name: /^promote$/i }));
    await waitFor(() => {
      expect(screen.getByText('Promoted canary')).toBeInTheDocument();
    });

    // Confirmed rollback, walking the data.error / data.detail / data.message fallback chain isn't
    // needed twice -- exercise the data.message branch here.
    server.use(http.post('/api/v1/canary/rollback', () => HttpResponse.json({ message: 'Rolled back to 0%' })));
    await user.click(screen.getByRole('button', { name: /^rollback$/i }));
    await waitFor(() => {
      expect(screen.getByText('Rolled back to 0%')).toBeInTheDocument();
    });
  });

  it('walks every action-error fallback branch and a non-Error action rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /evaluate metrics/i })).toBeInTheDocument();
    });

    // data.error branch
    server.use(http.post('/api/v1/canary/evaluate', () => HttpResponse.json({ error: 'evaluation failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));
    await waitFor(() => {
      expect(screen.getByText('evaluation failed')).toBeInTheDocument();
    });

    // data.detail branch (error absent)
    server.use(http.post('/api/v1/canary/evaluate', () => HttpResponse.json({ detail: 'canary unreachable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));
    await waitFor(() => {
      expect(screen.getByText('canary unreachable')).toBeInTheDocument();
    });

    // HTTP status fallback branch (error, detail, message all absent)
    server.use(http.post('/api/v1/canary/evaluate', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    // Non-Error rejection
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('action gremlin'));
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));
    await waitFor(() => {
      expect(screen.getByText('action gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });
});

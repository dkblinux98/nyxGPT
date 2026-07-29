import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CanaryPage from '../../../src/app/admin/canary/page';
import { exploreQueryUrl } from '../../../src/lib/grafanaExplore';

const CANARY_LOKI_QUERY =
  '{job="nyxgpt"} |= `canary:` |~ `deploying|Deployed|starting|started|promoting|Promoted|rolling back|rolled back|regression`';

const mockStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { state: 'healthy', message: 'ok', version: '1.0.0-abc1234' },
  canary: { state: 'not_deployed', message: 'nyxgpt-api-canary has 0 desired replicas (idle)', version: '' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
  history: [],
  available: true,
  unavailable_reason: null,
  mode: 'kubernetes',
  mode_supported: true,
  mode_message: null,
};

const mockActiveStatus = {
  namespace: 'nyxgpt',
  active: true,
  weight_percent: 25,
  stable: { state: 'healthy', message: 'ok', version: '1.0.0-abc1234' },
  canary: { state: 'unhealthy', message: 'elevated errors', version: '1.1.0-def5678' },
  metrics: { total_requests: 120, error_rate_percent: 1.234, p95_latency_ms: 456.7 },
  history: [
    { action: 'started', weight_percent: 10, ts: 1768300000 },
    { action: 'promoted', weight_percent: 25, from_weight_percent: 10, ts: 1768300100 },
    { action: 'evaluated', ts: 1768300200 },
    { action: 'deploy', version: 'nyxgpt-api:1.1.0-def5678', ts: 1768300300 },
  ],
  available: true,
  unavailable_reason: null,
  mode: 'kubernetes',
  mode_supported: true,
  mode_message: null,
};

const mockActiveHealthyStatus = {
  ...mockActiveStatus,
  canary: { state: 'healthy', message: 'nyxgpt-api-canary healthy (1/1 ready)', version: '1.1.0-def5678' },
};

const mockUnavailableStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  canary: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
  history: [],
  available: false,
  unavailable_reason: 'kubectl not found; cannot check deployment health',
  mode: 'kubernetes',
  mode_supported: true,
  mode_message: null,
};

const mockUnsupportedModeStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  canary: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
  history: [],
  available: false,
  unavailable_reason: 'No reachable Kubernetes cluster',
  mode: 'terraform',
  mode_supported: false,
  mode_message:
    'Canary deployment is provided by Kubernetes mode; this process is currently running in terraform mode. Run `nyxgpt ops install --kubernetes` to enable it.',
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

  it('renders the three honest track states plus version, never a false Unhealthy', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText('Healthy')).toBeInTheDocument();
    });
    expect(screen.getByText('Not deployed')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt-api-canary has 0 desired replicas (idle)')).toBeInTheDocument();
    expect(screen.getByText('1.0.0-abc1234')).toBeInTheDocument();
    expect(screen.queryByText('Unhealthy')).not.toBeInTheDocument();
  });

  it('shows the mode banner (not a false unhealthy alarm) when running outside Kubernetes mode', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockUnsupportedModeStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText(/doesn.t apply to the current deployment mode \(terraform\)/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Run `nyxgpt ops install --kubernetes`/)).toBeInTheDocument();
    // The mode banner is the honest explanation -- no Unhealthy cards or unreachable-cluster banner shown.
    expect(screen.queryByText('Unhealthy')).not.toBeInTheDocument();
    expect(screen.queryByText('Kubernetes is unreachable.')).not.toBeInTheDocument();
  });

  it('shows the unreachable-cluster banner and allows refreshing when mode is Kubernetes but kubectl is unavailable', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockUnavailableStatus)));
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText('Kubernetes is unreachable.')).toBeInTheDocument();
    });
    expect(screen.getByText('kubectl not found; cannot check deployment health')).toBeInTheDocument();

    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.queryByText('Kubernetes is unreachable.')).not.toBeInTheDocument();
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
    expect(capturedBody).toEqual({ weight_percent: 15, component: 'api' });

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
    expect(screen.getByText(/deploy.*\(nyxgpt-api:1\.1\.0-def5678\)/)).toBeInTheDocument();
  });

  it('deploys the current version to canary only', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /deploy current version to canary/i })).toBeInTheDocument();
    });

    server.use(
      http.post('/api/v1/canary/deploy', () =>
        HttpResponse.json({ message: 'Deployed nyxgpt-api:1.1.0-def5678 to nyxgpt-api-canary' })
      )
    );
    await user.click(screen.getByRole('button', { name: /deploy current version to canary/i }));

    await waitFor(() => {
      expect(screen.getByText('Deployed nyxgpt-api:1.1.0-def5678 to nyxgpt-api-canary')).toBeInTheDocument();
    });
  });

  it('does not deploy when the confirmation is declined', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    global.confirm = vi.fn().mockReturnValue(false);
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /deploy current version to canary/i })).toBeInTheDocument();
    });

    const deploySpy = vi.fn();
    server.use(
      http.post('/api/v1/canary/deploy', () => {
        deploySpy();
        return HttpResponse.json({});
      })
    );
    await user.click(screen.getByRole('button', { name: /deploy current version to canary/i }));
    expect(deploySpy).not.toHaveBeenCalled();
  });

  it('shows the canary error message as the hint when the canary track errors', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    // Stable healthy but canary in a genuine error state: pairNotReady flips
    // via the canary side, so the hint must carry the canary's message.
    const canaryErrorStatus = {
      ...mockStatus,
      stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '1.0.0-abc1234' },
      canary: { state: 'error', message: 'RBAC forbids reading nyxgpt-api-canary', version: '' },
    };
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(canaryErrorStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(
        screen.getByText(/Rollout controls are disabled until the stable\/canary pair is up: RBAC forbids reading nyxgpt-api-canary/)
      ).toBeInTheDocument();
    });
  });

  it('disables rollout controls with an explanatory hint when stable is not healthy', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    // A reachable cluster (available/mode_supported) but an unhealthy stable pair.
    const notReadyStatus = {
      ...mockStatus,
      stable: { state: 'unhealthy', message: 'nyxgpt-api-stable not healthy (2/4 ready)', version: '1.0.0-abc1234' },
    };
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(notReadyStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText(/Rollout controls are disabled until the stable\/canary pair is up/)).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /deploy current version to canary/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^start canary$/i })).toBeDisabled();
  });

  it('evaluates, promotes, and rolls back an active canary, honoring declined confirmations', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveHealthyStatus)));
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

  it('disables Promote (with a hint) while the canary itself is unhealthy', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^promote$/i })).toBeInTheDocument();
    });
    const promoteButton = screen.getByRole('button', { name: /^promote$/i });
    expect(promoteButton).toBeDisabled();
    expect(promoteButton).toHaveAttribute(
      'title',
      'Refusing to shift more traffic to a canary that is not healthy'
    );
  });

  it('walks every action-error fallback branch and a non-Error action rejection', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveHealthyStatus)));
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

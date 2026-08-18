import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CanaryPage from '../../../src/app/admin/canary/page';
import { exploreQueryUrl } from '../../../src/lib/grafanaExplore';

const CANARY_LOKI_QUERY =
  '{job="nyxgpt"} |= `canary:` |~ `deploying|Deployed|starting|started|promoting|Promoted|rolling back|rolled back|regression`';

// Per-track vitals in the shape `/api/v1/canary/status` reports since #3829:
// each track's OWN Pods, with `attributable` saying whether the numbers mean
// anything. When it is false the zeros mean "unknown", not "measured zero" --
// the page must never present them as figures (that conflation is the defect).
function trackMetrics(track: string, overrides: Record<string, unknown> = {}) {
  return {
    track,
    attributable: true,
    reason: '',
    source: `pod-proxy (${track})`,
    pods_ready: 1,
    pods_scraped: 1,
    total_requests: 0,
    error_rate_percent: 0,
    p95_latency_ms: 0,
    ...overrides,
  };
}

function unattributable(track: string, reason: string) {
  return trackMetrics(track, {
    attributable: false,
    reason,
    source: '',
    pods_ready: 0,
    pods_scraped: 0,
  });
}

const idleCanaryMetrics = unattributable('canary', 'the canary track has no ready Pods');
const idleStableMetrics = unattributable('stable', 'not measured while no rollout is in progress');

const mockStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { state: 'healthy', message: 'ok', version: '1.0.0-abc1234' },
  canary: { state: 'not_deployed', message: 'nyxgpt-api-canary has 0 desired replicas (idle)', version: '' },
  metrics: idleCanaryMetrics,
  stable_metrics: idleStableMetrics,
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
  pool_replicas: 4,
  resting_replicas: 1,
  stable: { state: 'healthy', message: 'ok', version: '1.0.0-abc1234' },
  canary: { state: 'unhealthy', message: 'elevated errors', version: '1.1.0-def5678' },
  metrics: trackMetrics('canary', {
    total_requests: 120,
    error_rate_percent: 1.234,
    p95_latency_ms: 456.7,
  }),
  // Deliberately busier and healthier than the canary: these are the numbers
  // that used to be reported AS the canary's (#3829), so every canary
  // assertion below is also an assertion that they did not leak across.
  stable_metrics: trackMetrics('stable', {
    pods_ready: 4,
    pods_scraped: 4,
    total_requests: 4000,
    error_rate_percent: 0.011,
    p95_latency_ms: 38.4,
  }),
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

// A Ready canary Pod nothing has routed to yet: measurable, and measurably at
// zero. This is the state `promote` refuses, and the only one in which the
// dashboard offers the override.
const mockIdleCanaryStatus = {
  ...mockActiveHealthyStatus,
  metrics: trackMetrics('canary', { total_requests: 0 }),
};

const mockUnavailableStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  canary: { state: 'not_deployed', message: 'No reachable Kubernetes cluster', version: '' },
  metrics: unattributable('canary', 'kubectl not found; cannot check deployment health'),
  stable_metrics: unattributable('stable', 'kubectl not found; cannot check deployment health'),
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
  metrics: unattributable('canary', 'No reachable Kubernetes cluster'),
  stable_metrics: unattributable('stable', 'No reachable Kubernetes cluster'),
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

  it('walks every load-status error branch, then falls back to the raw value on a non-Error rejection', async () => {
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
        // The pool is elastic (#3833) and 15% is not expressible in it, so
        // the server reports the weight it actually rounded to -- the page
        // must show THAT, not the number that was typed into the box.
        return HttpResponse.json({
          ok: true,
          message:
            'Started canary rollout at 25% (1/4 replicas); 15% is not expressible in a pool ' +
            'of at most 4 replicas, so it rounded to 25%',
        });
      }),
      http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus))
    );

    await user.click(screen.getByRole('button', { name: /^start canary$/i }));

    await waitFor(() => {
      expect(screen.getByText(/rounded to 25%/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Started canary rollout at 15%/)).not.toBeInTheDocument();
    expect(capturedBody).toEqual({ weight_percent: 15, component: 'api' });

    // Active state: rollout badge, the borrowed pool, metrics, and history
    // entries (with and without from_weight_percent)
    expect(screen.getByText('ROLLOUT IN PROGRESS — 25%')).toBeInTheDocument();
    expect(screen.getByText(/pool: 4 replicas \(stable rests at\s*1\)/)).toBeInTheDocument();
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

  it('omits the pool badge when the server does not report the pool (#3833)', async () => {
    // A pre-#3833 server sends neither `pool_replicas` nor
    // `resting_replicas`. The badge must stay away rather than assert a
    // resting count nobody reported -- the rest of the active view is
    // unaffected.
    const { pool_replicas: _pool, resting_replicas: _resting, ...preElasticPool } =
      mockActiveStatus;
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(preElasticPool)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText('ROLLOUT IN PROGRESS — 25%')).toBeInTheDocument();
    });
    expect(screen.queryByText(/pool: .* replicas/)).not.toBeInTheDocument();
    expect(screen.queryByText(/stable rests at/)).not.toBeInTheDocument();
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

  it('switches components via the tab bar, refetching status and re-targeting every action', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText(/Deploy a new version to nyxgpt-api-canary,/)).toBeInTheDocument();
    });
    expect(screen.getByText(/then promote it to nyxgpt-api-stable/)).toBeInTheDocument();

    let lastStatusUrl: string | null = null;
    const webStatus = {
      ...mockStatus,
      component: 'web',
      stable: { state: 'healthy', message: 'nyxgpt-web-stable healthy (2/2 ready)', version: '2.0.0-web1' },
      canary: { state: 'not_deployed', message: 'nyxgpt-web-canary has 0 desired replicas (idle)', version: '' },
    };
    server.use(
      http.get('/api/v1/canary/status', ({ request }) => {
        lastStatusUrl = request.url;
        return HttpResponse.json(webStatus);
      })
    );

    await user.click(screen.getByRole('button', { name: /^web$/i }));

    await waitFor(() => {
      expect(screen.getByText(/nyxgpt-web-stable healthy/)).toBeInTheDocument();
    });
    expect(lastStatusUrl).toContain('component=web');
    expect(screen.getByText(/Deploy a new version to nyxgpt-web-canary,/)).toBeInTheDocument();
    expect(screen.getByText(/then promote it to nyxgpt-web-stable/)).toBeInTheDocument();

    let deployBody: unknown = null;
    server.use(
      http.post('/api/v1/canary/deploy', async ({ request }) => {
        deployBody = await request.json();
        return HttpResponse.json({ message: 'Deployed nyxgpt-web:2.0.1-web2 to nyxgpt-web-canary' });
      })
    );
    await user.click(screen.getByRole('button', { name: /deploy current version to canary/i }));
    await waitFor(() => {
      expect(deployBody).toEqual({ component: 'web' });
    });

    // Switching back to api re-fetches the api-scoped status.
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));
    await user.click(screen.getByRole('button', { name: /^api$/i }));
    await waitFor(() => {
      expect(screen.getByText(/Deploy a new version to nyxgpt-api-canary,/)).toBeInTheDocument();
    });
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
  // #3831: every real API failure arrives as `{"error": {"code", "message",
  // "details", "request_id"}}` -- an OBJECT. The card interpolated it and
  // rendered "[object Object]", which during acceptance hid a Pod scheduling
  // failure and made a full cluster look like a broken canary feature.
  it.each([
    [
      'the API error envelope',
      {
        error: {
          code: 'http_error',
          message:
            'Rollout of nyxgpt-api-canary did not become healthy within 180s -- ' +
            'nyxgpt-api-canary-7f9c8b6d4-2xk9p: Unschedulable: 0/1 nodes are available: ' +
            '1 Insufficient memory.',
          details: null,
          request_id: 'req-42',
        },
      },
      /Insufficient memory/,
    ],
    [
      'an envelope whose details carry the reason',
      {
        error: {
          code: 'http_error',
          message: 'Request failed',
          details: { errors: ['FailedScheduling: Insufficient memory'] },
          request_id: null,
        },
      },
      /FailedScheduling: Insufficient memory/,
    ],
    [
      'a structured FastAPI detail',
      { detail: [{ loc: ['body', 'weight_percent'], msg: 'value is not a valid integer' }] },
      /body\.weight_percent: value is not a valid integer/,
    ],
    ['an object-shaped detail', { detail: { message: 'Cluster unreachable' } }, /Cluster unreachable/],
  ])('renders %s on a failed action instead of [object Object]', async (_label, payload, expected) => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveHealthyStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /evaluate metrics/i })).toBeInTheDocument();
    });

    server.use(
      http.post('/api/v1/canary/evaluate', () => HttpResponse.json(payload, { status: 409 }))
    );
    await user.click(screen.getByRole('button', { name: /evaluate metrics/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(expected);
    expect(alert).not.toHaveTextContent('[object Object]');
  });

  it('renders the envelope from a failed status load, not [object Object]', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(
      http.get('/api/v1/canary/status', () =>
        HttpResponse.json(
          {
            error: {
              code: 'http_error',
              message: 'kubectl not found; cannot check deployment health',
              details: null,
              request_id: 'req-7',
            },
          },
          { status: 503 }
        )
      )
    );

    render(<CanaryPage />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/kubectl not found; cannot check deployment health/);
    expect(alert).not.toHaveTextContent('[object Object]');
  });

  // #3829: the page showed ONE metrics row -- the counters of whichever
  // nyxgpt-api Pod happened to serve the request. A canary at 0/1 replicas was
  // therefore presented with a stable Pod's 459 requests / 0.00% errors, and
  // the rollout gate read the same figures. Each track now reports its own.
  it('renders each track vitals from its own Pods, never one row for both', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByText('canary')).toBeInTheDocument();
    });
    expect(screen.getByText('stable')).toBeInTheDocument();
    expect(screen.getAllByText('Requests')).toHaveLength(2);

    // Canary row: the exact input "Evaluate metrics" gates on.
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('1.23%')).toBeInTheDocument();
    expect(screen.getByText('457ms')).toBeInTheDocument();
    expect(screen.getByText('1/1')).toBeInTheDocument();

    // Stable row: busier and healthier, and kept firmly out of the canary's.
    expect(screen.getByText('4000')).toBeInTheDocument();
    expect(screen.getByText('0.01%')).toBeInTheDocument();
    expect(screen.getByText('38ms')).toBeInTheDocument();
    expect(screen.getByText('4/4')).toBeInTheDocument();

    expect(
      screen.getByText(
        /Measured from each track.s own Pods, excluding health probes and metrics scrapes/
      )
    ).toBeInTheDocument();
  });

  it('states why an unmeasurable track is unmeasurable instead of showing zeros as figures', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(
        screen.getByText(
          'No traffic attributable to this track: the canary track has no ready Pods'
        )
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(
        'No traffic attributable to this track: not measured while no rollout is in progress'
      )
    ).toBeInTheDocument();
    // The zeros behind an unattributable track mean "unknown" -- rendering
    // them as Requests / Error rate / p95 is what made #3829 readable as health.
    expect(screen.queryByText('Requests')).not.toBeInTheDocument();
    expect(screen.queryByText('Error rate')).not.toBeInTheDocument();
    expect(screen.queryByText('Pods measured')).not.toBeInTheDocument();
  });

  it('offers the no-traffic promote override only for a measurably idle canary, and sends it', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockIdleCanaryStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    const checkbox = await screen.findByLabelText(/Promote despite no canary traffic/i);
    expect(checkbox).not.toBeChecked();

    let promoteBody: unknown = null;
    server.use(
      http.post('/api/v1/canary/promote', async ({ request }) => {
        promoteBody = await request.json();
        return HttpResponse.json({});
      })
    );

    // Unchecked: the api-side refusal stands.
    await user.click(screen.getByRole('button', { name: /^promote$/i }));
    await waitFor(() => {
      expect(promoteBody).toEqual({ component: 'api', force: false });
    });

    // Checked: the operator's explicit "this cluster is simply idle".
    promoteBody = null;
    await user.click(await screen.findByLabelText(/Promote despite no canary traffic/i));
    expect(await screen.findByLabelText(/Promote despite no canary traffic/i)).toBeChecked();
    await user.click(screen.getByRole('button', { name: /^promote$/i }));
    await waitFor(() => {
      expect(promoteBody).toEqual({ component: 'api', force: true });
    });
  });

  it('withholds the override when the canary has served traffic or cannot be measured', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockActiveHealthyStatus)));
    const user = userEvent.setup();

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^promote$/i })).toBeInTheDocument();
    });
    // 120 canary requests: nothing to override.
    expect(screen.queryByLabelText(/Promote despite no canary traffic/i)).not.toBeInTheDocument();

    // Unattributable is NOT "idle": offering the override here would let an
    // operator wave through a canary nothing can even reach.
    server.use(
      http.get('/api/v1/canary/status', () =>
        HttpResponse.json({
          ...mockActiveHealthyStatus,
          metrics: unattributable('canary', 'every canary Pod scrape failed'),
        })
      )
    );
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(
        screen.getByText('No traffic attributable to this track: every canary Pod scrape failed')
      ).toBeInTheDocument();
    });
    expect(screen.queryByLabelText(/Promote despite no canary traffic/i)).not.toBeInTheDocument();
  });

  it('degrades a pre-per-track status payload to a named placeholder instead of blanking', async () => {
    mockObservability(mockMonitoringDisabled, mockLogAggregationDisabled);
    // The rolling-upgrade window: this build talking to an api still serving
    // the old process-wide `metrics` and no `stable_metrics` at all.
    server.use(
      http.get('/api/v1/canary/status', () =>
        HttpResponse.json({
          ...mockActiveHealthyStatus,
          metrics: { total_requests: 459, error_rate_percent: 0, p95_latency_ms: 6 },
          stable_metrics: undefined,
        })
      )
    );

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });
    expect(
      screen.getAllByText(
        'No traffic attributable to this track: not reported by this version of the API'
      )
    ).toHaveLength(2);
    // The old process-wide count is not silently re-presented as the canary's.
    expect(screen.queryByText('459')).not.toBeInTheDocument();
  });
});

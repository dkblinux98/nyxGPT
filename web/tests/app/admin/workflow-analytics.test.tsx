import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import WorkflowAnalyticsPage from '../../../src/app/admin/workflow-analytics/page';

const fullStats = {
  available: true,
  collected: true,
  stats: {
    window_days: 30,
    total_runs: 42,
    success_rate: 90,
    avg_duration_s: 125,
    failures: 4,
    by_workflow: [
      { workflow: 'ci', runs: 30, success_rate: 93, avg_duration_s: 300, failures: 2 },
      { workflow: 'deploy', runs: 12, success_rate: 83, avg_duration_s: 45, failures: 2 },
    ],
    top_failing: [{ workflow: 'ci', runs: 30, success_rate: 93, avg_duration_s: 300, failures: 2 }],
  },
  recent_runs: [
    {
      run_id: 1,
      workflow_name: 'ci',
      status: 'completed',
      conclusion: 'success',
      branch: 'main',
      issue_number: 42,
      url: 'https://github.com/org/repo/actions/runs/1',
      created_at: 1768300800,
      duration_s: 305,
    },
    {
      run_id: 2,
      workflow_name: 'deploy',
      status: 'completed',
      conclusion: null,
      branch: null,
      issue_number: null,
      url: null,
      created_at: 1768300900,
      duration_s: 45,
    },
    {
      run_id: 3,
      workflow_name: 'lint',
      status: 'queued',
      conclusion: null,
      branch: 'feature/x',
      issue_number: 0,
      url: 'https://github.com/org/repo/actions/runs/3',
      created_at: 1768301000,
      duration_s: null,
    },
  ],
};

const noFailuresStats = {
  available: true,
  collected: true,
  stats: {
    window_days: 7,
    total_runs: 5,
    success_rate: 100,
    avg_duration_s: 30,
    failures: 0,
    by_workflow: [],
    top_failing: [],
  },
  recent_runs: [],
};

function mockAnalytics(body: unknown) {
  server.use(http.get('/api/v1/admin/workflow-analytics', () => HttpResponse.json(body)));
}

describe('WorkflowAnalyticsPage', () => {
  it('renders the heading and back link', async () => {
    mockAnalytics(noFailuresStats);

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'CI Workflow Analytics' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('shows an error and recovers on retry', async () => {
    server.use(http.get('/api/v1/admin/workflow-analytics', () => new HttpResponse(null, { status: 500 })));

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load workflow analytics')).toBeInTheDocument();
    });

    mockAnalytics(noFailuresStats);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getByText('No runs in this window.')).toBeInTheDocument();
    });
  });

  it('shows the String(e) fallback message when loading rejects with a non-Error value', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('analytics gremlin'));

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText('analytics gremlin')).toBeInTheDocument();
    });

    fetchSpy.mockRestore();
  });

  it('shows the unavailable state including the default reason fallback', async () => {
    mockAnalytics({ available: false, stats: null, recent_runs: [] });

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Workflow analytics are unavailable \(unknown reason\)/)).toBeInTheDocument();
    });
  });

  it('shows the unavailable state with a specific reason', async () => {
    mockAnalytics({ available: false, reason: 'history store disabled', stats: null, recent_runs: [] });

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Workflow analytics are unavailable \(history store disabled\)/)).toBeInTheDocument();
    });
  });

  it('shows the not-yet-collected state with setup instructions', async () => {
    mockAnalytics({ available: true, collected: false, stats: null, recent_runs: [] });

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText('No workflow run history has been collected yet.')).toBeInTheDocument();
    });
    expect(screen.getByText(/collect_workflow_logs.sh/)).toBeInTheDocument();
  });

  it('renders null content when available and collected are true but stats is absent', async () => {
    mockAnalytics({ available: true, collected: true, stats: null, recent_runs: [] });

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'CI Workflow Analytics' })).toBeInTheDocument();
    });
    expect(screen.queryByText(/Summary \(last/)).not.toBeInTheDocument();
    expect(screen.queryByText('No workflow run history has been collected yet.')).not.toBeInTheDocument();
  });

  it('shows the no-failures and no-runs empty states', async () => {
    mockAnalytics(noFailuresStats);

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText('No failures in this window.')).toBeInTheDocument();
    });
    expect(screen.getByText('No runs in this window.')).toBeInTheDocument();
    expect(screen.getByText('No runs stored yet.')).toBeInTheDocument();
  });

  it('renders full stats, per-workflow breakdown, top failures, and recent runs', async () => {
    mockAnalytics(fullStats);

    render(<WorkflowAnalyticsPage />);

    await waitFor(() => {
      expect(screen.getByText('Summary (last 30 days)')).toBeInTheDocument();
    });

    const summarySection = screen.getByRole('region', { name: 'Summary' });
    expect(within(summarySection).getByText('42')).toBeInTheDocument();
    expect(within(summarySection).getByText('90%')).toBeInTheDocument();
    expect(within(summarySection).getByText('2m 5s')).toBeInTheDocument();
    expect(within(summarySection).getByText('4')).toBeInTheDocument();

    const topFailingSection = screen.getByRole('region', { name: 'Top failing workflows' });
    expect(topFailingSection.textContent).toContain('ci: 2 failures (93% success)');

    const breakdownSection = screen.getByRole('region', { name: 'Per-workflow breakdown' });
    expect(within(breakdownSection).getByText('ci')).toBeInTheDocument();
    expect(within(breakdownSection).getByText('deploy')).toBeInTheDocument();
    expect(within(breakdownSection).getByText('45s')).toBeInTheDocument();
    expect(within(breakdownSection).getByText('5m 0s')).toBeInTheDocument();

    // recent runs: linked url, conclusion vs status fallback, branch/issue fallbacks, duration '-' fallback
    const recentRunsSection = screen.getByRole('region', { name: 'Recent runs' });
    const ciLink = within(recentRunsSection).getByRole('link', { name: 'ci' });
    expect(ciLink).toHaveAttribute('href', 'https://github.com/org/repo/actions/runs/1');
    expect(within(recentRunsSection).getByText('success')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('main')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('42')).toBeInTheDocument();

    expect(within(recentRunsSection).getByText('deploy')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('completed')).toBeInTheDocument();
    expect(within(recentRunsSection).getAllByText('-').length).toBeGreaterThanOrEqual(2);

    expect(within(recentRunsSection).getByText('lint')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('queued')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('feature/x')).toBeInTheDocument();
    expect(within(recentRunsSection).getByText('0')).toBeInTheDocument();
  });

  it('changes the window when a different day range is selected', async () => {
    mockAnalytics(fullStats);
    const user = userEvent.setup();

    render(<WorkflowAnalyticsPage />);
    await waitFor(() => {
      expect(screen.getByLabelText('Window:')).toBeInTheDocument();
    });

    mockAnalytics({ ...fullStats, stats: { ...fullStats.stats, window_days: 90 } });
    await user.selectOptions(screen.getByLabelText('Window:'), '90');

    await waitFor(() => {
      expect(screen.getByText('Summary (last 90 days)')).toBeInTheDocument();
    });
  });
});

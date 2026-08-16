import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, delay } from 'msw';
import { server } from '../../mocks/server';
import CloudArtifactSmokePage from '../../../src/app/admin/cloud-smoke/page';

// The SRE/admin surface for `nyxgpt cloud smoke --container` (#3784). Two
// things make this page worth testing rather than eyeballing:
//
//  1. It is the Definition-of-Done surface: the smoke has to be *startable*
//     and its verdict *readable* from the dashboard, not only from a terminal.
//  2. Everything it renders is a claim about how much is proven. A run that
//     passed because a fault was injected, and the list of what a green run
//     does not cover, are the difference between "the install path works" and
//     "the cloud path works" -- the exact conflation the five serial rc9
//     defects hid behind. So the fault-injected banner and the coverage gaps
//     are pinned here, not treated as decoration.

const faults = [
  {
    fault: 'no-node',
    description: 'Drop the Node 20 provisioning (#3761)',
    expects: 'node-provisioning',
  },
  {
    fault: 'old-python',
    description: 'Rewrite versioned interpreters back to the system python3 (#3782)',
    expects: 'interpreter-selection',
  },
];

const idleStatus = {
  running: false,
  current: {},
  last_result: {},
  docker_available: true,
  docker_detail: '28.0.4',
  base_image: 'amazonlinux:2023',
  faults,
  coverage_gaps: [
    'EC2 provisioning: Terraform, the AMI itself and instance lifecycle are not exercised.',
    'cloud-init: the bootstrap is executed directly as root.',
  ],
  commands: {
    run: 'nyxgpt cloud smoke --container',
    status: 'nyxgpt cloud smoke --container --status',
  },
  docs: 'docs/cloud-artifact-smoke.md',
};

const passedResult = {
  passed: true,
  failure: '',
  failure_class: '',
  detail: 'bare AL2023 -> artifact install -> services serving, verified',
  expected_failure: false,
  version: '3.0.0rc9',
  base_image: 'amazonlinux:2023',
  steps: [{ step: 'preflight' }, { step: 'bootstrap' }, { step: 'services' }],
  diagnostics: {},
  started_at: '2026-08-15T01:00:00+00:00',
  finished_at: '2026-08-15T01:31:00+00:00',
  duration_seconds: 1860.4,
  injected_faults: [],
};

const failedResult = {
  ...passedResult,
  passed: false,
  failure: 'The EC2 bootstrap failed inside the container (exit 1)',
  failure_class: 'node provisioning: the web build ran without a usable Node 20 (the #3761 class)',
  detail: 'The EC2 bootstrap failed inside the container (exit 1)',
  diagnostics: {
    ops_status: 'nyxgpt-web  failed',
    // Empty sections are collected but must not render an empty disclosure.
    api_log: '',
  },
};

// A fault-injected run that passed *because* it failed. Without the banner it
// is indistinguishable from a normal green run on the panel.
const injectedResult = {
  ...passedResult,
  expected_failure: true,
  failure: 'The EC2 bootstrap failed inside the container (exit 1)',
  failure_class: 'interpreter selection: the venv was built on a Python too old',
  detail: 'injected old-python and the smoke failed with the expected interpreter-selection failure',
  injected_faults: [{ fault: 'old-python', description: faults[1].description, changed: true }],
};

// An older record, from before `steps`/`diagnostics` were recorded. It must
// render rather than take the panel down with it.
const sparseResult = {
  passed: true,
  failure: '',
  failure_class: '',
  detail: 'recorded by an older CLI',
  expected_failure: false,
  version: 'latest',
  base_image: 'amazonlinux:2023',
  started_at: '2026-08-01T00:00:00+00:00',
  finished_at: '2026-08-01T00:20:00+00:00',
  duration_seconds: 1200,
};

function mockStatus(payload: unknown, status = 200) {
  server.use(
    http.get('/api/v1/ops/cloud-artifact-smoke', () => HttpResponse.json(payload, { status }))
  );
}

function mockStart(payload: unknown, status = 200) {
  server.use(
    http.post('/api/v1/ops/cloud-artifact-smoke', () => HttpResponse.json(payload, { status }))
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockStatus(idleStatus);
});

describe('CloudArtifactSmokePage', () => {
  it('reports an idle machine with nothing recorded yet', async () => {
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText('No run has been recorded on this machine yet.')).toBeInTheDocument();
    // The CLI equivalents are on the page: the dashboard surface never
    // replaces the wrapped command, it points at it (CLAUDE.md).
    expect(screen.getByText('nyxgpt cloud smoke --container')).toBeInTheDocument();
    expect(screen.getByText('docs/cloud-artifact-smoke.md')).toBeInTheDocument();
    // Both injectable faults are offered, each labelled with its inversion.
    expect(screen.getByRole('option', { name: 'inject old-python (must fail)' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'inject no-node (must fail)' })).toBeInTheDocument();
  });

  it('states what a green run does not cover', async () => {
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText(/Terraform, the AMI itself/)).toBeInTheDocument();
    expect(screen.getByText(/executed directly as root/)).toBeInTheDocument();
  });

  it('refuses to offer a run when Docker is unusable here, and says why', async () => {
    mockStatus({
      ...idleStatus,
      docker_available: false,
      docker_detail: 'the `docker` command is not installed on this machine',
    });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText(/the `docker` command is not installed/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run smoke' })).toBeDisabled();
  });

  it('starts a run with the version and fault the operator chose', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/ops/cloud-artifact-smoke', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ started: true, started_at: '2026-08-15T02:00:00+00:00' });
      })
    );

    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');

    await userEvent.type(
      screen.getByLabelText('Published nyxGPT version to install'),
      '3.0.0rc9'
    );
    await userEvent.selectOptions(screen.getByLabelText('Fault to inject'), 'old-python');
    await userEvent.click(screen.getByRole('button', { name: 'Run smoke' }));

    expect(await screen.findByText(/Run started at 2026-08-15T02:00:00/)).toBeInTheDocument();
    expect(body).toEqual({ version: '3.0.0rc9', inject: ['old-python'] });
  });

  it('starts a plain run when nothing is filled in', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/ops/cloud-artifact-smoke', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ started: true, started_at: '2026-08-15T02:05:00+00:00' });
      })
    );

    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');
    await userEvent.click(screen.getByRole('button', { name: 'Run smoke' }));

    await screen.findByText(/Run started at 2026-08-15T02:05:00/);
    // No version and no fault: the backend's own defaults, not empty strings.
    expect(body).toEqual({});
  });

  it('says the run is starting while the request is in flight', async () => {
    server.use(
      http.post('/api/v1/ops/cloud-artifact-smoke', async () => {
        await delay(30);
        return HttpResponse.json({ started: true, started_at: '2026-08-15T02:10:00+00:00' });
      })
    );

    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');
    await userEvent.click(screen.getByRole('button', { name: 'Run smoke' }));

    expect(await screen.findByRole('button', { name: 'Starting…' })).toBeDisabled();
    await screen.findByText(/Run started at 2026-08-15T02:10:00/);
  });

  // The 409s this endpoint actually returns arrive in the API's error
  // envelope, `{"error": {"message": ...}}`. Reading `data.error` straight
  // rendered them as "[object Object]" -- the operator was shown a banner
  // that told them nothing about the run already in flight.
  it.each([
    [
      'the API error envelope',
      { error: { code: 'http_error', message: 'A containerized cloud smoke is still running' } },
      409,
      /A containerized cloud smoke is still running/,
    ],
    ['a bare string error', { error: 'docker is not reachable' }, 409, /docker is not reachable/],
    ['a FastAPI detail', { detail: 'A Docker engine is required' }, 409, /A Docker engine is required/],
    ['an unrecognised shape', { unexpected: true }, 500, /HTTP 500/],
    ['an error object without a message', { error: { code: 7 } }, 503, /HTTP 503/],
    ['a body that is not an object', 'plain text', 500, /HTTP 500/],
  ])('reports %s from a refused run', async (_label, payload, status, expected) => {
    mockStart(payload, status);

    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');
    await userEvent.click(screen.getByRole('button', { name: 'Run smoke' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(expected);
  });

  it('surfaces a failed status load and retries it on demand', async () => {
    mockStatus({ error: { message: 'the API is down' } }, 502);
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/the API is down/);

    mockStatus(idleStatus);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('No run has been recorded on this machine yet.')).toBeInTheDocument();
  });

  it('surfaces a non-Error rejection from the status fetch', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('network exploded');

    render(<CloudArtifactSmokePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/network exploded/);
    spy.mockRestore();
  });

  it('surfaces a non-Error rejection from starting a run', async () => {
    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');

    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('socket hang up');
    await userEvent.click(screen.getByRole('button', { name: 'Run smoke' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/socket hang up/);
    spy.mockRestore();
  });

  it('shows an in-flight run and the fault it is injecting, and blocks a second one', async () => {
    mockStatus({
      ...idleStatus,
      running: true,
      current: {
        started_at: '2026-08-15T03:00:00+00:00',
        version: '3.0.0rc9',
        faults: ['old-python'],
      },
    });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText(/injecting old-python/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run in flight…' })).toBeDisabled();
  });

  it('shows an in-flight run with no fault injected', async () => {
    mockStatus({
      ...idleStatus,
      running: true,
      current: { started_at: '2026-08-15T03:10:00+00:00', version: 'latest' },
    });
    render(<CloudArtifactSmokePage />);

    const line = await screen.findByText(/Started 2026-08-15T03:10:00/);
    expect(line).not.toHaveTextContent(/injecting/);
  });

  it('polls while a run is in flight and shows the verdict when it lands', async () => {
    // The run takes tens of minutes, so the panel is the only place an
    // operator watches it from -- a poll that never fires leaves them looking
    // at "Run in flight…" long after the smoke has finished.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let polls = 0;
      server.use(
        http.get('/api/v1/ops/cloud-artifact-smoke', () => {
          polls += 1;
          return HttpResponse.json(
            polls === 1
              ? {
                  ...idleStatus,
                  running: true,
                  current: { started_at: '2026-08-15T04:00:00+00:00', version: '3.0.0rc9' },
                }
              : { ...idleStatus, last_result: passedResult }
          );
        })
      );

      render(<CloudArtifactSmokePage />);
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Run in flight…' })).toBeInTheDocument()
      );

      await vi.advanceTimersByTimeAsync(15000);

      await waitFor(() => expect(screen.getByText('PASSED')).toBeInTheDocument());
      expect(polls).toBeGreaterThan(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders a passing run with the phases it completed', async () => {
    mockStatus({ ...idleStatus, last_result: passedResult });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText('PASSED')).toBeInTheDocument();
    expect(screen.getByText(/services serving, verified/)).toBeInTheDocument();
    expect(screen.getByText(/version 3.0.0rc9 on amazonlinux:2023/)).toBeInTheDocument();
    expect(screen.getByText('bootstrap')).toBeInTheDocument();
    // Nothing failed, so no defect class and no diagnostics are offered.
    expect(screen.queryByText(/Defect class:/)).not.toBeInTheDocument();
    expect(screen.queryByText('ops_status')).not.toBeInTheDocument();
  });

  it('renders a failing run with its defect class and non-empty diagnostics only', async () => {
    mockStatus({ ...idleStatus, last_result: failedResult });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText(/node provisioning/)).toBeInTheDocument();
    expect(screen.getByText('ops_status')).toBeInTheDocument();
    // `api_log` was collected but came back empty -- an empty disclosure is
    // noise an operator has to open to discover there is nothing in it.
    expect(screen.queryByText('api_log')).not.toBeInTheDocument();
  });

  it('marks a fault-injected pass as one, so it is not read as a clean green run', async () => {
    mockStatus({ ...idleStatus, last_result: injectedResult });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText('PASSED')).toBeInTheDocument();
    expect(
      screen.getByText(/fault-injected run: failure is the pass condition/)
    ).toBeInTheDocument();
  });

  it('renders a record that predates the steps and diagnostics fields', async () => {
    mockStatus({ ...idleStatus, last_result: sparseResult });
    render(<CloudArtifactSmokePage />);

    expect(await screen.findByText('PASSED')).toBeInTheDocument();
    expect(screen.getByText(/recorded by an older CLI/)).toBeInTheDocument();
  });

  it('refreshes the panel on demand', async () => {
    render(<CloudArtifactSmokePage />);
    await screen.findByText('No run has been recorded on this machine yet.');

    mockStatus({ ...idleStatus, last_result: passedResult });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    const lastRun = await screen.findByText('PASSED');
    expect(within(lastRun.parentElement as HTMLElement).getByText(/verified/)).toBeInTheDocument();
  });
});

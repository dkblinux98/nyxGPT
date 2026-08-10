import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import PortabilityPage from '../../../src/app/admin/portability/page';

// Shaped like nyxgpt.portability.check_matrix()'s payload (P6-16, #3516), with
// one green row and one gapped row -- the two states the page has to render
// differently.
const mockReport = {
  targets: [
    {
      key: 'linux-native',
      name: 'Linux native (systemd --user)',
      artifact: 'PyPI wheel (nyxgpt)',
      install: ['pip install nyxgpt'],
      operate: ['nyxgpt up', 'nyxgpt ops status'],
      teardown: 'nyxgpt down',
      status: 'ci-verified',
      evidence: ['.github/workflows/linux-native-smoke.yml'],
      notes: 'Installed from the published wheel with no repo checkout in the job.',
      gaps: [],
      checks: [
        {
          check: 'repo_less',
          passed: true,
          skipped: false,
          detail: 'all 4 commands install/operate from published artifacts',
        },
        {
          check: 'evidence',
          passed: true,
          skipped: true,
          detail: 'no repo checkout here, so evidence paths cannot be resolved (expected)',
        },
      ],
      invariants_passed: true,
      acceptance_ready: true,
    },
    {
      key: 'kubernetes',
      name: 'Kubernetes',
      artifact: 'ghcr.io/dkblinux98/nyxgpt-api',
      install: ['pip install nyxgpt'],
      operate: ['nyxgpt ops install --kubernetes --local'],
      teardown: 'nyxgpt ops down --kubernetes',
      status: 'gap',
      evidence: ['docs/kubernetes.md'],
      notes: 'Fully wrapped, but not yet checkout-free.',
      gaps: ['k8s/*.yaml is not package data, so the kustomization only exists in a checkout.'],
      checks: [
        {
          check: 'repo_less',
          passed: true,
          skipped: false,
          detail: 'all 3 commands install/operate from published artifacts',
        },
      ],
      invariants_passed: true,
      acceptance_ready: false,
    },
  ],
  acceptance_sequence: [
    {
      step: 'install',
      command: 'pip install nyxgpt',
      expect: '`nyxgpt --version` prints the released version; no checkout exists',
    },
    {
      step: 'teardown',
      command: 'nyxgpt cloud destroy --yes',
      expect: 'tunnel closed, substrate destroyed, no billed resources left',
    },
  ],
  commands: {
    report: 'nyxgpt ops portability',
    strict: 'nyxgpt ops portability --strict',
    json: 'nyxgpt ops portability --json',
  },
  checkout: '',
  summary: {
    total: 2,
    acceptance_ready: 1,
    invariants_failed: 0,
    open_gaps: 1,
    windows_in_scope: false,
  },
  acceptance_ready: false,
};

const allGreen = {
  ...mockReport,
  targets: [mockReport.targets[0]],
  summary: { ...mockReport.summary, total: 1, acceptance_ready: 1, open_gaps: 0 },
  acceptance_ready: true,
};

function serveReport(payload: unknown) {
  server.use(http.get('/api/v1/ops/portability', () => HttpResponse.json(payload)));
}

describe('PortabilityPage', () => {
  it('renders every target with its wrapped install, operate and teardown commands', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Linux native (systemd --user)')).toBeInTheDocument();
    });
    expect(screen.getByText('Kubernetes')).toBeInTheDocument();
    expect(screen.getAllByText('pip install nyxgpt').length).toBeGreaterThan(0);
    expect(screen.getByText('nyxgpt ops install --kubernetes --local')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt ops down --kubernetes')).toBeInTheDocument();
  });

  it('reports the gap and says the capstone criterion is not met', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText(/1\/2/)).toBeInTheDocument();
    });
    expect(screen.getByText(/k8s\/\*\.yaml is not package data/)).toBeInTheDocument();
    expect(
      screen.getByText(/capstone portability criterion is not met while any gap is open/)
    ).toBeInTheDocument();
    // The status badge distinguishes a gap from a verified row.
    expect(screen.getByText('Gap')).toBeInTheDocument();
    expect(screen.getByText('Verified in CI')).toBeInTheDocument();
  });

  it('drops the not-met warning once every target is checkout-free', async () => {
    serveReport(allGreen);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Linux native (systemd --user)')).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/capstone portability criterion is not met/)
    ).not.toBeInTheDocument();
  });

  it('renders a skipped check as skipped, not as a failure', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('skipped')).toBeInTheDocument();
    });
    expect(screen.queryByText('FAIL')).not.toBeInTheDocument();
  });

  it('renders the clean-machine acceptance sequence in order', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Clean-machine acceptance sequence')).toBeInTheDocument();
    });
    const steps = screen.getAllByRole('listitem').map((li) => li.textContent ?? '');
    const install = steps.findIndex((text) => text.includes('pip install nyxgpt'));
    const teardown = steps.findIndex((text) => text.includes('nyxgpt cloud destroy --yes'));
    expect(install).toBeGreaterThanOrEqual(0);
    expect(teardown).toBeGreaterThan(install);
  });

  it('renders the wrapped CLI commands from the backend rather than its own copy', async () => {
    serveReport({
      ...mockReport,
      commands: { ...mockReport.commands, report: 'nyxgpt ops portability --from-backend' },
    });
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('nyxgpt ops portability --from-backend')).toBeInTheDocument();
    });
  });

  it('never renders an action control -- the page only reports', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Kubernetes')).toBeInTheDocument();
    });
    // Refresh re-reads the matrix and changes nothing; it is the only button.
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent(/Refresh/);
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
  });

  it('surfaces a backend failure instead of an empty matrix', async () => {
    server.use(
      http.get('/api/v1/ops/portability', () =>
        HttpResponse.json({ error: 'backend unreachable' }, { status: 502 })
      )
    );
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText(/backend unreachable/)).toBeInTheDocument();
    });
  });

  it('links back to the admin dashboard', async () => {
    serveReport(mockReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /Back to Admin Dashboard/ })).toHaveAttribute(
        'href',
        '/admin/dashboard'
      );
    });
  });
});

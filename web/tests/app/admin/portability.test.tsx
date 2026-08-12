import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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

// A row whose invariants actually failed. Rendering a red FAIL and counting
// the failure in the summary is a large part of what this page is for, and no
// happy-path mock above exercises either. `operate: []` covers the same row's
// other realistic edge: a target that has no operate step yet renders no
// empty "Operate" heading.
const failingReport = {
  ...mockReport,
  targets: [
    {
      ...mockReport.targets[1],
      operate: [],
      checks: [
        {
          check: 'repo_less',
          passed: false,
          skipped: false,
          detail: "'git clone …' fetches source instead of installing a published artifact",
        },
      ],
      invariants_passed: false,
    },
  ],
  summary: { ...mockReport.summary, total: 1, acceptance_ready: 0, invariants_failed: 1 },
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

  it('renders a failed invariant as FAIL and counts it in the summary', async () => {
    serveReport(failingReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('FAIL')).toBeInTheDocument();
    });
    expect(screen.getByText(/1 invariant failure\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/fetches source instead of installing/)).toBeInTheDocument();
  });

  it('omits the Operate heading for a target with no operate commands', async () => {
    serveReport(failingReport);
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText('Tear down')).toBeInTheDocument();
    });
    expect(screen.queryByText('Operate')).not.toBeInTheDocument();
  });

  it('falls back to the backend detail when the error body has no error field', async () => {
    server.use(
      http.get('/api/v1/ops/portability', () =>
        HttpResponse.json({ detail: 'matrix module failed to import' }, { status: 500 })
      )
    );
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText(/matrix module failed to import/)).toBeInTheDocument();
    });
  });

  it('falls back to the bare status when the error body explains nothing', async () => {
    server.use(
      http.get('/api/v1/ops/portability', () => HttpResponse.json({}, { status: 503 }))
    );
    render(<PortabilityPage />);

    await waitFor(() => {
      expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    });
  });

  it('surfaces a rejection that is not an Error', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('connection reset') as unknown as typeof fetch;
    try {
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/connection reset/)).toBeInTheDocument();
      });
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('disables the refresh control while the matrix is being re-read', async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let calls = 0;
    server.use(
      http.get('/api/v1/ops/portability', async () => {
        calls += 1;
        // Only the refresh is gated; the initial load resolves immediately so
        // the button exists to click.
        if (calls > 1) {
          await gate;
        }
        return HttpResponse.json(mockReport);
      })
    );
    render(<PortabilityPage />);

    const button = await screen.findByRole('button', { name: 'Refresh' });
    fireEvent.click(button);

    const refreshing = await screen.findByRole('button', { name: 'Refreshing…' });
    expect(refreshing).toBeDisabled();

    release();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    });
    expect(calls).toBe(2);
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

  // --- PyPI publish panel (#3727) ---
  //
  // Acceptance installs come from PyPI, so testing the release-branch tip
  // repo-less needs a published pre-release. The panel says which build to
  // pin; it never publishes one (that carries the owner's credentials).

  describe('PyPI publish panel', () => {
    const rcPlan = {
      branch: 'feat/x',
      channel: 'rc',
      release: '3.0.0',
      declared_version: '3.0.0',
      published_rcs: [],
      published_dev_builds: [],
      next_rc_version: '3.0.0rc1',
      next_dev_version: '3.0.0.dev1',
      version: '3.0.0rc1',
      is_prerelease: true,
      publishable: false,
      blockers: ['feat/x is not a release branch -- a build is only ever cut from the tip'],
      guardrails: [],
      pypi_lookup_error: '',
      workflow: 'release-publish-pypi.yml',
      docs: 'docs/cloud.md#pypi-publishing-dev-rc-and-stable',
      commands: {
        plan: 'nyxgpt release publish --channel rc',
        publish: 'nyxgpt release publish --channel rc --publish',
        install: 'pip install nyxgpt==3.0.0rc1',
        user_data: 'nyxgpt cloud user-data --os linux --version 3.0.0rc1',
        deploy: 'nyxgpt cloud deploy --version 3.0.0rc1',
      },
    };

    function serveRc(payload: unknown, status = 200) {
      server.use(
        http.get('/api/v1/ops/release-candidate', () => HttpResponse.json(payload, { status }))
      );
    }

    it('names the next build and the pinned commands that install it', async () => {
      serveReport(mockReport);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/PyPI builds/)).toBeInTheDocument();
      });
      expect(screen.getByText('3.0.0rc2')).toBeInTheDocument();
      expect(screen.getByText('pip install nyxgpt==3.0.0rc2')).toBeInTheDocument();
      expect(
        screen.getByText('nyxgpt cloud user-data --os linux --version 3.0.0rc2')
      ).toBeInTheDocument();
      expect(screen.getByText('nyxgpt cloud deploy --version 3.0.0rc2')).toBeInTheDocument();
      expect(screen.getByText('nyxgpt release publish --channel rc --publish')).toBeInTheDocument();
      expect(screen.getByText('ready to cut')).toBeInTheDocument();
    });

    it('offers the macOS rc install when the channel stamps the tap', async () => {
      // An rc publish also pushes nyxgpt-api-rc/nyxgpt-web-rc to the tap, so
      // the panel has to say how to accept a candidate on macOS too (#3727).
      serveReport(mockReport);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/Accept it on macOS/)).toBeInTheDocument();
      });
      expect(
        screen.getByText('brew tap dkblinux98/nyxgpt && brew install nyxgpt-api-rc nyxgpt-web-rc')
      ).toBeInTheDocument();
    });

    it('omits the macOS install for a dev build, which never touches the tap', async () => {
      // The backend omits `commands.brew` on the dev channel; rendering the
      // heading anyway would advertise a formula no nightly ever publishes.
      serveReport(mockReport);
      serveRc({
        ...rcPlan,
        branch: 'v3.0.0',
        channel: 'dev',
        version: '3.0.0.dev42',
        publishable: true,
        blockers: [],
        commands: {
          plan: 'nyxgpt release publish --channel dev',
          publish: 'nyxgpt release publish --channel dev --publish',
          install: 'pip install nyxgpt==3.0.0.dev42',
          user_data: 'nyxgpt cloud user-data --os linux --version 3.0.0.dev42',
          deploy: 'nyxgpt cloud deploy --version 3.0.0.dev42',
        },
      });
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText('pip install nyxgpt==3.0.0.dev42')).toBeInTheDocument();
      });
      expect(screen.queryByText(/Accept it on macOS/)).not.toBeInTheDocument();
    });

    it('lists the nightly dev builds already on PyPI', async () => {
      serveReport(mockReport);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/Nightly dev builds/)).toBeInTheDocument();
      });
      expect(screen.getByText(/3\.0\.0\.dev4/)).toBeInTheDocument();
    });

    it('shows why a build cannot be cut instead of offering one anyway', async () => {
      serveReport(mockReport);
      serveRc(rcPlan);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText('blocked')).toBeInTheDocument();
      });
      expect(screen.getByText(/is not a release branch/)).toBeInTheDocument();
      // Both lines ("Published RCs", "Nightly dev builds") say so explicitly
      // rather than rendering an empty list.
      expect(screen.getAllByText(/none yet/)).toHaveLength(2);
    });

    it('warns when PyPI could not be reached, rather than trusting the number', async () => {
      // The next build number is derived from what PyPI serves, so a failed
      // lookup has to be visible -- not silently rendered as a fact.
      serveReport(mockReport);
      serveRc({
        ...rcPlan,
        pypi_lookup_error: 'Could not reach PyPI at https://pypi.org/pypi/nyxgpt/json: timed out',
        blockers: ['the next version for the rc channel cannot be resolved'],
      });
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/PyPI lookup failed/)).toBeInTheDocument();
      });
      expect(screen.getByText(/timed out/)).toBeInTheDocument();
    });

    it('keeps rendering the matrix when the publish-plan lookup fails', async () => {
      serveReport(mockReport);
      serveRc({ error: 'pypi unreachable' }, 502);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/Could not load the publish plan/)).toBeInTheDocument();
      });
      // The matrix above it is unaffected -- an unreachable PyPI must not
      // hide which targets are repo-less.
      expect(screen.getByText('Linux native (systemd --user)')).toBeInTheDocument();
    });

    it('falls back to `detail` when a failure carries no `error` field', async () => {
      // FastAPI's own error shape, and the last-resort `HTTP <status>`, must
      // both reach the operator rather than rendering an empty message.
      serveReport(mockReport);
      serveRc({ detail: 'release branch not configured' }, 400);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/release branch not configured/)).toBeInTheDocument();
      });
    });

    it('falls back to the status code when a failure carries no message at all', async () => {
      serveReport(mockReport);
      serveRc({}, 503);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
      });
    });

    it('offers no control to publish -- cutting a build is a terminal command', async () => {
      serveReport(mockReport);
      render(<PortabilityPage />);

      await waitFor(() => {
        expect(screen.getByText('3.0.0rc2')).toBeInTheDocument();
      });
      const buttons = screen.getAllByRole('button');
      expect(buttons).toHaveLength(1);
      expect(buttons[0]).toHaveTextContent(/Refresh/);
    });
  });
});

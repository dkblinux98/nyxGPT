import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import InfrastructurePage from '../../../src/app/admin/infrastructure/page';

const mockStatusTerraform = {
  mode: 'terraform',
  native: {},
  compose: {},
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: true,
    containers: { api: 'running', web: 'running', cassandra: 'exited' },
  },
  kubernetes: {
    available: true,
    probe_available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['pod/nyxgpt-api-abc123   1/1   Running'],
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusEmpty = {
  mode: 'none',
  native: {},
  compose: {},
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: false,
    containers: {},
  },
  kubernetes: {
    available: false,
    probe_available: false,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusCannotDetermine = {
  mode: 'none',
  native: {},
  compose: {},
  conflicts: [],
  terraform: {
    probe_available: false,
    deployed: false,
    containers: { api: 'absent', web: 'absent' },
  },
  kubernetes: {
    available: true,
    probe_available: false,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusKubernetesServing = {
  mode: 'kubernetes',
  native: {},
  compose: {},
  conflicts: [],
  terraform: { probe_available: true, deployed: false, containers: {} },
  kubernetes: {
    available: true,
    probe_available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['pod/nyxgpt-api-stable-abc   1/1   Running'],
  },
  serving: {
    supported: true,
    active: true,
    weight_percent: 20,
    stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '2.0.0-abc123' },
    canary: { state: 'healthy', message: 'nyxgpt-api-canary healthy (1/1 ready)', version: '2.0.1-def456' },
  },
};

describe('InfrastructurePage', () => {
  it('renders the detected mode and terraform/kubernetes status when deployed', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('DEPLOYED')).toHaveLength(2);
    });
    expect(screen.getByRole('heading', { name: 'Terraform' })).toBeInTheDocument();
    expect(screen.getByText('api')).toBeInTheDocument();
    expect(screen.getAllByText('running')).toHaveLength(2);
    expect(screen.getByText('exited')).toBeInTheDocument();
    expect(screen.getByText(/pod\/nyxgpt-api-abc123/)).toBeInTheDocument();
  });

  it('renders empty/not-deployed state and the kubectl-missing hint', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('NOT DEPLOYED')).toHaveLength(1);
    });
    expect(screen.getByText(/kubectl not found/)).toBeInTheDocument();
    expect(screen.getByText('Nothing detected running')).toBeInTheDocument();
  });

  it('renders "cannot determine" instead of a false NOT DEPLOYED when probes fail', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusCannotDetermine)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('CANNOT DETERMINE')).toHaveLength(2);
    });
    expect(screen.queryByText('NOT DEPLOYED')).not.toBeInTheDocument();
    expect(screen.getAllByText(/Cannot determine from this deployment mode/)).toHaveLength(2);
  });

  it('never renders install/destroy controls or api key inputs', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Terraform' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^install$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^destroy$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^remove$/i })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Auth API key/i)).not.toBeInTheDocument();
  });

  it('links back to the admin dashboard and out to the canary page', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    const backLink = await screen.findByRole('link', { name: /back to admin dashboard/i });
    expect(backLink).toHaveAttribute('href', '/admin/dashboard');

    const canaryLink = await screen.findByRole('link', { name: /canary page/i });
    expect(canaryLink).toHaveAttribute('href', '/admin/canary');
  });

  it('states single-instance serving when traffic splitting is unsupported (non-kubernetes mode)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    expect(await screen.findByText(/Single instance serving 100% of traffic/)).toBeInTheDocument();
  });

  it('shows stable/canary weight and health when serving is supported (kubernetes mode)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    expect(await screen.findByText(/Canary rollout active -- 20% of traffic to canary/)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt-api-stable healthy/)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt-api-canary healthy/)).toBeInTheDocument();
  });

  it('surfaces a port conflict warning when native and compose collide', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({ ...mockStatusEmpty, mode: 'native', conflicts: ['api'] })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/Port conflict: api/)).toBeInTheDocument();
    });
  });

  it('walks every load-status error branch, then falls back to String(e) on a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json({ error: 'infra offline' }, { status: 500 })));
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('infra offline')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json({ detail: 'store unreachable' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('store unreachable')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json({}, { status: 503 })));
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

  it('re-polls status via the Refresh status button', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /refresh status/i })).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));
    await user.click(screen.getByRole('button', { name: /refresh status/i }));
    await waitFor(() => {
      expect(screen.getAllByText('DEPLOYED')).toHaveLength(2);
    });
  });
});

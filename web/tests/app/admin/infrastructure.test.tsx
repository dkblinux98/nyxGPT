import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import InfrastructurePage from '../../../src/app/admin/infrastructure/page';

const mockStatusFull = {
  terraform: {
    deployed: true,
    containers: { api: 'running', web: 'running', cassandra: 'exited' },
  },
  kubernetes: {
    available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['pod/nyxgpt-api-abc123   1/1   Running'],
  },
};

const mockStatusEmpty = {
  terraform: {
    deployed: false,
    containers: {},
  },
  kubernetes: {
    available: false,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
  },
};

describe('InfrastructurePage', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('renders terraform and kubernetes status when both are deployed and populated', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('DEPLOYED')).toHaveLength(2);
    });
    expect(screen.getByText('api')).toBeInTheDocument();
    expect(screen.getAllByText('running')).toHaveLength(2);
    expect(screen.getByText('exited')).toBeInTheDocument();
    expect(screen.getByText(/pod\/nyxgpt-api-abc123/)).toBeInTheDocument();
    expect(screen.queryByText(/kubectl not found/)).not.toBeInTheDocument();
  });

  it('renders empty/not-deployed state and the kubectl-missing hint', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('NOT DEPLOYED')).toHaveLength(2);
    });
    expect(screen.getByText(/kubectl not found/)).toBeInTheDocument();
  });

  it('links back to the admin dashboard', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    const backLink = await screen.findByRole('link', { name: /back to admin dashboard/i });
    expect(backLink).toHaveAttribute('href', '/admin/dashboard');
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

  it('honors a declined confirmation for terraform install', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[0]).toBeInTheDocument();
    });

    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);
    expect(screen.queryByText(/OK|FAIL/)).not.toBeInTheDocument();
  });

  it('installs the terraform stack with a supplied api key and shows mixed step results', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[0]).toBeInTheDocument();
    });

    const apiKeyInputs = screen.getAllByPlaceholderText(/Auth API key/i);
    await user.type(apiKeyInputs[0], 'my-secret-key');

    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/infra/terraform/install', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({
          ok: false,
          results: [
            { ok: true, message: 'terraform applied', details: '' },
            { ok: false, message: 'health check failed', details: 'api container exited with code 1' },
          ],
        });
      }),
      http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull))
    );
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);

    await waitFor(() => {
      expect(screen.getByText('terraform applied')).toBeInTheDocument();
    });
    expect(screen.getByText('health check failed')).toBeInTheDocument();
    expect(screen.getByText('api container exited with code 1')).toBeInTheDocument();
    expect(capturedBody).toEqual({ api_key: 'my-secret-key' });
  });

  it('installs the terraform stack with a blank api key (sent as undefined)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[0]).toBeInTheDocument();
    });

    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/infra/terraform/install', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({ ok: true, results: [{ ok: true, message: 'applied', details: '' }] });
      })
    );
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);

    await waitFor(() => {
      expect(screen.getByText('applied')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ api_key: undefined });
  });

  it('walks every terraform-install error fallback branch and a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[0]).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/install', () => HttpResponse.json({ error: 'install failed' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('install failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/install', () => HttpResponse.json({ detail: 'terraform busy' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('terraform busy')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/install', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('terraform gremlin'));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('terraform gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('honors a declined confirmation for terraform destroy, then destroys it when confirmed', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^destroy$/i })[0]).toBeInTheDocument();
    });

    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    expect(screen.queryByText(/destroyed/)).not.toBeInTheDocument();

    global.confirm = vi.fn().mockReturnValue(true);
    server.use(
      http.post('/api/v1/infra/terraform/down', () => HttpResponse.json({ ok: true, results: [{ ok: true, message: 'destroyed', details: '' }] })),
      http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty))
    );
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('destroyed')).toBeInTheDocument();
    });
  });

  it('walks every terraform-down error fallback branch and a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^destroy$/i })[0]).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/down', () => HttpResponse.json({ error: 'destroy failed' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('destroy failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/down', () => HttpResponse.json({ detail: 'terraform locked' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('terraform locked')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/terraform/down', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('destroy gremlin'));
    await user.click(screen.getAllByRole('button', { name: /^destroy$/i })[0]);
    await waitFor(() => {
      expect(screen.getByText('destroy gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('honors a declined confirmation for kubernetes install, then installs with a supplied api key', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[1]).toBeInTheDocument();
    });

    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    expect(screen.queryByText(/deployed/)).not.toBeInTheDocument();

    global.confirm = vi.fn().mockReturnValue(true);
    const apiKeyInputs = screen.getAllByPlaceholderText(/Auth API key/i);
    await user.type(apiKeyInputs[1], 'k8s-secret-key');

    let capturedBody: unknown = null;
    server.use(
      http.post('/api/v1/infra/kubernetes/install', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json({ ok: true, results: [{ ok: true, message: 'deployed', details: '' }] });
      }),
      http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull))
    );
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('deployed')).toBeInTheDocument();
    });
    expect(capturedBody).toEqual({ api_key: 'k8s-secret-key' });
  });

  it('walks every kubernetes-install error fallback branch and a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^install$/i })[1]).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/install', () => HttpResponse.json({ error: 'k8s install failed' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('k8s install failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/install', () => HttpResponse.json({ detail: 'cluster unreachable' }, { status: 500 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('cluster unreachable')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/install', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('kubernetes gremlin'));
    await user.click(screen.getAllByRole('button', { name: /^install$/i })[1]);
    await waitFor(() => {
      expect(screen.getByText('kubernetes gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('honors a declined confirmation for kubernetes remove, then removes it when confirmed', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^remove$/i })).toBeInTheDocument();
    });

    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    expect(screen.queryByText(/removed/)).not.toBeInTheDocument();

    global.confirm = vi.fn().mockReturnValue(true);
    server.use(
      http.post('/api/v1/infra/kubernetes/down', () => HttpResponse.json({ ok: true, results: [{ ok: true, message: 'removed', details: '' }] })),
      http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty))
    );
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.getByText('removed')).toBeInTheDocument();
    });
  });

  it('walks every kubernetes-down error fallback branch and a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^remove$/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/down', () => HttpResponse.json({ error: 'remove failed' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.getByText('remove failed')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/down', () => HttpResponse.json({ detail: 'namespace stuck terminating' }, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.getByText('namespace stuck terminating')).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/infra/kubernetes/down', () => HttpResponse.json({}, { status: 502 })));
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 502')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('kubernetes down gremlin'));
    await user.click(screen.getByRole('button', { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.getByText('kubernetes down gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('refreshes status via the Refresh buttons', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const user = userEvent.setup();
    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^refresh$/i })[0]).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusFull)));
    await user.click(screen.getAllByRole('button', { name: /^refresh$/i })[0]);
    await waitFor(() => {
      expect(screen.getAllByText('DEPLOYED')).toHaveLength(2);
    });
  });
});

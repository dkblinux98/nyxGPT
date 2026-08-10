import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CloudInfrastructurePage from '../../../src/app/admin/cloud-infrastructure/page';

// The SRE/admin surface for `nyxgpt cloud infra` (#3509). The point of these
// tests is that provisioning is reachable from the dashboard (CLAUDE.md's
// Definition of Done) and that the destructive path can't be triggered by a
// single stray click.

const notProvisioned = {
  provisioned: false,
  config_synced: false,
  state_file: '/home/owner/.nyxGPT/cloud/terraform.tfstate',
  state_file_exists: false,
  region: '',
  instance_id: '',
  instance_type: '',
  public_ip: '',
  private_ip: '',
  vpc_id: '',
  security_group_id: '',
  ssh_key_name: '',
  owner_ip_cidr: '',
  access_model: {
    open_ports: [],
    ssh_only: true,
    world_open_ingress: false,
    reachability: 'SSH tunnel to loopback-bound services',
  },
};

const provisioned = {
  ...notProvisioned,
  provisioned: true,
  config_synced: true,
  state_file_exists: true,
  region: 'us-east-1',
  instance_id: 'i-0abc123',
  instance_type: 'm5.large',
  public_ip: '198.51.100.200',
  vpc_id: 'vpc-0abc',
  security_group_id: 'sg-0abc',
  ssh_key_name: 'owner-pair',
  owner_ip_cidr: '198.51.100.7/32',
  access_model: { ...notProvisioned.access_model, open_ports: [22] },
};

function mockStatus(payload: unknown) {
  server.use(http.get('/api/v1/cloud/infra', () => HttpResponse.json(payload)));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CloudInfrastructurePage', () => {
  it('reports when nothing is provisioned yet', async () => {
    mockStatus(notProvisioned);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('not provisioned')).toBeInTheDocument();
    expect(screen.getByText('none')).toBeInTheDocument();
  });

  it('shows the provisioned substrate and its single open port', async () => {
    mockStatus(provisioned);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('provisioned')).toBeInTheDocument();
    expect(screen.getByText('i-0abc123')).toBeInTheDocument();
    expect(screen.getByText('sg-0abc')).toBeInTheDocument();
    expect(screen.getByText('198.51.100.7/32')).toBeInTheDocument();
    expect(screen.getByText('22')).toBeInTheDocument();
  });

  it('plans without creating anything and posts only the filled-in inputs', async () => {
    mockStatus(notProvisioned);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/infra/plan', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ action: 'plan', settings: { aws_region: 'eu-west-2' } });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    await userEvent.type(screen.getByLabelText('AWS region'), 'eu-west-2');
    await userEvent.click(screen.getByRole('button', { name: 'Plan' }));

    await waitFor(() => expect(screen.getByText(/Nothing was created/)).toBeInTheDocument());
    expect(body).toEqual({ region: 'eu-west-2' });
  });

  it('applies with every provisioning input and reports the created instance', async () => {
    mockStatus(notProvisioned);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/infra/apply', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          action: 'apply',
          outputs: { instance_id: 'i-0abc123', region: 'us-east-1' },
        });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    await userEvent.type(screen.getByLabelText('AWS region'), 'us-east-1');
    await userEvent.type(screen.getByLabelText('Instance type'), 'm5.large');
    await userEvent.type(screen.getByLabelText('Existing EC2 key pair'), 'owner-pair');
    await userEvent.type(
      screen.getByLabelText(/public key file to register/),
      '~/.ssh/id_ed25519.pub'
    );
    await userEvent.type(screen.getByLabelText(/SSH source IP\/CIDR/), '198.51.100.7/32');
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(screen.getByText(/Instance i-0abc123 in us-east-1/)).toBeInTheDocument()
    );
    // Every field the operator filled in has to reach the backend -- a dropped
    // owner_ip would silently provision against the auto-detected IP instead.
    expect(body).toEqual({
      region: 'us-east-1',
      instance_type: 'm5.large',
      ssh_key_name: 'owner-pair',
      ssh_public_key: '~/.ssh/id_ed25519.pub',
      owner_ip: '198.51.100.7/32',
    });
  });

  it('does not fabricate instance details when apply returns no outputs', async () => {
    mockStatus(notProvisioned);
    server.use(
      http.post('/api/v1/cloud/infra/apply', () => HttpResponse.json({ action: 'apply' }))
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(
        screen.getByText(/Instance unknown in the configured region/)
      ).toBeInTheDocument()
    );
  });

  it('surfaces a failed initial status load instead of rendering an empty substrate', async () => {
    server.use(
      http.get('/api/v1/cloud/infra', () => HttpResponse.json(null, { status: 500 }))
    );

    render(<CloudInfrastructurePage />);

    // No `error` / `detail` in the body, so the HTTP status is the fallback.
    expect(await screen.findByText(/HTTP 500/)).toBeInTheDocument();
    expect(screen.getByText('not provisioned')).toBeInTheDocument();
    expect(screen.getByText('none')).toBeInTheDocument();
  });

  it('surfaces a non-Error rejection from the status fetch', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('network exploded');

    render(<CloudInfrastructurePage />);

    expect(await screen.findByText(/network exploded/)).toBeInTheDocument();
    spy.mockRestore();
  });

  it.each([
    ['a bare string error', { error: 'terraform plan failed' }, 400, /terraform plan failed/],
    ['a FastAPI detail', { detail: 'no AWS credentials found' }, 400, /no AWS credentials found/],
    ['an unrecognised shape', { unexpected: true }, 500, /HTTP 500/],
    ['an error object without a message', { error: { code: 7 } }, 503, /HTTP 503/],
  ])('reports %s from a failed plan', async (_label, payload, status, expected) => {
    mockStatus(notProvisioned);
    server.use(
      http.post('/api/v1/cloud/infra/plan', () => HttpResponse.json(payload, { status }))
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    await userEvent.click(screen.getByRole('button', { name: 'Plan' }));

    await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument());
  });

  it('surfaces a non-Error rejection from an action', async () => {
    mockStatus(notProvisioned);

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('plan exploded');
    await userEvent.click(screen.getByRole('button', { name: 'Plan' }));

    await waitFor(() => expect(screen.getByText(/plan exploded/)).toBeInTheDocument());
    spy.mockRestore();
  });

  it('surfaces a failed apply instead of claiming success', async () => {
    mockStatus(notProvisioned);
    server.use(
      http.post('/api/v1/cloud/infra/apply', () =>
        HttpResponse.json(
          { error: { message: 'No SSH key configured' } },
          { status: 409 }
        )
      )
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('not provisioned');

    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(screen.getByText(/No SSH key configured/)).toBeInTheDocument()
    );
  });

  it('keeps destroy disabled until DESTROY is typed', async () => {
    mockStatus(provisioned);
    render(<CloudInfrastructurePage />);
    await screen.findByText('provisioned');

    const destroy = screen.getByRole('button', { name: 'Destroy' });
    expect(destroy).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Type DESTROY to confirm'), 'destroy');
    expect(destroy).toBeDisabled();

    await userEvent.clear(screen.getByLabelText('Type DESTROY to confirm'));
    await userEvent.type(screen.getByLabelText('Type DESTROY to confirm'), 'DESTROY');
    expect(destroy).toBeEnabled();
  });

  it('confirms the teardown explicitly to the backend', async () => {
    mockStatus(provisioned);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/infra/destroy', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ action: 'destroy', settings: {} });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('provisioned');

    await userEvent.type(screen.getByLabelText('Type DESTROY to confirm'), 'DESTROY');
    await userEvent.click(screen.getByRole('button', { name: 'Destroy' }));

    await waitFor(() => expect(screen.getByText('Substrate destroyed.')).toBeInTheDocument());
    expect(body).toEqual({ confirm: true });
  });
});

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

// Terraform remote state (#3510). The panel is a second, independent
// subsystem on the same page: its own status fetch, its own failures.
const localBackend = {
  backend: 'local',
  remote_enabled: false,
  bootstrapped: false,
  bucket: '',
  table: '',
  key: '',
  region: '',
  locking: 'none (local file)',
  local_state_file: '/home/owner/.nyxGPT/cloud/terraform.tfstate',
  local_state_exists: true,
};

const remoteBackend = {
  ...localBackend,
  backend: 's3',
  remote_enabled: true,
  bootstrapped: true,
  bucket: 'nyxgpt-tfstate-0abc',
  table: 'nyxgpt-tfstate-locks',
  key: 'aws/terraform.tfstate',
  region: 'us-east-1',
  locking: 'DynamoDB',
  local_state_exists: false,
};

const twoVersions = {
  versions: [
    { version_id: 'ver-newest', last_modified: '2026-08-10T05:00:00Z', size: 4096, latest: true },
    { version_id: 'ver-older', last_modified: '2026-08-09T05:00:00Z', size: 4000, latest: false },
  ],
};

function mockStatus(payload: unknown) {
  server.use(http.get('/api/v1/cloud/infra', () => HttpResponse.json(payload)));
}

function mockStateStatus(payload: unknown, status = 200) {
  server.use(http.get('/api/v1/cloud/state', () => HttpResponse.json(payload, { status })));
}

beforeEach(() => {
  vi.clearAllMocks();
  // The page loads the state backend on mount, so every test needs this
  // answered -- MSW is set to error on unhandled requests. Individual tests
  // override it with a later `server.use`.
  mockStateStatus(localBackend);
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

// The Terraform remote-state panel (#3510). Local state is invisible to a
// second operator and unlocked; these tests cover getting off it, and the
// recovery paths that exist because remote state can go wrong in ways a local
// file cannot -- a lock nobody released, a state written wrong.
describe('CloudInfrastructurePage — Terraform state', () => {
  it('reports local state and offers the migration', async () => {
    mockStatus(provisioned);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('local file')).toBeInTheDocument();
    expect(
      screen.getByText('/home/owner/.nyxGPT/cloud/terraform.tfstate')
    ).toBeInTheDocument();
    expect(screen.getByText(/State is a single local file on this machine/)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Migrate to S3 + DynamoDB' })
    ).toBeInTheDocument();
    // Recovery only applies to remote state -- nothing to list or unlock yet.
    expect(screen.queryByRole('button', { name: 'List versions' })).not.toBeInTheDocument();
  });

  it('migrates to the remote backend and names where state now lives', async () => {
    mockStatus(provisioned);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/state/migrate', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          backend: {
            bucket: 'nyxgpt-tfstate-0abc',
            key: 'aws/terraform.tfstate',
            table: 'nyxgpt-tfstate-locks',
          },
        });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('local file');

    await userEvent.click(screen.getByRole('button', { name: 'Migrate to S3 + DynamoDB' }));

    await waitFor(() =>
      expect(
        screen.getByText(/s3:\/\/nyxgpt-tfstate-0abc\/aws\/terraform.tfstate/)
      ).toBeInTheDocument()
    );
    expect(body).toEqual({});
  });

  it('does not fabricate backend details when migrate returns none', async () => {
    mockStatus(provisioned);
    server.use(http.post('/api/v1/cloud/state/migrate', () => HttpResponse.json({})));

    render(<CloudInfrastructurePage />);
    await screen.findByText('local file');

    await userEvent.click(screen.getByRole('button', { name: 'Migrate to S3 + DynamoDB' }));

    await waitFor(() =>
      expect(screen.getByText(/Remote state active: s3:\/\/undefined/)).toBeInTheDocument()
    );
  });

  it('shows the remote backend and its lock table once migrated', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('S3 + DynamoDB lock')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt-tfstate-0abc')).toBeInTheDocument();
    expect(screen.getByText('aws/terraform.tfstate')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt-tfstate-locks')).toBeInTheDocument();
    expect(screen.getByText('DynamoDB')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Migrate to S3 + DynamoDB' })
    ).not.toBeInTheDocument();
  });

  it('renders empty backend fields rather than "undefined" when the payload omits them', async () => {
    mockStatus(provisioned);
    mockStateStatus({ remote_enabled: true });
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('S3 + DynamoDB lock')).toBeInTheDocument();
    // Backend, Locking, Bucket, Object key, Lock table, Region -- all blank.
    expect(screen.getAllByText('—')).toHaveLength(6);
  });

  it('lists stored versions, newest first and marked as current', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    server.use(http.get('/api/v1/cloud/state/versions', () => HttpResponse.json(twoVersions)));

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');

    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));

    expect(await screen.findByText('ver-newest')).toBeInTheDocument();
    expect(screen.getByText(/4096 bytes · current/)).toBeInTheDocument();
    expect(screen.getByText('ver-older')).toBeInTheDocument();

    // Restoring the version that is already current is a no-op, so it is not
    // offered; the older one is.
    const restoreButtons = screen.getAllByRole('button', { name: 'Restore' });
    expect(restoreButtons).toHaveLength(2);
    expect(restoreButtons[0]).toBeDisabled();
    expect(restoreButtons[1]).toBeEnabled();
  });

  it('says so when the bucket holds no versions yet', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    server.use(http.get('/api/v1/cloud/state/versions', () => HttpResponse.json({})));

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');

    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));

    expect(await screen.findByText(/No stored versions yet/)).toBeInTheDocument();
  });

  it('reports a failed version listing instead of an empty history', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    server.use(
      http.get('/api/v1/cloud/state/versions', () =>
        HttpResponse.json({ detail: 'bucket versioning is disabled' }, { status: 409 })
      )
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');

    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));

    await waitFor(() =>
      expect(screen.getByText(/bucket versioning is disabled/)).toBeInTheDocument()
    );
    // "No stored versions yet" would read as "nothing to recover" -- which is
    // the opposite of what a failed listing means.
    expect(screen.queryByText(/No stored versions yet/)).not.toBeInTheDocument();
  });

  it('never restores on a single click -- the confirmation is a separate button', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    let restoreCalls = 0;
    server.use(
      http.get('/api/v1/cloud/state/versions', () => HttpResponse.json(twoVersions)),
      http.post('/api/v1/cloud/state/restore', () => {
        restoreCalls += 1;
        return HttpResponse.json({});
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');
    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));
    await screen.findByText('ver-older');

    await userEvent.click(screen.getAllByRole('button', { name: 'Restore' })[1]);

    // Selecting a version must not send anything: the API's `confirm` guard
    // exists because a later apply against the wrong state destroys resources.
    expect(restoreCalls).toBe(0);
    expect(screen.getByRole('button', { name: 'Confirm restore' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(restoreCalls).toBe(0);
    expect(screen.queryByRole('button', { name: 'Confirm restore' })).not.toBeInTheDocument();
  });

  it('sends the confirmation only on the second, explicit click', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get('/api/v1/cloud/state/versions', () => HttpResponse.json(twoVersions)),
      http.post('/api/v1/cloud/state/restore', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ version_id: 'ver-older' });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');
    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));
    await screen.findByText('ver-older');

    await userEvent.click(screen.getAllByRole('button', { name: 'Restore' })[1]);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm restore' }));

    await waitFor(() =>
      expect(screen.getByText(/Restored version ver-older as the current state/)).toBeInTheDocument()
    );
    expect(body).toEqual({ version_id: 'ver-older', confirm: true });
    // The list is stale after a restore -- it is cleared rather than left
    // showing which version "was" current.
    expect(screen.queryByText('ver-older')).not.toBeInTheDocument();
  });

  it('drops an armed restore when the version list is reloaded', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    server.use(http.get('/api/v1/cloud/state/versions', () => HttpResponse.json(twoVersions)));

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');
    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));
    await screen.findByText('ver-older');

    await userEvent.click(screen.getAllByRole('button', { name: 'Restore' })[1]);
    expect(screen.getByRole('button', { name: 'Confirm restore' })).toBeInTheDocument();

    // A refreshed list can put a different version in that row.
    await userEvent.click(screen.getByRole('button', { name: 'List versions' }));

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Confirm restore' })).not.toBeInTheDocument()
    );
  });

  it('keeps force-unlock disabled until a lock id is supplied', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');

    expect(screen.getByRole('button', { name: 'Force unlock' })).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Lock ID'), '   ');
    expect(screen.getByRole('button', { name: 'Force unlock' })).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Lock ID'), 'abc-123');
    expect(screen.getByRole('button', { name: 'Force unlock' })).toBeEnabled();
  });

  it('releases the named lock and clears the field', async () => {
    mockStatus(provisioned);
    mockStateStatus(remoteBackend);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/state/unlock', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ unlocked: true });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('S3 + DynamoDB lock');

    await userEvent.type(screen.getByLabelText('Lock ID'), '  abc-123  ');
    await userEvent.click(screen.getByRole('button', { name: 'Force unlock' }));

    await waitFor(() =>
      expect(screen.getByText('Released lock abc-123.')).toBeInTheDocument()
    );
    expect(body).toEqual({ lock_id: 'abc-123' });
    expect(screen.getByLabelText('Lock ID')).toHaveValue('');
  });

  it('keeps a state failure out of the substrate panel, and vice versa', async () => {
    server.use(
      http.get('/api/v1/cloud/infra', () =>
        HttpResponse.json({ error: 'terraform not installed' }, { status: 503 })
      )
    );
    mockStateStatus({ detail: 'no AWS credentials found' }, 409);

    render(<CloudInfrastructurePage />);

    // Both failures survive: whichever request finished last used to overwrite
    // the other panel's error, hiding it.
    expect(await screen.findByText(/terraform not installed/)).toBeInTheDocument();
    expect(await screen.findByText(/no AWS credentials found/)).toBeInTheDocument();
  });

  it('falls back to the HTTP status when a failed state load carries no message', async () => {
    mockStatus(provisioned);
    mockStateStatus(null, 500);

    render(<CloudInfrastructurePage />);

    expect(await screen.findByText(/HTTP 500/)).toBeInTheDocument();
    // The substrate panel still rendered its own data.
    expect(screen.getByText('i-0abc123')).toBeInTheDocument();
  });

  it('surfaces a failed state action without claiming success', async () => {
    mockStatus(provisioned);
    server.use(
      http.post('/api/v1/cloud/state/migrate', () =>
        HttpResponse.json({ error: { message: 'bucket name already taken' } }, { status: 409 })
      )
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('local file');

    await userEvent.click(screen.getByRole('button', { name: 'Migrate to S3 + DynamoDB' }));

    await waitFor(() =>
      expect(screen.getByText(/bucket name already taken/)).toBeInTheDocument()
    );
    expect(screen.queryByText(/Remote state active/)).not.toBeInTheDocument();
  });

  it('surfaces a non-Error rejection from a state action', async () => {
    mockStatus(provisioned);

    render(<CloudInfrastructurePage />);
    await screen.findByText('local file');

    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('migrate exploded');
    await userEvent.click(screen.getByRole('button', { name: 'Migrate to S3 + DynamoDB' }));

    await waitFor(() => expect(screen.getByText(/migrate exploded/)).toBeInTheDocument());
    spy.mockRestore();
  });

  it('surfaces a non-Error rejection from the state status load', async () => {
    mockStatus(provisioned);
    const realFetch = globalThis.fetch;
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url === '/api/v1/cloud/state') return Promise.reject('state fetch exploded');
      return realFetch(input, init);
    });

    render(<CloudInfrastructurePage />);

    expect(await screen.findByText(/state fetch exploded/)).toBeInTheDocument();
    spy.mockRestore();
  });
});

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

// The deployment itself (#3513): what release is on the instance, and whether
// the SSH tunnel that is the only way to reach it is open.
const lifecycleCommands = {
  deploy: 'nyxgpt cloud deploy',
  redeploy: 'nyxgpt cloud deploy',
  destroy: 'nyxgpt cloud destroy --yes',
  tunnel: 'nyxgpt cloud tunnel',
  tunnel_stop: 'nyxgpt cloud tunnel --stop',
  status: 'nyxgpt cloud deploy --status',
  allow_ip: 'nyxgpt cloud allow-ip',
};

const notDeployed = {
  deployed: false,
  version: '',
  host: '',
  instance_id: '',
  region: '',
  profiles: [],
  tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
  health: { checked: false, healthy: false, status: 0, reason: '' },
  history: [],
  urls: { api: 'http://localhost:8000', web: 'http://localhost:3000' },
  access_command: 'nyxgpt cloud tunnel',
  commands: lifecycleCommands,
};

const deployedTunnelClosed = {
  ...notDeployed,
  deployed: true,
  version: '3.0.0',
  host: '198.51.100.200',
  instance_id: 'i-0abc123',
  region: 'us-east-1',
  profiles: ['monitoring', 'logging', 'tracing', 'errors'],
  urls: {
    api: 'http://localhost:8000',
    web: 'http://localhost:3000',
    grafana: 'http://localhost:3001',
  },
};

const deployedTunnelOpen = {
  ...deployedTunnelClosed,
  tunnel: {
    running: true,
    pid: 4242,
    host: '198.51.100.200',
    profiles: deployedTunnelClosed.profiles,
    urls: deployedTunnelClosed.urls,
  },
  health: {
    checked: true,
    healthy: true,
    status: 200,
    url: 'http://localhost:8000/health',
    reason: '',
  },
};

// Two lifecycle events, newest first, as the backend returns them (#3514).
// The failed one matters most: it is what an operator comes to this panel to
// reconstruct.
const withHistory = {
  ...deployedTunnelOpen,
  history: [
    {
      ts: 1754800000,
      action: 'deploy',
      outcome: 'succeeded',
      version: '3.0.0',
      instance_id: 'i-0abc123',
      detail: 'healthy over the access tunnel',
    },
    {
      ts: 1754700000,
      action: 'deploy',
      outcome: 'failed',
      version: '2.9.0',
      instance_id: 'i-0abc123',
      detail: 'stack installed but http://localhost:8000/health never returned 200 within 900s',
    },
  ],
};

function mockDeployStatus(payload: unknown) {
  server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(payload)));
}

beforeEach(() => {
  vi.clearAllMocks();
  // The page loads the state backend and the deployment on mount, so every
  // test needs both answered -- MSW is set to error on unhandled requests.
  // Individual tests override them with a later `server.use`.
  mockStateStatus(localBackend);
  mockDeployStatus(notDeployed);
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

  it('sends every filled-in provisioning input to the plan', async () => {
    mockStatus(notProvisioned);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/infra/plan', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ action: 'plan', settings: { aws_region: 'us-east-1' } });
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
    await userEvent.click(screen.getByRole('button', { name: 'Plan' }));

    await waitFor(() => expect(screen.getByText(/Nothing was created/)).toBeInTheDocument());
    // Every field the operator filled in has to reach the backend -- a dropped
    // owner_ip would silently plan against the auto-detected IP instead.
    expect(body).toEqual({
      region: 'us-east-1',
      instance_type: 'm5.large',
      ssh_key_name: 'owner-pair',
      ssh_public_key: '~/.ssh/id_ed25519.pub',
      owner_ip: '198.51.100.7/32',
    });
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

  it('offers no control that creates or destroys cloud resources', async () => {
    // The owner's 2026-08-09 decision on #3514: the cloud page is
    // status-plus-CLI-pointers, so a stray click can never provision or
    // delete billed infrastructure. Asserted as an absence because that is
    // exactly what the decision buys.
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelOpen);
    render(<CloudInfrastructurePage />);
    await screen.findByText('provisioned');

    expect(screen.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deploy' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Destroy' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Type DESTROY to confirm')).not.toBeInTheDocument();
    // Plan survives: it reports what would change and creates nothing.
    expect(screen.getByRole('button', { name: 'Plan' })).toBeInTheDocument();
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
    // The state panel's Backend, Locking, Bucket, Object key, Lock table and
    // Region are all blank, as are the deployment panel's version, instance,
    // region and profiles on an undeployed substrate -- ten placeholders, and
    // not a single literal "undefined" anywhere on the page.
    expect(screen.getAllByText('—')).toHaveLength(10);
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
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

// The deployment panel (P6-11, #3513; reconciled to status-only by P6-15,
// #3514). The point of these tests is that an operator can see the whole
// deployment -- what release is on the box, whether it answers, what has
// happened to it, and how to change it -- without the page being able to
// change it, and that nothing ever advertises an instance-facing URL.
describe('CloudInfrastructurePage deployment panel', () => {
  it('reports nothing deployed on a fresh substrate', async () => {
    mockStatus(notProvisioned);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('not deployed')).toBeInTheDocument();
    // Opening a tunnel to a box with nothing on it is meaningless.
    expect(screen.getByRole('button', { name: 'Open tunnel' })).toBeDisabled();
  });

  it('asks the backend for a real health answer rather than assuming one', async () => {
    mockStatus(provisioned);
    let requestedUrl = '';
    server.use(
      http.get('/api/v1/cloud/deploy', ({ request }) => {
        requestedUrl = request.url;
        return HttpResponse.json(deployedTunnelOpen);
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    expect(requestedUrl).toContain('probe_health=true');
    expect(screen.getByText(/healthy \(HTTP 200 over the tunnel\)/)).toBeInTheDocument();
  });

  it('distinguishes "not checked" from "unhealthy"', async () => {
    // A closed tunnel means unreachable from here, which says nothing about
    // whether the stack is running on the instance. Collapsing the two would
    // send an operator chasing an outage that isn't happening.
    mockStatus(provisioned);
    mockDeployStatus({
      ...deployedTunnelClosed,
      health: {
        checked: false,
        healthy: false,
        status: 0,
        reason: 'no access tunnel is open, so the instance is not reachable from here',
      },
    });
    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    expect(screen.getByText(/not checked — no access tunnel is open/)).toBeInTheDocument();
    expect(screen.queryByText(/unhealthy/)).not.toBeInTheDocument();
  });

  it('reports an unhealthy stack with the status it actually got', async () => {
    mockStatus(provisioned);
    mockDeployStatus({
      ...deployedTunnelOpen,
      health: {
        checked: true,
        healthy: false,
        status: 503,
        url: 'http://localhost:8000/health',
        reason: 'the tunneled API did not answer with 200',
      },
    });
    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    expect(screen.getByText(/unhealthy — HTTP 503 over the tunnel/)).toBeInTheDocument();
  });

  it('re-reads the deployment state on an explicit refresh', async () => {
    mockStatus(provisioned);
    let calls = 0;
    server.use(
      http.get('/api/v1/cloud/deploy', () => {
        calls += 1;
        return HttpResponse.json(deployedTunnelOpen);
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');
    expect(calls).toBe(1);

    await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));

    await waitFor(() => expect(calls).toBe(2));
  });

  it('shows every deploy and teardown, newest first, including the failures', async () => {
    mockStatus(provisioned);
    mockDeployStatus(withHistory);
    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    expect(screen.getByText(/deploy 3.0.0 · succeeded/)).toBeInTheDocument();
    expect(screen.getByText(/deploy 2.9.0 · failed/)).toBeInTheDocument();
    expect(screen.getByText(/never returned 200 within 900s/)).toBeInTheDocument();

    const outcomes = screen.getAllByText(/^(succeeded|failed)$/).map((el) => el.textContent);
    expect(outcomes).toEqual(['succeeded', 'failed']);
  });

  it('says so plainly when nothing has ever been deployed from this machine', async () => {
    mockStatus(notProvisioned);
    render(<CloudInfrastructurePage />);
    await screen.findByText('not deployed');

    expect(
      screen.getByText('Nothing has been deployed or torn down from this machine yet.')
    ).toBeInTheDocument();
  });

  it('points at the wrapped CLI commands that own the lifecycle', async () => {
    // Rendered from the backend's own LIFECYCLE_COMMANDS, so the page cannot
    // drift from what the CLI accepts -- and every one is a `nyxgpt` command,
    // never a raw docker/terraform one (CLAUDE.md).
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelOpen);
    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    expect(screen.getAllByText('nyxgpt cloud deploy').length).toBeGreaterThan(0);
    expect(screen.getByText('nyxgpt cloud destroy --yes')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud deploy --status')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud allow-ip')).toBeInTheDocument();
  });

  it('shows the installed release and the localhost-only URLs once deployed', async () => {
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelOpen);
    render(<CloudInfrastructurePage />);

    expect(await screen.findByText('deployed')).toBeInTheDocument();
    expect(screen.getByText('3.0.0')).toBeInTheDocument();
    expect(screen.getByText('open (pid 4242)')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:3001')).toBeInTheDocument();
    // The instance's own address must never be offered as a way in.
    expect(screen.queryByText(/http:\/\/198\.51\.100\.200/)).not.toBeInTheDocument();
  });

  it('opens the access tunnel on request', async () => {
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelClosed);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/deploy/tunnel', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ action: 'tunnel', running: true, pid: 4242, urls: {} });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    await userEvent.click(screen.getByRole('button', { name: 'Open tunnel' }));

    await waitFor(() => expect(screen.getByText(/Access tunnel open/)).toBeInTheDocument());
    expect(body).toEqual({ action: 'start' });
  });

  it('closes the access tunnel and says what that costs', async () => {
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelOpen);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/cloud/deploy/tunnel', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ action: 'tunnel-stop', stopped: true, pid: 4242 });
      })
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    await userEvent.click(screen.getByRole('button', { name: 'Close tunnel' }));

    await waitFor(() =>
      expect(screen.getByText(/Nothing on the instance is reachable/)).toBeInTheDocument()
    );
    expect(body).toEqual({ action: 'stop' });
  });

  it('says so when the deployment status cannot be read on mount', async () => {
    mockStatus(provisioned);
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ detail: 'deploy state unreadable' }, { status: 500 })
      )
    );

    render(<CloudInfrastructurePage />);

    // A deployment panel that silently showed "not deployed" here would be
    // claiming the instance is empty on no evidence at all.
    expect(await screen.findByText(/deploy state unreadable/)).toBeInTheDocument();
    expect(screen.getByText('provisioned')).toBeInTheDocument();
  });

  it('surfaces a failed tunnel request rather than claiming the access state changed', async () => {
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelClosed);
    server.use(
      http.post('/api/v1/cloud/deploy/tunnel', () =>
        HttpResponse.json({ detail: 'bind: Address already in use' }, { status: 409 })
      )
    );

    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    await userEvent.click(screen.getByRole('button', { name: 'Open tunnel' }));

    await waitFor(() =>
      expect(screen.getByText(/Address already in use/)).toBeInTheDocument()
    );
    expect(screen.queryByText(/Access tunnel open/)).not.toBeInTheDocument();
  });

  it('falls back to String(e) on a non-Error rejection from the deployment status load', async () => {
    mockStatus(provisioned);
    const realFetch = globalThis.fetch;
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : String(input);
      if (url.startsWith('/api/v1/cloud/deploy')) return Promise.reject('deploy fetch exploded');
      return realFetch(input, init);
    });

    render(<CloudInfrastructurePage />);

    expect(await screen.findByText(/deploy fetch exploded/)).toBeInTheDocument();
    spy.mockRestore();
  });

  it('falls back to String(e) on a non-Error rejection from a tunnel request', async () => {
    mockStatus(provisioned);
    mockDeployStatus(deployedTunnelClosed);
    render(<CloudInfrastructurePage />);
    await screen.findByText('deployed');

    const spy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce('tunnel exploded');
    await userEvent.click(screen.getByRole('button', { name: 'Open tunnel' }));

    await waitFor(() => expect(screen.getByText(/tunnel exploded/)).toBeInTheDocument());
    spy.mockRestore();
  });

  it('keeps a deployment failure out of the substrate panel', async () => {
    mockStatus(provisioned);
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ detail: 'deploy state unreadable' }, { status: 409 })
      )
    );

    render(<CloudInfrastructurePage />);

    await waitFor(() =>
      expect(screen.getByText(/deploy state unreadable/)).toBeInTheDocument()
    );
    // The substrate panel is a separate subsystem and must be untouched.
    expect(screen.getByText('provisioned')).toBeInTheDocument();
  });
});

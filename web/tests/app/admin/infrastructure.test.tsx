import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import {
  CLOUD_DEPLOY_UNKNOWN,
  CLOUD_LIFECYCLE_COMMANDS,
  CLOUD_STATE_LOCAL,
} from '../../mocks/handlers';
import InfrastructurePage from '../../../src/app/admin/infrastructure/page';

// The in-cluster observability layer (#3787) the api reports under
// `kubernetes.observability`. Undeployed by default: most fixtures describe a
// cluster running the app tier only.
const observabilityAbsent = {
  probe_available: true,
  deployed: false,
  workloads: {},
  port_forward_command: 'nyxgpt ops port-forward --target observability',
};

const observabilityDeployed = {
  probe_available: true,
  deployed: true,
  workloads: {
    prometheus: '1/1 ready',
    grafana: '1/1 ready',
    loki: '1/1 ready',
    jaeger: '1/1 ready',
    glitchtip: 'absent',
    promtail: '1/1 ready',
  },
  port_forward_command: 'nyxgpt ops port-forward --target observability',
  // Two commands since #3986, which is what the live api sends: the forward is
  // the bring-your-own answer, and this one puts a stripped published port
  // back. Without the field here the branch the api actually takes was never
  // rendered by any test.
  publish_command: 'nyxgpt ops observability --kubernetes',
};

// The same deployed tier as reported by an api predating #3986 -- the field is
// optional on the client for exactly this version skew, and the sentence has to
// end cleanly rather than trailing `undefined` or a dangling "; if".
const observabilityDeployedWithoutPublishCommand = {
  ...observabilityDeployed,
  publish_command: undefined,
};

const mockStatusTerraform = {
  mode: 'terraform',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: true,
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: true,
    containers: { api: 'running', web: 'running', cassandra: 'exited' },
  },
  kubernetes: {
    available: true,
    configured: true,
    probe_available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['pod/nyxgpt-api-abc123   1/1   Running'],
    context: 'docker-desktop',
    provisioned: false,
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusEmpty = {
  mode: 'none',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: true,
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: false,
    containers: {},
  },
  kubernetes: {
    available: false,
    configured: false,
    probe_available: true,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
    context: '',
    provisioned: false,
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusKubernetesNotConfigured = {
  mode: 'none',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: true,
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: false,
    containers: {},
  },
  kubernetes: {
    available: true,
    configured: false,
    probe_available: true,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
    context: '',
    provisioned: false,
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

// The payload the api Pod itself produces (#3988): no kubeconfig context --
// a Pod has none -- but in-cluster ServiceAccount credentials, Pods found, and
// the Compose/native rows explicitly scoped out rather than answered against
// the container's own filesystem.
const mockStatusInCluster = {
  mode: 'kubernetes',
  in_cluster: true,
  install_mode: {
    mode: 'artifact',
    checkout: null,
    label: 'artifact (published/vendored build)',
    components: ['api', 'web'],
    identity: { known: false, manager: '', services: {}, version: '', channel: '', detail: '' },
    in_scope: false,
    out_of_scope_reason:
      'Not in scope from here: this API is running inside a Kubernetes Pod. The Kubernetes card above describes this deployment; a native install is a separate one, on a host this process cannot see.',
  },
  native: {},
  compose: {},
  compose_probe_available: false,
  compose_probe_reason:
    'Not in scope from here: this API is running inside a Kubernetes Pod, which has no host filesystem and no Docker socket. Run `nyxgpt ops status` on the host to survey a Docker Compose deployment there.',
  conflicts: [],
  terraform: { probe_available: true, deployed: false, containers: {} },
  kubernetes: {
    available: true,
    configured: true,
    probe_available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['nyxgpt-api-stable-1   1/1 Running'],
    pod_states: [
      { name: 'nyxgpt-api-stable-1', state: 'ready', summary: '1/1 Running', details: '' },
    ],
    context: 'in-cluster (ServiceAccount)',
    provisioned: false,
    in_cluster: true,
    install_mode: { mode: 'artifact', checkout: null, label: 'artifact', recorded: true },
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic.',
  },
};

const mockStatusCannotDetermine = {
  mode: 'none',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: true,
  conflicts: [],
  terraform: {
    probe_available: false,
    deployed: false,
    containers: { api: 'absent', web: 'absent' },
  },
  kubernetes: {
    available: true,
    configured: true,
    probe_available: false,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
    context: 'kind-nyxgpt-local',
    provisioned: true,
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusComposeCannotDetermine = {
  mode: 'terraform',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: false,
  compose_probe_reason:
    '`docker compose ps` exited 125: permission denied while trying to connect to the Docker daemon socket',
  conflicts: [],
  terraform: {
    probe_available: true,
    deployed: true,
    containers: { api: 'running', web: 'running', cassandra: 'exited' },
  },
  kubernetes: {
    available: true,
    configured: true,
    probe_available: true,
    deployed: false,
    namespace: 'nyxgpt',
    pods: [],
    context: 'kind-nyxgpt-local',
    provisioned: true,
    observability: observabilityAbsent,
  },
  serving: {
    supported: false,
    message: 'Single instance serving 100% of traffic -- traffic splitting is a Kubernetes-mode feature (see the Canary page).',
  },
};

const mockStatusKubernetesServing = {
  mode: 'kubernetes',
  native: {},
  native_probe_available: true,
  compose: {},
  compose_probe_available: true,
  conflicts: [],
  terraform: { probe_available: true, deployed: false, containers: {} },
  kubernetes: {
    available: true,
    configured: true,
    probe_available: true,
    deployed: true,
    namespace: 'nyxgpt',
    pods: ['pod/nyxgpt-api-stable-abc   1/1   Running'],
    context: 'kind-nyxgpt-local',
    provisioned: true,
    observability: observabilityAbsent,
  },
  serving: {
    supported: true,
    active: true,
    weight_percent: 20,
    stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '2.0.0-abc123' },
    canary: { state: 'healthy', message: 'nyxgpt-api-canary healthy (1/1 ready)', version: '2.0.1-def456' },
    components: {
      api: {
        active: true,
        weight_percent: 20,
        stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '2.0.0-abc123' },
        canary: { state: 'healthy', message: 'nyxgpt-api-canary healthy (1/1 ready)', version: '2.0.1-def456' },
      },
      web: {
        active: false,
        weight_percent: 0,
        stable: { state: 'healthy', message: 'nyxgpt-web-stable healthy (4/4 ready)', version: '2.0.0-abc123' },
        canary: { state: 'not_deployed', message: 'nyxgpt-web-canary has 0 desired replicas (idle)', version: '' },
      },
    },
  },
};

describe('InfrastructurePage', () => {
  it('renders an inactive rollout with version-less tracks and an empty reachable cluster', async () => {
    // Covers the serving box's no-active-rollout branch, the version-less
    // track rendering, and the reachable-but-not-deployed kubernetes card
    // (NOT DEPLOYED badge + empty-pods message).
    const inactiveServing = {
      ...mockStatusKubernetesServing,
      kubernetes: {
        available: true,
        configured: true,
        probe_available: true,
        deployed: false,
        namespace: 'nyxgpt',
        pods: [],
      },
      serving: {
        supported: true,
        active: false,
        weight_percent: 0,
        stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '' },
        canary: { state: 'not_deployed', message: 'nyxgpt-api-canary not deployed', version: '' },
        components: {
          api: {
            active: false,
            weight_percent: 0,
            stable: { state: 'healthy', message: 'nyxgpt-api-stable healthy (4/4 ready)', version: '' },
            canary: { state: 'not_deployed', message: 'nyxgpt-api-canary not deployed', version: '' },
          },
        },
      },
    };
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(inactiveServing)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/No canary rollout active -- stable serves 100% of traffic\./)).toBeInTheDocument();
    });
    // The terraform card (undeployed in this fixture), the kubernetes card,
    // and the in-cluster observability section. This fixture's `kubernetes`
    // deliberately omits `observability` altogether -- an api that predates
    // #3787 -- and the page must degrade that to NOT DEPLOYED rather than
    // throwing and blanking the whole operator surface (#3468).
    expect(screen.getAllByText('NOT DEPLOYED')).toHaveLength(3);
    expect(screen.getByText(/No pods in the/)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt-api-canary not deployed/)).toBeInTheDocument();
  });

  it('renders the detected mode and terraform/kubernetes status when deployed', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('DEPLOYED')).toHaveLength(2);
    });
    expect(screen.getByRole('heading', { name: 'Terraform (local containers)' })).toBeInTheDocument();
    expect(screen.getByText('api')).toBeInTheDocument();
    expect(screen.getAllByText('running')).toHaveLength(2);
    expect(screen.getByText('exited')).toBeInTheDocument();
    expect(screen.getByText(/pod\/nyxgpt-api-abc123/)).toBeInTheDocument();
  });

  it('renders empty/not-deployed state and the kubectl-missing hint', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      // Terraform and kubernetes both confidently NOT DEPLOYED: kubectl missing
      // means there was never a context to be unreachable (see #3468).
      expect(screen.getAllByText('NOT DEPLOYED')).toHaveLength(2);
    });
    expect(screen.getByText(/kubectl not found/)).toBeInTheDocument();
    expect(screen.getByText('Nothing detected running')).toBeInTheDocument();
  });

  it('renders NOT DEPLOYED (not CANNOT DETERMINE) when kubectl has no current-context configured', async () => {
    // Repro for #3468: a machine that has never had a k8s deployment (no
    // kubeconfig/current-context) must read as NOT DEPLOYED, not CANNOT
    // DETERMINE -- that state is reserved for a *configured* cluster the
    // probe couldn't reach.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesNotConfigured)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('NOT DEPLOYED')).toHaveLength(2);
    });
    expect(screen.queryByText('CANNOT DETERMINE')).not.toBeInTheDocument();
    // The wording names BOTH credentials since #3988 -- an empty
    // current-context is no longer the whole question, because a process
    // running in a Pod has none and full API access.
    expect(screen.getByText(/No cluster configured from this vantage point/)).toBeInTheDocument();
  });

  it('reports the cluster it is served from, and scopes out what a Pod cannot answer (#3988)', async () => {
    // The defect: served BY the api Pod, this page called that Pod's own
    // cluster NOT DEPLOYED (detection asked `kubectl config current-context`,
    // which is empty in a Pod), surveyed Compose against the container's
    // filesystem, and offered native remedies for a host it cannot see.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusInCluster)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
    });
    // `getByText('DEPLOYED')` above is an exact match, so it cannot be
    // satisfied by a "NOT DEPLOYED" badge -- the Terraform and observability
    // cards legitimately carry those, and are a different question.
    expect(
      screen.getByText(/this page is being served from inside this cluster/),
    ).toBeInTheDocument();

    // Compose and Native are scoped out, not guessed at -- and the leaked
    // container path from the report must not be on screen.
    expect(screen.getAllByText('NOT IN SCOPE')).toHaveLength(2);
    expect(screen.queryByText(/\/root\/\.nyxGPT/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No install identity recorded/)).not.toBeInTheDocument();
    expect(screen.queryByText('CANNOT DETERMINE')).not.toBeInTheDocument();
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

  it('renders "cannot determine" for the Compose section when the compose probe is unavailable (e.g. Terraform mode without a reachable compose file)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusComposeCannotDetermine)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Terraform (local containers)' })).toBeInTheDocument();
    });
    expect(screen.getAllByText('CANNOT DETERMINE')).toHaveLength(1);
    expect(
      screen.getByText(/the Compose survey could not be run from wherever/)
    ).toBeInTheDocument();
    expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
  });

  it('scopes the Compose card out on a Kubernetes HOST, rather than saying it cannot determine (#4137)', async () => {
    // The owner's k3s instance, reached over the wrapped tunnel: NOT
    // in-cluster (the api answering is on the host), a cluster holding every
    // Pod, and a Docker socket this session cannot reach. The badge used to
    // be chosen by `inCluster ? 'NOT IN SCOPE' : 'CANNOT DETERMINE'`, so this
    // landed on CANNOT DETERMINE and printed
    // `/home/ec2-user/.nyxGPT/docker-compose.yml` as the cause -- directly
    // above its own list of ready Pods.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusComposeCannotDetermine,
          mode: 'kubernetes',
          in_cluster: false,
          terraform: { probe_available: true, deployed: false, containers: {} },
          compose_in_scope: false,
          compose_out_of_scope_reason:
            'Not in scope for this deployment: nyxGPT runs as Kubernetes Pods here (14 in the nyxgpt namespace -- see the Kubernetes card), and the Compose survey could not be run from this process, so there is no Compose state this deployment is missing.',
          compose_probe_reason:
            'Not in scope for this deployment: nyxGPT runs as Kubernetes Pods here (14 in the nyxgpt namespace -- see the Kubernetes card), and the Compose survey could not be run from this process, so there is no Compose state this deployment is missing.',
          kubernetes: {
            ...mockStatusComposeCannotDetermine.kubernetes,
            deployed: true,
            pods: ['cassandra-0   1/1 Running'],
            pod_states: [
              { name: 'cassandra-0', state: 'ready', summary: '1/1 Running', details: '' },
            ],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('NOT IN SCOPE')).toBeInTheDocument();
    });
    expect(screen.queryByText('CANNOT DETERMINE')).not.toBeInTheDocument();
    expect(screen.queryByText(/docker-compose\.yml/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/the Compose survey could not be run from wherever/)
    ).not.toBeInTheDocument();
    expect(screen.getByText(/nyxGPT runs as Kubernetes Pods here/)).toBeInTheDocument();
  });

  it('keeps the Compose card in scope when its survey actually answered, cluster or not (#4137)', async () => {
    // A host that runs both has a real Compose answer, and hiding it would
    // hide the dual-stack conflict next to it.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusComposeCannotDetermine,
          compose: { grafana: 'running' },
          compose_probe_available: true,
          compose_probe_reason: '',
          compose_in_scope: true,
          compose_out_of_scope_reason: '',
          kubernetes: { ...mockStatusComposeCannotDetermine.kubernetes, deployed: true },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('grafana')).toBeInTheDocument();
    });
    expect(screen.queryByText('NOT IN SCOPE')).not.toBeInTheDocument();
  });

  it('says the native Cassandra row is unknown, not absent, when the container read was denied (#4022)', async () => {
    // The owner's EC2 instance: the API process's `systemd --user` session
    // predates its `docker` group, so `docker ps` is denied -- and until #4022
    // the card rendered that as `absent`, reporting a running Cassandra as
    // gone. `unknown` is a different claim and has to read as one.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started', web: 'started', cassandra: 'unknown' },
          native_probe_available: false,
          native_probe_reason: '`docker ps` exited 1: permission denied while trying to connect',
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('unknown — cannot determine')).toBeInTheDocument();
    });
    expect(screen.getByText(/could not read\s+container state/)).toBeInTheDocument();
    expect(
      screen.getByText(/exited 1: permission denied while trying to connect/)
    ).toBeInTheDocument();
    expect(screen.getByText(/sudo loginctl terminate-user \$USER/)).toBeInTheDocument();
  });

  it('names why the compose probe could not run, rather than only that it could not (#3812)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusComposeCannotDetermine)));

    render(<InfrastructurePage />);

    // The operator reads the cause on the page -- exit 125 against an
    // unreachable daemon -- instead of having to go find it in a log.
    await waitFor(() => {
      expect(
        screen.getByText(/exited 125: permission denied while trying to connect/)
      ).toBeInTheDocument();
    });
  });

  it('still says it cannot determine when the probe reports no reason at all', async () => {
    // Serialized without the key at all: JSON.stringify drops undefined,
    // so this is the old-API / no-reason-available shape.
    const withoutReason = { ...mockStatusComposeCannotDetermine, compose_probe_reason: undefined };
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(withoutReason)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getAllByText('CANNOT DETERMINE')).toHaveLength(1);
    });
    expect(screen.queryByText(/Reason:/)).not.toBeInTheDocument();
  });

  it('never renders install/destroy controls or api key inputs', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Terraform (local containers)' })).toBeInTheDocument();
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

  it('shows web alongside api in the per-component canary breakdown (kubernetes mode)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/Canary rollout active -- 20% of traffic to canary/)).toBeInTheDocument();
    });
    expect(screen.getByText('api')).toBeInTheDocument();
    expect(screen.getByText('web')).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt-web-stable healthy/)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt-web-canary has 0 desired replicas \(idle\)/)).toBeInTheDocument();
    expect(screen.getByText('No canary rollout active -- stable serves 100% of traffic.')).toBeInTheDocument();
  });

  it('labels a nyxgpt-provisioned kind cluster and its teardown behavior (#3596)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    expect(await screen.findByText('kind-nyxgpt-local')).toBeInTheDocument();
    expect(
      screen.getByText(/local kind cluster provisioned by nyxgpt/)
    ).toBeInTheDocument();
  });

  it('labels a bring-your-own cluster as never destroyed by ops down (#3596)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    expect(await screen.findByText('docker-desktop')).toBeInTheDocument();
    expect(
      screen.getByText(/bring-your-own cluster \(never destroyed/)
    ).toBeInTheDocument();
  });

  it('labels the native card as a dev install and names the checkout it runs (#3789)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started', web: 'started', ollama: 'started' },
          install_mode: {
            mode: 'dev',
            checkout: '/Users/owner/src/nyxGPT',
            label: 'dev (editable checkout at /Users/owner/src/nyxGPT)',
            components: ['api', 'web'],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEV INSTALL')).toBeInTheDocument();
    });
    expect(screen.getByText('/Users/owner/src/nyxGPT')).toBeInTheDocument();
    expect(screen.getByText(/not exercising the artifact path/)).toBeInTheDocument();
  });

  it('still labels a dev install when the marker recorded no checkout (#3789)', async () => {
    // `install_mode.checkout` is nullable end-to-end: `_reconcile_install_mode`
    // writes `null` when `_dev_checkout_root()` cannot resolve one, and
    // `read_install_mode` reads any falsy recorded value back as None. The
    // card must still read as a dev install in that case -- losing the path
    // must not silently downgrade the warning to the artifact wording.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started', web: 'started' },
          install_mode: {
            mode: 'dev',
            checkout: null,
            label: 'dev (editable checkout at unknown checkout)',
            components: ['api', 'web'],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEV INSTALL')).toBeInTheDocument();
    });
    expect(screen.getByText('an unrecorded checkout')).toBeInTheDocument();
    expect(screen.getByText(/not exercising the artifact path/)).toBeInTheDocument();
  });

  it('labels the native card as an artifact install by default (#3789)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started' },
          install_mode: {
            mode: 'artifact',
            checkout: null,
            label: 'artifact (published/vendored build -- the repo-less default)',
            components: ['api', 'web'],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT INSTALL')).toBeInTheDocument();
    });
    expect(screen.getByText(/repo-less default/)).toBeInTheDocument();
    expect(screen.queryByText('DEV INSTALL')).not.toBeInTheDocument();
  });

  it('falls back to artifact wording when an older api omits install_mode (#3789)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT INSTALL')).toBeInTheDocument();
    });
  });

  // --- the RUNNING build, not the installed one (#4133) ---
  //
  // Everything above is derived from disk -- a marker file and the Cellar --
  // and a process outlives the build it was started from. A `brew upgrade` on
  // a running stack left the api serving from a venv the upgrade had deleted
  // while every one of those lines reported the new keg. `running_build` is
  // the serving process's own `sys.prefix`, so the process that may be wrong
  // is the one answering this page.

  const nativeWithRunningBuild = (running_build: unknown) => ({
    ...mockStatusEmpty,
    mode: 'native',
    native: { api: 'started', web: 'started' },
    install_mode: {
      mode: 'artifact',
      checkout: null,
      label: 'artifact (published/vendored build -- the repo-less default)',
      components: ['api', 'web'],
      running_build,
    },
  });

  it('states a running-build mismatch, both paths and the repair (#4133)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          nativeWithRunningBuild({
            state: 'mismatch',
            running: {
              executable: '/Users/owner/.nyxGPT/opt/nyxgpt-api/venv/bin/python3',
              prefix: '/Users/owner/.nyxGPT/opt/nyxgpt-api/venv',
              python: '3.11.9',
              pid: 4133,
              version: '3.0.0rc17',
              prefix_exists: false,
            },
            expected_prefix:
              '/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc17/libexec/venv',
            expected_source: "the nyxgpt-api@3.0.0rc keg's venv",
            detail: 'pid 4133 is running python 3.11.9 from a deleted venv',
            remediation: 'nyxgpt ops restart api',
            summary: 'MISMATCH',
          })
        )
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(
        screen.getByText(/Running build does not match the installed build/)
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText('/Users/owner/.nyxGPT/opt/nyxgpt-api/venv')
    ).toBeInTheDocument();
    expect(
      screen.getByText('/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc17/libexec/venv')
    ).toBeInTheDocument();
    expect(screen.getByText('nyxgpt ops restart api')).toBeInTheDocument();
    // The version is reported elsewhere on this card and describes the
    // install. The card has to say that outright, because the whole defect is
    // a plausible version standing in for a statement about the process.
    expect(screen.getByText(/not this process/)).toBeInTheDocument();
    // The acute form: the running interpreter's venv is gone, so the next
    // restart by any path cannot start the api.
    expect(screen.getByText(/no longer exists/)).toBeInTheDocument();
  });

  it('omits the deleted-venv warning when the running venv is still there (#4133)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          nativeWithRunningBuild({
            state: 'mismatch',
            running: {
              executable: '/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc14/libexec/venv/bin/python3',
              prefix: '/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc14/libexec/venv',
              python: '3.12.4',
              pid: 77,
              version: '3.0.0rc14',
              prefix_exists: true,
            },
            expected_prefix:
              '/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc17/libexec/venv',
            expected_source: "the nyxgpt-api@3.0.0rc keg's venv",
            detail: 'pid 77 is running python 3.12.4 from the previous keg',
            remediation: 'nyxgpt ops restart api',
            summary: 'MISMATCH',
          })
        )
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(
        screen.getByText(/Running build does not match the installed build/)
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/no longer exists/)).not.toBeInTheDocument();
  });

  it('reports an undetermined running build as not-a-match (#4133)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          nativeWithRunningBuild({
            state: 'undetermined',
            running: null,
            expected_prefix: '',
            expected_source: '',
            detail: 'the nyxgpt-api@3.0.0rc keg carries no libexec/venv',
            remediation: 'nyxgpt ops restart api',
            summary: 'could not determine',
          })
        )
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/Could not confirm/)).toBeInTheDocument();
    });
    expect(screen.getByText(/not the same as a match/)).toBeInTheDocument();
    expect(
      screen.queryByText(/Running build does not match the installed build/)
    ).not.toBeInTheDocument();
  });

  it('says nothing about the running build where the question does not apply (#4133)', async () => {
    // A permanent row on every Compose/Kubernetes host is what teaches an
    // operator to skip the one that matters.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          nativeWithRunningBuild({
            state: 'not_applicable',
            running: null,
            expected_prefix: '',
            expected_source: '',
            detail: 'the api port on this host is held by a container deployment',
            remediation: '',
            summary: 'not applicable here',
          })
        )
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT INSTALL')).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/Running build does not match the installed build/)
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not confirm/)).not.toBeInTheDocument();
  });

  it('renders against an api that omits running_build entirely (#4133)', async () => {
    // Mid-upgrade, the web tier restarts first and talks to an api from the
    // previous build -- which is exactly the scenario this field is about, so
    // its absence must not break the card.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(nativeWithRunningBuild(undefined))
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT INSTALL')).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/Running build does not match the installed build/)
    ).not.toBeInTheDocument();
  });

  // --- WHICH build, not merely which mode (#3861) ---
  //
  // A mode cannot tell a 2.1.0 keg from a 3.0.0rc12 one -- both are
  // 'artifact' -- which is how four install identities accumulated on one
  // machine with nothing able to say so. The page has to name the build, and
  // has to say when it cannot rather than presenting the mode as if it did.

  it('names the installed build and the services it is registered under (#3861)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started', web: 'started' },
          install_mode: {
            mode: 'artifact',
            checkout: null,
            label: 'artifact (published/vendored build -- the repo-less default)',
            components: ['api', 'web'],
            identity: {
              known: true,
              manager: 'brew',
              services: { api: 'nyxgpt-api@3.0.0rc', web: 'nyxgpt-web@3.0.0rc' },
              version: '3.0.0rc12',
              channel: 'candidate',
              detail:
                'brew: api=nyxgpt-api@3.0.0rc, web=nyxgpt-web@3.0.0rc; ' +
                'version 3.0.0rc12; channel candidate',
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('3.0.0rc12 (candidate)')).toBeInTheDocument();
    expect(screen.getByText('brew')).toBeInTheDocument();
    // Both components are named with their concrete service names -- the
    // whole point is that `nyxgpt-api` and `nyxgpt-api@3.0.0rc` read
    // differently here.
    expect(screen.getByText('api=nyxgpt-api@3.0.0rc')).toBeInTheDocument();
    expect(screen.getByText('web=nyxgpt-web@3.0.0rc')).toBeInTheDocument();
    expect(screen.queryByText(/No install identity recorded/)).not.toBeInTheDocument();
  });

  it('still names the services when a known identity carries no version (#3861)', async () => {
    // A recorded identity whose version could not be read is still an
    // identity: the service names are what distinguish two kegs, and losing
    // the version must not drop the card back to "artifact" with nothing
    // said about which build registered them.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started' },
          install_mode: {
            mode: 'artifact',
            checkout: null,
            label: 'artifact (published/vendored build -- the repo-less default)',
            components: ['api', 'web'],
            identity: {
              known: true,
              manager: 'brew',
              services: { api: 'nyxgpt-api' },
              version: '',
              channel: 'stable',
              detail: 'brew: api=nyxgpt-api; version unknown; channel stable',
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('unknown version (stable)')).toBeInTheDocument();
    expect(screen.getByText('api=nyxgpt-api')).toBeInTheDocument();
  });

  it('says so plainly when the marker records no identity (#3861)', async () => {
    // `known: false` is what a marker written before identities were
    // recorded reads back as. Unknown must never be presented as "the same
    // as whatever is installed" -- that silence is the defect.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          mode: 'native',
          native: { api: 'started' },
          install_mode: {
            mode: 'artifact',
            checkout: null,
            label: 'artifact (published/vendored build -- the repo-less default)',
            components: ['api', 'web'],
            identity: {
              known: false,
              manager: 'unknown',
              services: {},
              version: '',
              channel: 'unknown',
              detail: 'no install identity recorded',
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText(/No install identity recorded/)).toBeInTheDocument();
    expect(screen.getByText(/cannot say which build the native/)).toBeInTheDocument();
  });

  // --- The Kubernetes card's OWN install mode (#3834) ---
  //
  // Reported separately from the native marker on purpose: a host can run a
  // native dev install and a Kubernetes artifact deployment at the same time,
  // and reporting one for the other is the defect this section exists to
  // prevent. Each of the three states below is a different thing an operator
  // must be able to act on, so each is pinned.
  const withK8sInstallMode = (install_mode: unknown) => ({
    ...mockStatusKubernetesServing,
    kubernetes: { ...mockStatusKubernetesServing.kubernetes, install_mode },
  });

  it('reports the Kubernetes deployment as unrecorded when nothing recorded a mode (#3834)', async () => {
    // `recorded: false` -- a cluster deployed from another machine, or before
    // nyxGPT recorded the mode. It must NOT read as the artifact default:
    // that default would be a guess about someone else's deployment.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          withK8sInstallMode({
            mode: 'artifact',
            checkout: null,
            label: 'unrecorded (no install-mode marker for this cluster)',
            recorded: false,
          })
        )
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('unrecorded')).toBeInTheDocument();
    // Both records, not just this machine's marker (#3988): the install writes
    // one into the cluster too, so "unrecorded" now means neither answered.
    expect(
      screen.getByText(/neither this cluster nor the machine this dashboard runs on/)
    ).toBeInTheDocument();
    expect(screen.getByText(/for a working-tree build\) to record it/)).toBeInTheDocument();
  });

  it('degrades the Kubernetes install mode to unrecorded against an older api (#3834)', async () => {
    // The field is optional end-to-end: an api process from before #3834 (or
    // mid-upgrade, when web restarts first) sends no `install_mode` at all.
    // Absent is the same claim as unrecorded -- never the artifact default.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(withK8sInstallMode(undefined))
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('unrecorded')).toBeInTheDocument();
    expect(
      screen.queryByText(/images built from the published/)
    ).not.toBeInTheDocument();
  });

  it('names the checkout a dev-mode Kubernetes deployment was built from (#3834)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          withK8sInstallMode({
            mode: 'dev',
            checkout: '/Users/owner/src/nyxGPT',
            label: 'dev (images built from the working tree at /Users/owner/src/nyxGPT)',
            recorded: true,
          })
        )
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('dev')).toBeInTheDocument();
    expect(screen.getByText('/Users/owner/src/nyxGPT')).toBeInTheDocument();
    // The images are frozen at install time -- the operator has to be told
    // which command puts the cluster back on the artifact path.
    expect(
      screen.getByText(/as it was at install time, not from published artifacts/)
    ).toBeInTheDocument();
  });

  it('still reads as dev when the Kubernetes marker recorded no checkout (#3834)', async () => {
    // `checkout` is nullable in the marker exactly as it is for native, and
    // losing the path must not silently downgrade the warning to artifact
    // wording -- the Pods still run something that was never published.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          withK8sInstallMode({
            mode: 'dev',
            checkout: null,
            label: 'dev (images built from the working tree at unknown checkout)',
            recorded: true,
          })
        )
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('dev')).toBeInTheDocument();
    expect(screen.getByText('an unrecorded checkout')).toBeInTheDocument();
  });

  it('reports a Kubernetes deployment built from published artifacts (#3834)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json(
          withK8sInstallMode({
            mode: 'artifact',
            checkout: null,
            label:
              'artifact (images built from the published nyxgpt-api/nyxgpt-web artifacts)',
            recorded: true,
          })
        )
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('artifact')).toBeInTheDocument();
    expect(screen.getByText(/images built from the published/)).toBeInTheDocument();
    expect(screen.queryByText('unrecorded')).not.toBeInTheDocument();
  });

  // --- "What version", and which record answered (#3988, second round) ---
  //
  // The owner's re-test passed detection and failed on these two: the card
  // reported Pods, no version at all, and an `install_mode.mode` of
  // `artifact` for a `--dev` cluster beside a `label` reading "unrecorded".
  // Neither was a vantage-point limit -- in-cluster the api process serving
  // this page IS this deployment's api -- so each state below is pinned, and
  // "unrecorded" is pinned together with the *where*.

  it('reports the version this Kubernetes deployment is running, and its source (#3988)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            in_cluster: true,
            version: {
              known: true,
              version: '3.0.0rc1',
              channel: 'rc',
              source:
                'this api process -- a Pod of this deployment, so this is the version serving now',
            },
            install_mode: {
              mode: 'dev',
              checkout: '/Users/owner/src/nyxGPT',
              label: 'dev (images built from the working tree at /Users/owner/src/nyxGPT)',
              recorded: true,
              source:
                "the cluster's own install record (configmap/nyxgpt-install-mode in namespace nyxgpt)",
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('3.0.0rc1')).toBeInTheDocument();
    expect(screen.getByText(/\(rc channel\)/)).toBeInTheDocument();
    expect(screen.getByText(/a Pod of this deployment/)).toBeInTheDocument();
    // The mode is read from the cluster's record, not this machine's marker,
    // and the card has to be able to say which -- the two vantage points
    // keep different records, so "dev" alone does not locate the claim.
    expect(screen.getByText(/configmap\/nyxgpt-install-mode/)).toBeInTheDocument();
  });

  it('omits an unknown channel and an unnamed source rather than inventing them (#3988)', async () => {
    // A version read off the install record of a deployment whose channel
    // nothing could parse. `channel: 'unknown'` must not render as a channel
    // called "unknown", and an empty `source` must not render a dangling
    // "from".
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            version: { known: true, version: '3.0.0', channel: 'unknown', source: '' },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('3.0.0')).toBeInTheDocument();
    expect(screen.queryByText(/unknown channel/)).not.toBeInTheDocument();
    expect(screen.queryByText(/— from/)).not.toBeInTheDocument();
  });

  it('says the version is unknown when no record carries one (#3988)', async () => {
    // `known: false` is what a deployment installed before the cluster
    // carried a record reads back as, off-cluster. The card must say unknown
    // -- showing a release nobody installed is the defect, one row over from
    // the `artifact`-for-`--dev` one this issue was reopened for.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            version: { known: false, version: '', channel: 'unknown', source: '' },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(await screen.findByText('unknown')).toBeInTheDocument();
    expect(
      screen.getByText(/carries no install record to read\s+a version from/)
    ).toBeInTheDocument();
  });

  // --- The Terraform card's OWN install mode (#3835) -------------------
  // A Terraform deployment records its own marker, so the card must report
  // that deployment's build and never the native one above it. The state
  // that matters most is the third one: containers running with no marker,
  // which is what every pre-#3835 deployment looks like after an upgrade --
  // and which was built from a working tree, so badging it ARTIFACT IMAGES
  // asserts the opposite of the truth.

  it('labels the terraform card as dev images and names the working tree (#3835)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          terraform: {
            ...mockStatusTerraform.terraform,
            install_mode: {
              mode: 'dev',
              checkout: '/Users/owner/src/nyxGPT',
              label: 'dev (images built from the working tree at /Users/owner/src/nyxGPT)',
              images: { api: 'nyxgpt-tf-api:dev', web: 'nyxgpt-tf-web:dev' },
              recorded: true,
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEV IMAGES')).toBeInTheDocument();
    });
    expect(screen.getByText('/Users/owner/src/nyxGPT')).toBeInTheDocument();
    expect(screen.getByText(/not exercising the artifact path/)).toBeInTheDocument();
    expect(screen.queryByText('IMAGES NOT RECORDED')).not.toBeInTheDocument();
  });

  it('still labels terraform dev images when the marker recorded no checkout (#3835)', async () => {
    // The Terraform twin of the native nullable-checkout case: losing the
    // path must not downgrade the card to the artifact wording.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          terraform: {
            ...mockStatusTerraform.terraform,
            install_mode: {
              mode: 'dev',
              checkout: null,
              label: 'dev (images built from the working tree at unknown checkout)',
              images: {},
              recorded: true,
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEV IMAGES')).toBeInTheDocument();
    });
    expect(screen.getByText('an unrecorded checkout')).toBeInTheDocument();
  });

  it('labels the terraform card as artifact images when the install recorded it (#3835)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          terraform: {
            ...mockStatusTerraform.terraform,
            install_mode: {
              mode: 'artifact',
              checkout: null,
              label:
                'artifact (published container images -- the repo-less default) ' +
                '[api=ghcr.io/dkblinux98/nyxgpt-api:3.0.0]',
              images: { api: 'ghcr.io/dkblinux98/nyxgpt-api:3.0.0' },
              recorded: true,
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT IMAGES')).toBeInTheDocument();
    });
    // The label names the image actually serving, not just the mode.
    expect(screen.getByText(/ghcr\.io\/dkblinux98\/nyxgpt-api:3\.0\.0/)).toBeInTheDocument();
    expect(screen.queryByText('IMAGES NOT RECORDED')).not.toBeInTheDocument();
  });

  it('reports a deployed-but-unrecorded terraform stack as not recorded, never artifact (#3835)', async () => {
    // The post-upgrade state of every deployment made before #3835: the
    // containers are up, nothing wrote a marker, and the build is genuinely
    // unknown. Claiming the artifact default here would state the reverse of
    // what those deployments actually run.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          terraform: {
            ...mockStatusTerraform.terraform,
            install_mode: {
              mode: 'artifact',
              checkout: null,
              label: 'not recorded (a Terraform deployment is running that no install recorded)',
              images: {},
              recorded: false,
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('IMAGES NOT RECORDED')).toBeInTheDocument();
    });
    expect(screen.getByText(/no install recorded what they were built from/)).toBeInTheDocument();
    expect(screen.queryByText('ARTIFACT IMAGES')).not.toBeInTheDocument();
    expect(screen.queryByText('DEV IMAGES')).not.toBeInTheDocument();
  });

  it('reports not recorded when an older api omits the terraform install_mode entirely (#3835)', async () => {
    // `mockStatusTerraform` has containers running and no `install_mode` key
    // at all -- an api process from before this field existed. Same answer:
    // unknown, not artifact.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('IMAGES NOT RECORDED')).toBeInTheDocument();
    });
    expect(screen.queryByText('ARTIFACT IMAGES')).not.toBeInTheDocument();
  });

  it('keeps the artifact default for a recorded but undeployed terraform stack (#3835)', async () => {
    // Nothing is running, so there is no live deployment to mis-describe --
    // the recorded marker is the whole truth and the card reports it.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusEmpty,
          terraform: {
            probe_available: true,
            deployed: false,
            containers: {},
            install_mode: {
              mode: 'artifact',
              checkout: null,
              label: 'artifact (published container images -- the repo-less default)',
              images: {},
              recorded: true,
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ARTIFACT IMAGES')).toBeInTheDocument();
    });
    expect(screen.queryByText('IMAGES NOT RECORDED')).not.toBeInTheDocument();
  });

  it('lists the in-cluster observability workloads and the wrapped port-forward command (#3787)', async () => {
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: { ...mockStatusKubernetesServing.kubernetes, observability: observabilityDeployed },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    // Per-workload state, so the operator sees *which* piece is missing.
    expect(screen.getByText('grafana')).toBeInTheDocument();
    expect(screen.getByText('prometheus')).toBeInTheDocument();
    expect(screen.getByText('absent')).toBeInTheDocument();
    expect(screen.getAllByText('1/1 ready')).toHaveLength(5);
    // Reaching the UIs is a `nyxgpt` command, never a raw kubectl one.
    expect(
      screen.getByText('nyxgpt ops port-forward --target observability')
    ).toBeInTheDocument();
    expect(screen.queryByText(/kubectl/)).not.toBeInTheDocument();
    // ...and the repair path the api sends beside it (#3986): a published node
    // port that re-applying the shipped manifests stripped is put back by a
    // second wrapped command. Scoped to that paragraph, because the same
    // command is this page's deploy pointer when the tier is absent.
    const accessNote = screen
      .getByText('nyxgpt ops port-forward --target observability')
      .closest('p') as HTMLElement;
    expect(accessNote.textContent).toMatch(
      /put them back with\s*nyxgpt ops observability --kubernetes\./
    );
    expect(accessNote.textContent).not.toContain('undefined');
  });

  it('ends the access sentence cleanly when the api sends no publish command (#3986)', async () => {
    // `publish_command` is optional on the client so an api predating #3986
    // leaves the sentence short instead of rendering `undefined` into a
    // command -- the version-skew leg the page's own comment promises.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            observability: observabilityDeployedWithoutPublishCommand,
          },
        })
      )
    );

    render(<InfrastructurePage />);

    const forward = await screen.findByText('nyxgpt ops port-forward --target observability');
    const accessNote = forward.closest('p') as HTMLElement;
    expect(accessNote.textContent).not.toContain('undefined');
    expect(accessNote.textContent).not.toMatch(/put them back with/);
    expect(accessNote.textContent?.trimEnd()).toMatch(
      /nyxgpt ops port-forward --target observability\.$/
    );
  });

  it('tells the operator how to deploy the observability layer when the cluster has none (#3787)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    expect(screen.getByText(/No observability workloads in the/)).toBeInTheDocument();
    expect(
      screen.getByText('nyxgpt ops observability --kubernetes')
    ).toBeInTheDocument();
    expect(
      screen.queryByText('nyxgpt ops port-forward --target observability')
    ).not.toBeInTheDocument();
  });

  it('names the Pods no node could schedule, with the wrapped command that refuses it (#3825)', async () => {
    // An unschedulable Pod appears as `Pending` in the pod list, which is also
    // what a placed Pod pulling its image looks like -- so the k8s install that
    // oversubscribed the node read as healthy here. Scoped with `within` because
    // the remedy command appears in three places on this page.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            unschedulable: ['prometheus-abc123', 'nyxgpt-api-canary-9f8e7d'],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    const heading = await screen.findByText('2 Pod(s) could not be scheduled');
    const block = heading.parentElement as HTMLElement;
    // The names, so the operator sees *which* piece of the stack is missing.
    expect(within(block).getByText('prometheus-abc123')).toBeInTheDocument();
    expect(within(block).getByText('nyxgpt-api-canary-9f8e7d')).toBeInTheDocument();
    expect(within(block).getByText(/No node had enough unreserved memory or CPU/)).toBeInTheDocument();
    // Reporting only, and the cure is a `nyxgpt` command -- never raw kubectl.
    expect(
      within(block).getByText('nyxgpt ops install --kubernetes')
    ).toBeInTheDocument();
    expect(within(block).queryByText(/kubectl/)).not.toBeInTheDocument();
  });

  it('says nothing about scheduling when every Pod was placed (#3825)', async () => {
    // The block must stay silent on a healthy cluster: a standing "could not be
    // scheduled" box would train the operator to ignore it. Also pins the
    // absent-field path, which is what an api predating #3825 returns.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    expect(screen.queryByText(/could not be scheduled/)).not.toBeInTheDocument();
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
  // --- AWS: information only, source-aware (#3804) ---------------------
  //
  // The section that replaced the /admin/cloud-infrastructure screen. Its
  // whole point is that the answer depends on where the UI is running, so
  // every source gets its own case.

  it('reports the AWS substrate as unknown -- never "not provisioned" -- on a machine that is neither an instance nor an operator workstation', async () => {
    // The rc12 defect this section exists to prevent, in its general form: a
    // machine with no source must not assert an answer about AWS.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'AWS substrate' })).toBeInTheDocument();
    });
    expect(screen.getAllByText('UNKNOWN')).toHaveLength(2);
    expect(screen.queryByText('NOT PROVISIONED')).not.toBeInTheDocument();
    expect(screen.getByText(/neither an EC2 instance nor one that has provisioned the substrate/)).toBeInTheDocument();
    expect(screen.getByText(/no deploy has been recorded here and this is not the instance/)).toBeInTheDocument();
  });

  it('reports a deploy that did not finish as NOT COMPLETED, never as DEPLOYED or UNKNOWN (#3993)', async () => {
    // The failure family this issue closes. Keying the badge off `known`
    // alone would print DEPLOYED for a provision that died partway -- worse
    // than the UNKNOWN it replaced, because it sends the operator to debug
    // the wrong thing entirely.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-attempt',
          known: true,
          deployed: false,
          version: '3.0.0',
          host: '203.0.113.10',
          instance_id: 'i-0abc123def',
          attempt: {
            status: 'failed',
            phase: 'provision',
            version: '3.0.0',
            error: '[FAIL] Could not reconcile Grafana admin credential',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('NOT COMPLETED')).toBeInTheDocument();
    });
    expect(screen.queryByText('DEPLOYED')).not.toBeInTheDocument();
    expect(screen.getByText(/stopped at the `provision` phase/)).toBeInTheDocument();
    expect(screen.getByText(/Could not reconcile Grafana admin credential/)).toBeInTheDocument();
    // #4181. The billing assertion needs AWS to have confirmed the resource in
    // this run; nothing here did, so the card says what it actually knows. The
    // card asserted a charge over an unverified record while AWS held no
    // instances at all, and this is the sentence that replaces it.
    expect(screen.queryByText(/An instance exists and is being billed/)).toBeNull();
    expect(
      screen.getByText(/nothing confirmed it at AWS in this run/)
    ).toBeInTheDocument();
    // Observable, never operable (D-017): a pointer to the command, not a
    // button. (The commands table lower down names it too, hence getAllByText.)
    expect(screen.getAllByText('nyxgpt cloud deploy').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /deploy/i })).not.toBeInTheDocument();
  });

  it('does not claim an instance is billing when the deploy failed before the substrate (#4007)', async () => {
    // D-018 inside the card written to enforce it. A deploy that dies at
    // `infra` -- no terraform binary, no credentials -- records no ids, and
    // the card used to assert billing over it and point at `cloud destroy`,
    // which raises "nothing to destroy".
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-attempt',
          known: true,
          deployed: false,
          host: '',
          instance_id: '',
          instance_type: '',
          attempt: {
            status: 'failed',
            phase: 'infra',
            error: 'terraform init failed: no such binary',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('NOT COMPLETED')).toBeInTheDocument();
    });
    expect(screen.queryByText(/An instance exists and is being billed/)).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing is recorded as provisioned by this attempt/)).toBeInTheDocument();
    // The commands reference table lower down still lists destroy; what must
    // not appear is the CARD offering it as the next step for this state.
    expect(screen.queryByText(/to tear it\s+down/)).not.toBeInTheDocument();
  });

  it('reports a provisioned substrate with no deploy against it as SUBSTRATE ONLY (#3993)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'substrate-record',
          known: true,
          deployed: false,
          host: '203.0.113.10',
          instance_id: 'i-0abc123def',
          attempt: {},
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('SUBSTRATE ONLY')).toBeInTheDocument();
    });
    expect(screen.queryByText('DEPLOYED')).not.toBeInTheDocument();
    expect(
      screen.getByText(/no deploy has been recorded against it/)
    ).toBeInTheDocument();
  });

  it('shows IMDS-derived substrate facts when the dashboard is running on the EC2 instance (#3804)', async () => {
    // The owner's rc12 observation: served *from* the provisioned instance,
    // the page used to read "not provisioned" with every field blank because
    // it looked at Terraform state that lives on the operator's workstation.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'local-instance',
          known: true,
          on_instance: true,
          deployed: true,
          version: '3.0.0rc12',
          host: '203.0.113.10',
          region: 'us-east-1',
          health: {
            checked: false,
            healthy: false,
            status: 0,
            reason: 'this dashboard is served from the instance -- the stack answering this request is the deployment',
          },
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'imds',
            source_label: 'instance metadata (this dashboard is running on the instance)',
            on_ec2: true,
            known: true,
            provisioned: true,
            region: 'us-east-1',
            instance_id: 'i-0abc123',
            instance_type: 'm5.large',
            public_ip: '203.0.113.10',
            vpc_id: 'vpc-0def456',
            subnet_id: 'subnet-0aaa111',
            security_group_id: 'sg-0bbb222',
            ssh_key_name: 'nyxgpt-owner',
            owner_ip_cidr: '',
            access_model: { ...CLOUD_DEPLOY_UNKNOWN.infra.access_model, open_ports: [22] },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('PROVISIONED')).toBeInTheDocument();
    });
    expect(screen.getByText('i-0abc123')).toBeInTheDocument();
    expect(screen.getByText('vpc-0def456')).toBeInTheDocument();
    expect(screen.getByText('sg-0bbb222')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt-owner')).toBeInTheDocument();
    expect(screen.getByText('22')).toBeInTheDocument();
    expect(screen.getByText(/instance metadata \(this dashboard is running on the instance\)/)).toBeInTheDocument();
    // The one substrate fact IMDS cannot answer, named rather than blanked.
    expect(screen.getByText(/not visible from the instance/)).toBeInTheDocument();
    // The deployment is read first-hand, and the tunnel is not this machine's.
    expect(screen.getByText('3.0.0rc12')).toBeInTheDocument();
    expect(screen.getByText(/served by the deployed stack itself/)).toBeInTheDocument();
    expect(screen.getByText(/not applicable — the tunnel is opened/)).toBeInTheDocument();
    // Terraform state does not exist on the instance, and saying "local file"
    // there would be a plain falsehood.
    expect(screen.getByText('NOT ON THIS MACHINE')).toBeInTheDocument();
    expect(screen.getByText(/Terraform state lives on the machine that provisioned the substrate/)).toBeInTheDocument();
  });

  it('reports the cloud deployment from inside the cluster serving the page (#4138)', async () => {
    // The owner's 2026-10-03 observation on the EC2 k3s instance: both cards
    // read UNKNOWN -- "it is neither an EC2 instance" -- while `nyxgpt cloud
    // status` on that same box printed the instance in full. The api Pod
    // serving this page reaches neither IMDS (link-local is not routed into
    // the Pod network) nor the host's ~/.nyxGPT/cloud, so the install records
    // the facts in the cluster and the API serves them from there.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'cluster-record',
          known: true,
          on_instance: true,
          deployed: true,
          version: '3.0.0rc17',
          host: '184.193.4.58',
          instance_id: 'i-081ec19e9a96fa4ff',
          instance_type: 'm5.xlarge',
          region: 'us-east-1',
          substrate: 'kubernetes',
          os_family: 'linux',
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'cluster-record',
            source_label:
              'the cloud-deploy record in this cluster (configmap/nyxgpt-cloud-deploy in namespace nyxgpt) -- this page is served from a Pod inside the cluster on the instance, and `nyxgpt ops install --kubernetes` wrote these facts there from the instance’s own metadata',
            on_ec2: true,
            known: true,
            provisioned: true,
            region: 'us-east-1',
            instance_id: 'i-081ec19e9a96fa4ff',
            instance_type: 'm5.xlarge',
            public_ip: '184.193.4.58',
            vpc_id: 'vpc-0def456',
            subnet_id: 'subnet-0aaa111',
            security_group_id: 'sg-0bbb222',
            ssh_key_name: 'nyxgpt-owner',
            owner_ip_cidr: '',
            access_model: { ...CLOUD_DEPLOY_UNKNOWN.infra.access_model, open_ports: [22] },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    // AC1: real values, not UNKNOWN, on both cards.
    await waitFor(() => {
      expect(screen.getByText('PROVISIONED')).toBeInTheDocument();
    });
    expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
    expect(screen.queryByText(/neither an EC2 instance/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/no deploy has been recorded here and this is not the instance/)
    ).not.toBeInTheDocument();
    // AC2: the values `nyxgpt cloud status` prints on the instance.
    expect(screen.getAllByText('i-081ec19e9a96fa4ff').length).toBeGreaterThan(0);
    expect(screen.getByText('i-081ec19e9a96fa4ff (m5.xlarge)')).toBeInTheDocument();
    expect(screen.getAllByText('184.193.4.58').length).toBeGreaterThan(0);
    expect(screen.getByText('3.0.0rc17')).toBeInTheDocument();
    expect(screen.getAllByText('us-east-1').length).toBeGreaterThan(0);
    // AC3: the vantage point is named, as #3988's Kubernetes card names its own
    // -- and it names the record, not an IMDS read nothing made.
    expect(screen.getByText(/configmap\/nyxgpt-cloud-deploy/)).toBeInTheDocument();
    expect(
      screen.getByText(/served by an api Pod of the cluster on the instance/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Read first-hand: this dashboard is served by the deployed stack itself/)).not.toBeInTheDocument();
    // Unchanged from the IMDS vantage point, because it is equally true in a
    // Pod: the security-group rule is not metadata, Terraform state is not
    // here, and the tunnel is the operator's.
    expect(screen.getByText(/not visible from the instance/)).toBeInTheDocument();
    expect(screen.getByText('NOT ON THIS MACHINE')).toBeInTheDocument();
    expect(screen.getByText(/not applicable — the tunnel is opened/)).toBeInTheDocument();
  });

  it('shows Terraform-state-derived facts, the open tunnel and the remote backend on the operator workstation', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.0rc12',
          host: '203.0.113.10',
          region: 'us-east-1',
          profiles: ['monitoring', 'tracing'],
          tunnel: { running: true, pid: 4242, host: '203.0.113.10', profiles: [], urls: {} },
          health: { checked: true, healthy: true, status: 200, reason: '' },
          urls: { api: 'http://localhost:8000', web: 'http://localhost:3000' },
          history: [
            { ts: 1755300000, action: 'deploy', outcome: 'succeeded', version: '3.0.0rc12' },
            { ts: 1755200000, action: 'destroy', outcome: 'failed', detail: 'instance still present' },
          ],
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'terraform-state',
            source_label: 'Terraform state on this machine',
            known: true,
            provisioned: true,
            region: 'us-east-1',
            instance_id: 'i-0abc123',
            owner_ip_cidr: '198.51.100.4/32',
            access_model: { ...CLOUD_DEPLOY_UNKNOWN.infra.access_model, open_ports: [22] },
          },
        })
      )
    );
    server.use(
      http.get('/api/v1/cloud/state', () =>
        HttpResponse.json({
          ...CLOUD_STATE_LOCAL,
          backend: 's3',
          remote_enabled: true,
          bootstrapped: true,
          bucket: 'nyxgpt-tfstate-1234',
          table: 'nyxgpt-tfstate-locks',
          key: 'nyxgpt/aws/terraform.tfstate',
          region: 'us-east-1',
          locking: 'dynamodb',
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('PROVISIONED')).toBeInTheDocument();
    });
    expect(screen.getByText('Terraform state on this machine')).toBeInTheDocument();
    expect(screen.getByText('198.51.100.4/32')).toBeInTheDocument();
    expect(screen.getByText('open (pid 4242)')).toBeInTheDocument();
    expect(screen.getByText('healthy (HTTP 200 over the tunnel)')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:8000')).toBeInTheDocument();
    expect(screen.getByText('S3 + DYNAMODB LOCK')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt-tfstate-locks')).toBeInTheDocument();
    // Deploy history, including a failed teardown with its detail.
    expect(screen.getByText('succeeded')).toBeInTheDocument();
    expect(screen.getByText(/instance still present/)).toBeInTheDocument();
  });

  it('names where a cloud deploy keeps its chat sessions, and separates "not recorded" from "file" (#3865)', async () => {
    // The rc12 defect this row exists to make visible: a cloud deploy silently
    // ran the back-compat `file` backend, so chats lived as JSON on the
    // instance's own disk and no other mode could see them. Three distinct
    // claims, and the third is the one that is easy to get wrong -- a deploy
    // record written before the flag existed says *nothing* about the backend,
    // which is not the same as saying the sessions are file-backed.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const deployed = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      version: '3.0.0rc12',
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
    };

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, session_backend: 'cassandra' })
      )
    );
    const first = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/Cassandra \(nyxgpt\.chat_sessions\) — shared with every mode/)
      ).toBeInTheDocument();
    });
    first.unmount();

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, session_backend: 'file' })
      )
    );
    const second = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/JSON files on the instance’s own disk/)
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/lost with the instance/)).toBeInTheDocument();
    second.unmount();

    // No key at all: rather than guess, the page names the wrapped command
    // that can ask the instance itself.
    server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(deployed)));
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/not recorded — this deploy predates the session-backend flag/)
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/nyxgpt cloud ops session-backend/)).toBeInTheDocument();
  });

  it('shows which AWS account and key pair a deployment used, on both cloud cards (#4186)', async () => {
    // The acceptance criterion this row answers: "the choice is visible
    // afterwards". The account was the one provisioning input no screen ever
    // showed, so an operator with more than one AWS account could not tell
    // which of them held their instance -- and the same acceptance round found
    // commands running against the wrong one (#4181).
    //
    // The *string* is rendered by Python (`aws_account_label`, from
    // `cloud_identity.recorded_account_label`) and displayed here as-is, so
    // this asserts it is displayed rather than reassembled: the four branches
    // of that wording were briefly written out again in this file's TypeScript
    // and had already drifted on the apostrophe (ledger D-066).
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.1rc1',
          region: 'us-east-1',
          aws_profile: 'nyxgpt',
          aws_account_id: '066835328281',
          aws_account_label: 'nyxgpt (066835328281)',
          ssh_key_name: 'nyxgpt-smoke-key',
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'terraform-state',
            source_label: 'Terraform state on this machine',
            known: true,
            provisioned: true,
            instance_id: 'i-0abc123',
            ssh_key_name: 'nyxgpt-smoke-key',
            aws_profile: 'nyxgpt',
            aws_account_id: '066835328281',
            aws_account_label: 'nyxgpt (066835328281)',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
    });
    // Once on the substrate card, once on the cloud deployment card -- the
    // card an operator reads after a deploy, where "which account is this in?"
    // is part of the answer.
    expect(screen.getAllByText('AWS account')).toHaveLength(2);
    expect(screen.getAllByText('nyxgpt (066835328281)')).toHaveLength(2);
    // The key pair, likewise on both: SSH is the only way into the instance.
    expect(screen.getAllByText('SSH key pair')).toHaveLength(2);
    expect(screen.getAllByText('nyxgpt-smoke-key')).toHaveLength(2);
  });

  it('says "not recorded here" rather than leaving the account rows blank (#4186)', async () => {
    // The inverse, and the case that is easy to get wrong. On the instance
    // itself, or in an api Pod, no `infra.json` exists to have recorded the
    // account -- that is a claim about *this machine's knowledge*, not a claim
    // that the deployment has no profile, and a blank row would read as the
    // latter (D-018). The wording is pinned by docs/cloud.md for both this
    // surface and `nyxgpt cloud status`.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'imds',
          known: true,
          on_instance: true,
          deployed: true,
          version: '3.0.1rc1',
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'imds',
            source_label: 'instance metadata (this dashboard is running on the instance)',
            on_ec2: true,
            known: true,
            provisioned: true,
            instance_id: 'i-0abc123',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
    });
    expect(screen.getAllByText('not recorded here')).toHaveLength(2);
  });

  it('still renders the account rows when the api predates the rendered label (#4186)', async () => {
    // Version skew during an upgrade: an api older than this page sends no
    // `aws_account_label` at all. The page must not render `undefined`, and it
    // must not rebuild the wording from the two raw fields either -- there is
    // nothing to rebuild it from, since an api without the label has no
    // account fields to offer.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const cloudWithoutLabel: Record<string, unknown> = { ...CLOUD_DEPLOY_UNKNOWN };
    delete cloudWithoutLabel.aws_account_label;
    const infraWithoutLabel: Record<string, unknown> = { ...CLOUD_DEPLOY_UNKNOWN.infra };
    delete infraWithoutLabel.aws_account_label;
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...cloudWithoutLabel,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.1rc1',
          infra: {
            ...infraWithoutLabel,
            source: 'terraform-state',
            source_label: 'Terraform state on this machine',
            known: true,
            provisioned: true,
            instance_id: 'i-0abc123',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('DEPLOYED')).toBeInTheDocument();
    });
    expect(screen.getAllByText('not recorded here')).toHaveLength(2);
    expect(screen.queryByText('undefined')).not.toBeInTheDocument();
  });

  it('says when the cloud instance is running a shipped working tree rather than a release (#3950)', async () => {
    // Every other field on this card reads identically for a `--dev` deploy
    // and an artifact deploy of the same version -- version, host, instance,
    // region, profiles, health. So without this the page reports a
    // working-tree build as a published release, and an operator debugging
    // one has no way to tell which they are looking at.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const deployed = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      version: '3.0.0',
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
    };

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, dev: true, source_dir: '/Users/o/src/nyxGPT' })
      )
    );
    const devDeploy = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('DEV BUILD')).toBeInTheDocument();
    });
    expect(
      screen.getByText(/working tree shipped from \/Users\/o\/src\/nyxGPT \(--dev\)/)
    ).toBeInTheDocument();
    // And it says what that means, not just what it is: the version above
    // names a release this stack is not running.
    expect(screen.getByText(/not a published 3\.0\.0 release/)).toBeInTheDocument();
    devDeploy.unmount();

    // A dev deploy whose record predates `source_dir` -- or was written by a
    // path that never captured it -- still has to say *which* claim it is
    // making. "working tree shipped from " with nothing after it reads as a
    // rendering bug; naming the gap says the build is unverifiable rather
    // than pretending the directory is known.
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, dev: true, source_dir: '' })
      )
    );
    const devNoDir = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('DEV BUILD')).toBeInTheDocument();
    });
    expect(
      screen.getByText(/working tree shipped from an unrecorded checkout \(--dev\)/)
    ).toBeInTheDocument();
    devNoDir.unmount();

    // The artifact path makes the positive claim rather than staying silent:
    // "published release" is the thing an operator wants confirmed.
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, dev: false, source_dir: '' })
      )
    );
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText('published release, installed from PyPI on the instance')
      ).toBeInTheDocument();
    });
    expect(screen.queryByText('DEV BUILD')).not.toBeInTheDocument();
  });

  it('names the substrate, and separates "not recorded" from "native" (#3956)', async () => {
    // Three distinct claims, and the third is the one that is easy to get
    // wrong: a deploy record written before `--kubernetes` existed says
    // *nothing* about the substrate, which is not the same as saying it was
    // native. Only the k3s answer makes canary rollout available, so an
    // operator reading this row is deciding whether `nyxgpt cloud canary`
    // is even a thing they can run.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const deployed = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      version: '3.0.0',
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
    };

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, substrate: 'kubernetes' })
      )
    );
    const k8s = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/single-node k3s cluster on the instance/)).toBeInTheDocument();
    });
    // The row earns its place by saying what the substrate *enables*.
    expect(screen.getByText(/canary rollout available/)).toBeInTheDocument();
    k8s.unmount();

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...deployed, substrate: 'native' })
      )
    );
    const native = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/native services on the instance/)).toBeInTheDocument();
    });
    // And what it would take to get canary, rather than leaving the operator
    // to infer that native means "no".
    expect(screen.getByText(/--kubernetes` deploys onto a cluster instead/)).toBeInTheDocument();
    native.unmount();

    // A record predating the flag: silence about the substrate, not a claim
    // that it was native.
    server.use(
      http.get('/api/v1/cloud/deploy', () => HttpResponse.json({ ...deployed, substrate: '' }))
    );
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/not recorded — this deploy predates the substrate record/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/single-node k3s cluster/)).not.toBeInTheDocument();
  });

  it('names which target OS provisioned the instance, and separates "not recorded" from "linux" (#3867)', async () => {
    // The two target OSes do not leave an instance in the same shape: an EC2
    // Mac runs the Homebrew formulas under launchd with no observability
    // stack and no self-heal watchdog, and nothing else on this page
    // distinguishes them. Three distinct claims, and the third is the one
    // that is easy to get wrong -- a deploy record written before `--os`
    // existed says *nothing* about the target OS, which is not the same as
    // saying it was Linux.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const deployed = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      version: '3.0.0',
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
    };

    server.use(
      http.get('/api/v1/cloud/deploy', () => HttpResponse.json({ ...deployed, os_family: 'macos' }))
    );
    const first = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/macOS \(EC2 Mac\) — remote Homebrew tap \+ brew services/)
      ).toBeInTheDocument();
    });
    // #4150 moved this claim rather than dropping it: the row used to fold the
    // watchdog into the nested-virtualization clause, and self-healing being
    // off is a default this bootstrap does not change, not a platform limit.
    // Asserted on the state it now reports, because the point of the row is
    // that a Mac's shape is named rather than guessed.
    expect(screen.getByText(/Self-healing is off/)).toBeInTheDocument();
    first.unmount();

    server.use(
      http.get('/api/v1/cloud/deploy', () => HttpResponse.json({ ...deployed, os_family: 'linux' }))
    );
    const second = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/Linux — published PyPI release \+ systemd --user/)
      ).toBeInTheDocument();
    });
    second.unmount();

    // No key at all: the page says so rather than defaulting to Linux.
    server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(deployed)));
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/not recorded — this deploy predates the `nyxgpt cloud deploy --os` flag/)
      ).toBeInTheDocument();
    });
  });

  it('reports the EC2 Mac screen path on a macOS deployment, and only there (#4121)', async () => {
    // Observable, not operable (D-017): the page says whether the screen path
    // is open and names the wrapped command, and carries no control that
    // opens it. The three states are distinct answers with distinct next
    // commands, so collapsing any two would send an operator to re-run a
    // configuration step that already succeeded.
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const base = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      version: '3.0.0',
      os_family: 'macos',
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
      commands: {
        ...CLOUD_DEPLOY_UNKNOWN.commands,
        screen: 'nyxgpt cloud screen',
        screen_stop: 'nyxgpt cloud screen --stop',
      },
    };
    const screenPayload = {
      running: true,
      pid: 4242,
      local_port: 5900,
      url: 'vnc://localhost:5900',
      configured: true,
      configured_at: '2026-10-02T05:00:00',
      password_file: '/home/op/.nyxGPT/secrets/cloud-mac-vnc-password',
      command: 'nyxgpt cloud screen',
      stop_command: 'nyxgpt cloud screen --stop',
    };

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...base, screen: screenPayload })
      )
    );
    const open = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/open at vnc:\/\/localhost:5900 \(pid 4242\)/)).toBeInTheDocument();
    });
    // The pointer is there; a button is not.
    expect(screen.getAllByText('nyxgpt cloud screen').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /screen/i })).toBeNull();
    open.unmount();

    // Enabled on the Mac but no tunnel: a different answer from "never set up".
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...base,
          screen: { ...screenPayload, running: false, pid: 0, url: '' },
        })
      )
    );
    const closed = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/Screen Sharing is enabled on the Mac \(loopback only\)/)).toBeInTheDocument();
    });
    closed.unmount();

    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...base,
          screen: { ...screenPayload, running: false, pid: 0, url: '', configured: false },
        })
      )
    );
    const never = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/not set up — `nyxgpt cloud screen` opens one/)).toBeInTheDocument();
    });
    never.unmount();

    // A Linux deployment has no screen, so the row and the pointer are absent
    // rather than claiming a closed path on a box that has not got one.
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ ...base, os_family: 'linux', screen: screenPayload })
      )
    );
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText(/Linux — published PyPI release/)).toBeInTheDocument();
    });
    expect(screen.queryByText('Mac screen path')).toBeNull();
    expect(screen.queryByText('nyxgpt cloud screen')).toBeNull();
  });

  it('reads "not provisioned" only when this machine has Terraform state that records no instance', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'terraform-state',
            source_label: 'Terraform state on this machine',
            known: true,
            provisioned: false,
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('NOT PROVISIONED')).toBeInTheDocument();
    });
    expect(screen.getByText(/has Terraform state for the substrate and it records no instance/)).toBeInTheDocument();
    expect(screen.getByText('LOCAL FILE')).toBeInTheDocument();
  });

  it('carries no acting cloud control: no Plan, state migrate/restore/unlock or tunnel buttons, only wrapped command pointers (#3804)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Cloud lifecycle commands' })).toBeInTheDocument();
    });
    for (const name of [/^plan$/i, /migrate/i, /restore/i, /unlock/i, /tunnel/i, /^deploy$/i, /destroy/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    // Refresh status is the only button on the page, and it is a re-read.
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: /refresh status/i })).toBeInTheDocument();
    // No lingering route to the removed screen.
    expect(screen.queryByRole('link', { name: /AWS Cloud Infrastructure/i })).not.toBeInTheDocument();
  });

  it('renders the lifecycle pointers the backend sent, so the page cannot drift from the CLI (#3804)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          commands: { ...CLOUD_LIFECYCLE_COMMANDS, deploy: 'nyxgpt cloud deploy --version 9.9.9' },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('nyxgpt cloud deploy --version 9.9.9')).toBeInTheDocument();
    });
    expect(screen.getByText('nyxgpt cloud destroy --yes')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud tunnel --stop')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud state migrate')).toBeInTheDocument();
  });

  it('keeps the local sections readable when the cloud read fails, in its own error slot', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ error: 'cloud status unavailable' }, { status: 502 })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('cloud status unavailable')).toBeInTheDocument();
    });
    // The local probe is unaffected -- one failure must not blank the other.
    expect(screen.getByRole('heading', { name: 'Terraform (local containers)' })).toBeInTheDocument();
    expect(screen.getByText(/pod\/nyxgpt-api-abc123/)).toBeInTheDocument();
    // With no cloud payload the pointers still render, from the page's own
    // copy of the wrapped commands.
    expect(screen.getByText('nyxgpt cloud destroy --yes')).toBeInTheDocument();

    // Retrying re-reads only the cloud half.
    const user = userEvent.setup();
    server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(CLOUD_DEPLOY_UNKNOWN)));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.queryByText('cloud status unavailable')).not.toBeInTheDocument();
    });
  });

  it('reports a closed tunnel, an unhealthy probe, an undated history entry and an unreadable state backend', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/state', () =>
        HttpResponse.json({ detail: 'no backend configured' }, { status: 500 })
      )
    );
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.0rc12',
          tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
          health: { checked: true, healthy: false, status: 502, reason: 'the tunneled API did not answer with 200' },
          // `ts` absent, as an entry written by an older CLI would be: the
          // label must degrade rather than print "Invalid Date".
          history: [{ action: 'deploy', outcome: 'failed', version: '3.0.0rc11' }],
          infra: {
            ...CLOUD_DEPLOY_UNKNOWN.infra,
            source: 'terraform-state',
            source_label: 'Terraform state on this machine',
            known: true,
            provisioned: true,
            instance_id: 'i-0abc123',
            // Unset on a workstation that never recorded one -- and not an
            // instance, so there is no "not visible from here" to say either.
            owner_ip_cidr: '',
            access_model: { ...CLOUD_DEPLOY_UNKNOWN.infra.access_model, open_ports: [22] },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('closed')).toBeInTheDocument();
    });
    expect(screen.getByText('unhealthy — HTTP 502 over the tunnel')).toBeInTheDocument();
    expect(screen.getByText(/deploy 3\.0\.0rc11 · failed/)).toBeInTheDocument();
    expect(screen.getByText('UNKNOWN')).toBeInTheDocument();
    expect(screen.getByText(/the state backend could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/not visible from the instance/)).not.toBeInTheDocument();
  });

  it('walks every cloud-read error branch, then falls back to String(e) on a non-Error rejection', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({ detail: 'cloud state store unreachable' }, { status: 500 })
      )
    );
    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('cloud state store unreachable')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json({}, { status: 503 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('HTTP 503')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('cloud gremlin'));
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('cloud gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('separates "no response at all" from an HTTP status, and an absent health field from either', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    const deployed = {
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      deployed: true,
      tunnel: { running: false, pid: 0, host: '', profiles: [], urls: {} },
    };
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...deployed,
          health: { checked: true, healthy: false, status: 0, reason: '' },
        })
      )
    );

    const { unmount } = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('unhealthy — no response over the tunnel')).toBeInTheDocument();
    });
    unmount();

    // Not probed and the backend gave no reason: say so rather than leaving
    // the sentence hanging.
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...deployed,
          health: { checked: false, healthy: false, status: 0, reason: '' },
        })
      )
    );
    const second = render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('not checked — no probe was run')).toBeInTheDocument();
    });
    second.unmount();

    // An api that predates the health field at all: unknown, not unhealthy.
    const withoutHealth: Record<string, unknown> = { ...deployed };
    delete withoutHealth.health;
    server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(withoutHealth)));

    render(<InfrastructurePage />);
    await waitFor(() => {
      expect(screen.getByText('unknown')).toBeInTheDocument();
    });
  });

  it('shows the connection target and the wrapped tunnel it executes, plus the instance type (#3813)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.0',
          host: '203.0.113.10',
          instance_id: 'i-0abc123',
          instance_type: 't3.large',
          region: 'us-east-1',
          connection: {
            known: true,
            host: '203.0.113.10',
            user: 'ec2-user',
            identity_file: '/home/op/.ssh/nyxgpt.pem',
            target: 'ec2-user@203.0.113.10',
            tunnel_invocation: 'ssh -N -L 8000:127.0.0.1:8000 ec2-user@203.0.113.10',
            command: 'nyxgpt cloud tunnel',
            reason: '',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ec2-user@203.0.113.10')).toBeInTheDocument();
    });
    expect(screen.getByText('/home/op/.ssh/nyxgpt.pem')).toBeInTheDocument();
    expect(screen.getByText('i-0abc123 (t3.large)')).toBeInTheDocument();
    // The raw ssh is shown as diagnostics only -- the sentence around it must
    // point at the wrapped command (CLAUDE.md's wrapper requirement).
    expect(
      screen.getByText('ssh -N -L 8000:127.0.0.1:8000 ec2-user@203.0.113.10')
    ).toBeInTheDocument();
    expect(screen.getByText(/Run the wrapped command, not this/)).toBeInTheDocument();
    // And the wrapped way to see the instance's containers is named, so an
    // operator never needs a hand-rolled ssh plus a raw `docker compose ps`.
    expect(screen.getByText('nyxgpt cloud ops status')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud status')).toBeInTheDocument();
  });

  it('names ssh’s own defaults when the deploy recorded no identity file (#3813)', async () => {
    // A deploy made with an agent-held key records an empty `identity_file`,
    // and `connection_status` reports that as a real answer rather than a
    // missing one. The page has to say which key ssh will use, not leave the
    // row blank -- an operator reading a blank there cannot tell "defaults"
    // from "the dashboard failed to report it".
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'deploy-record',
          known: true,
          deployed: true,
          version: '3.0.0',
          host: '203.0.113.10',
          instance_id: 'i-0abc123',
          region: 'us-east-1',
          connection: {
            known: true,
            host: '203.0.113.10',
            user: 'ec2-user',
            identity_file: '',
            target: 'ec2-user@203.0.113.10',
            // No `-i` in the invocation either: ssh_argv omits it when the
            // deploy recorded no key.
            tunnel_invocation: 'ssh -N -L 8000:127.0.0.1:8000 ec2-user@203.0.113.10',
            command: 'nyxgpt cloud tunnel',
            reason: '',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('ec2-user@203.0.113.10')).toBeInTheDocument();
    });
    expect(screen.getByText('(ssh’s own ~/.ssh defaults and agent)')).toBeInTheDocument();
  });

  it('says why there is no connection target rather than rendering a blank one (#3813)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
    server.use(
      http.get('/api/v1/cloud/deploy', () =>
        HttpResponse.json({
          ...CLOUD_DEPLOY_UNKNOWN,
          source: 'local-instance',
          known: true,
          deployed: true,
          on_instance: true,
          version: '3.0.0',
          connection: {
            ...CLOUD_DEPLOY_UNKNOWN.connection,
            reason:
              'this dashboard is served by the instance itself -- the SSH user and identity file are the operator workstation’s, and are recorded there',
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/Not reportable from here/)).toBeInTheDocument();
    });
    expect(screen.getByText(/served by the instance itself/)).toBeInTheDocument();
    expect(screen.queryByText(/Run the wrapped command, not this/)).not.toBeInTheDocument();
  });

  it('badges a Pod that is merely starting as PENDING, not as a failure (#3827)', async () => {
    // The raw `kubectl get pods` line says "Pending" for a Pod pulling its
    // image AND for one the node cannot fit. The page must not repeat the
    // install's old mistake of calling both of them broken.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          kubernetes: {
            ...mockStatusTerraform.kubernetes,
            pod_states: [
              { name: 'nyxgpt-api-stable-1', state: 'ready', summary: 'Running', details: '' },
              {
                name: 'grafana-2',
                state: 'pending',
                summary: 'Pending: ContainerCreating',
                details: '',
              },
              {
                name: 'prometheus-3',
                state: 'failed',
                summary: 'Pending: unschedulable',
                details: '0/1 nodes are available: 1 Insufficient memory.',
              },
            ],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('grafana-2')).toBeInTheDocument();
    });
    expect(screen.getByText('PENDING')).toBeInTheDocument();
    expect(screen.getByText('Pending: ContainerCreating')).toBeInTheDocument();
    // ...and the one that will never start is distinct, with its reason.
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText(/Insufficient memory/)).toBeInTheDocument();
  });

  it('badges a Pod the rollout already replaced as SUPERSEDED, not FAILED (#3990)', async () => {
    // Kubernetes keeps terminal Pods for diagnosis, so every rollout leaves one
    // behind in phase Failed. Badging that red showed a serving deployment as
    // broken for ever -- and disagreed with `nyxgpt ops status`, which is the
    // one-screen-two-verdicts defect #3827 exists to prevent.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusTerraform,
          kubernetes: {
            ...mockStatusTerraform.kubernetes,
            pod_states: [
              {
                name: 'nyxgpt-web-stable-598d7fddd8-45w4x',
                state: 'superseded',
                summary: 'Failed: superseded by nyxgpt-web-stable-6774c4f89-bjf7d',
                details: 'A previous revision\u2019s Pod that its workload has already replaced.',
              },
              {
                name: 'nyxgpt-web-stable-6774c4f89-bjf7d',
                state: 'ready',
                summary: 'Running',
                details: '',
              },
            ],
          },
        })
      )
    );

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText('nyxgpt-web-stable-598d7fddd8-45w4x')).toBeInTheDocument();
    });
    expect(screen.getByText('SUPERSEDED')).toBeInTheDocument();
    expect(screen.queryByText('FAILED')).not.toBeInTheDocument();
    // Shown, not hidden: an operator looking for why a Pod died needs it.
    expect(screen.getByText(/superseded by nyxgpt-web-stable-6774c4f89-bjf7d/)).toBeInTheDocument();
  });

  it('says READY is not the same as receiving, and names the command that asks (#3990)', async () => {
    // Ten READY badges over a tier that observed nothing is the #3990 state.
    // The data-flow answer needs `kubectl exec` into the Grafana and api Pods,
    // which this api's ServiceAccount deliberately cannot do, so the page
    // points at the CLI instead of growing the privilege.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            observability: {
              ...observabilityDeployed,
              workload_states: [
                { name: 'grafana', state: 'ready', summary: '1/1 ready', details: '' },
              ],
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    const readyNote = screen.getByText(
      /READY means the workload is running, not that telemetry is reaching it/
    );
    expect(readyNote).toBeInTheDocument();
    // Scoped to this paragraph, not the whole page. Several cards now name the
    // same command -- the version card's no-install-record fallback (#3988),
    // and the ones #4150 and #4133 added -- so a page-wide `getByText` finds
    // more than one and throws. `getAllByText(...).length > 0` would pass
    // equally for a page that names the command anywhere EXCEPT this note,
    // which is the one place the claim is about: it is the caveat that has to
    // carry the pointer. Asserting it inside the paragraph says that, and
    // stays true however many other cards mention it.
    expect(within(readyNote).getByText('nyxgpt ops status')).toBeInTheDocument();
  });

  it('badges the observability workloads from the same vocabulary as the Pods (#3827)', async () => {
    // The card badged every Pod READY/PENDING/FAILED/SUPERSEDED and then, a
    // section lower, printed the observability workloads as grey `0/1 ready`
    // text -- one screen giving two different verdicts on the same condition,
    // which is the contradiction this issue is about. Three of those four
    // apply here: SUPERSEDED is a Pod-only answer (#3990), since a workload is
    // never the replica that got rolled past.
    server.use(
      http.get('/api/v1/infra/status', () =>
        HttpResponse.json({
          ...mockStatusKubernetesServing,
          kubernetes: {
            ...mockStatusKubernetesServing.kubernetes,
            observability: {
              ...observabilityDeployed,
              workload_states: [
                { name: 'grafana', state: 'ready', summary: '1/1 ready', details: '' },
                { name: 'prometheus', state: 'pending', summary: '0/1 ready', details: '' },
                {
                  name: 'glitchtip',
                  state: 'failed',
                  summary: 'absent',
                  details: 'Re-run `nyxgpt ops observability --kubernetes`.',
                },
              ],
            },
          },
        })
      )
    );

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    expect(screen.getByText('READY')).toBeInTheDocument();
    // The one the card used to call healthy alongside a `[FAIL]` from the
    // install for the same zero-ready condition.
    expect(screen.getByText('PENDING')).toBeInTheDocument();
    expect(screen.getByText('0/1 ready')).toBeInTheDocument();
    expect(screen.getByText('FAILED')).toBeInTheDocument();
  });

  it('falls back to the plain pod lines when the api predates pod_states', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusTerraform)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByText(/pod\/nyxgpt-api-abc123/)).toBeInTheDocument();
    });
    expect(screen.queryByText('PENDING')).not.toBeInTheDocument();
  });

  it('distinguishes the two Terraforms: local containers here, AWS provisioning below (#3804)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));

    render(<InfrastructurePage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Terraform (local containers)' })).toBeInTheDocument();
    });
    expect(screen.getByText(/containers Terraform runs on/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'AWS substrate' })).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // EC2 Mac Dedicated Host panel (#3995/#4121).
  //
  // The whole panel reached v3.0.0 with no test naming `mac_host` at all, and it
  // is nothing but branches: every Row picks between two strings. CI's 100%
  // coverage gate caught it only once an unrelated merge touched `web/`, because
  // the `web` job is conditional on the diff -- so the gap sat in the tree while
  // several runs reported green with that job skipped.
  //
  // These cover both sides of each branch the panel owns, because the panel's
  // whole job is to say which of two true-but-different things is the case: a
  // host still inside AWS's 24-hour window versus one past it, a release AWS has
  // been asked for versus one nobody has scheduled, a rate we recorded versus
  // one we did not. Getting that wrong costs real money quietly.
  describe('EC2 Mac Dedicated Host panel', () => {
    const macHost = (over = {}) => ({
      ...CLOUD_DEPLOY_UNKNOWN,
      source: 'deploy-record',
      known: true,
      commands: CLOUD_LIFECYCLE_COMMANDS,
      mac_host: {
        host_id: 'h-0abc123def456',
        instance_type: 'mac2.metal',
        region: 'us-east-1',
        availability_zone: 'us-east-1a',
        allocated_at: '2026-10-01T09:50:00Z',
        release_at: '2026-10-02T09:50:00Z',
        releasable_now: false,
        release_scheduled: false,
        // #4136: `accrued_cost` is Cost Explorer's figure and nothing else, and
        // `verified_at` is what lets the panel say "still billing" at all. The
        // fixture carries both, so the tests below exercise the confirmed case;
        // the unconfirmed one gets its own tests.
        accrued_cost: 15.6,
        accrued_source: 'aws-cost-explorer',
        spend_through: '2026-10-02',
        spend_as_of: '2026-10-02T10:00:00Z',
        estimated_cost: 15.6,
        hourly_rate: 0.65,
        currency: 'USD',
        verified_at: '2026-10-02T10:00:00Z',
        host_present: true,
        incoherent: [],
        // #4181: `usable` is the one gate the panel claims presence, billing
        // or release through -- confirmed by AWS in this run, under the
        // credentials this run resolved, over a record that does not
        // contradict itself -- and `provenance` is the sentence printed when
        // it is closed. Both are computed once, server-side, by
        // `cloud_mac.observe_host`.
        billing: true,
        usable: true,
        provenance: 'confirmed at AWS in nyxgpt (066835328281) at 2026-10-02T10:00:00Z',
        observation: {
          confirmed: true,
          confirmed_at: '2026-10-02T10:00:00Z',
          present: true,
          coherent: true,
          usable: true,
          findings: [],
          reason: '',
          profile: 'nyxgpt',
          account_id: '066835328281',
          account_label: 'nyxgpt (066835328281)',
          provenance: 'confirmed at AWS in nyxgpt (066835328281) at 2026-10-02T10:00:00Z',
        },
        ...over,
      },
    });

    const renderWith = async (over = {}) => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(macHost(over))));
      render(<InfrastructurePage />);
      await waitFor(() => {
        expect(
          screen.getByRole('heading', { name: /EC2 Mac Dedicated Host — still billing/ })
        ).toBeInTheDocument();
      });
    };

    // The same render, for the cases where the panel must NOT make the claim
    // (#4181). A separate waiter rather than a flag, because waiting on the
    // wrong heading is how a test that is supposed to prove a claim is absent
    // ends up proving nothing.
    const renderUnconfirmed = async (over = {}) => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(http.get('/api/v1/cloud/deploy', () => HttpResponse.json(macHost(over))));
      render(<InfrastructurePage />);
      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            name: /EC2 Mac Dedicated Host — recorded here, not confirmed at AWS/,
          })
        ).toBeInTheDocument();
      });
      expect(
        screen.queryByRole('heading', { name: /EC2 Mac Dedicated Host — still billing/ })
      ).toBeNull();
    };

    it('names the host, its type, location, allocation and what AWS billed', async () => {
      await renderWith();
      expect(screen.getByText('h-0abc123def456 (mac2.metal)')).toBeInTheDocument();
      expect(screen.getByText('us-east-1 / us-east-1a')).toBeInTheDocument();
      expect(screen.getByText('2026-10-01T09:50:00Z')).toBeInTheDocument();
      // #4136: the figure, and where it came from. The row this replaced read
      // `$48.44` for a host AWS had billed $12.02 for and had stopped charging
      // for two days earlier, because it multiplied a local clock by a local
      // rate -- and the 15 tests here asserted that number was rendered
      // correctly, never that it was true.
      expect(
        screen.getByText(/USD 15\.60 from AWS Cost Explorer through 2026-10-02/)
      ).toBeInTheDocument();
    });

    it('falls back to the bare host id and unknown location when AWS reported neither', async () => {
      await renderWith({ instance_type: '', region: '', availability_zone: '', allocated_at: '' });
      expect(screen.getByText('h-0abc123def456')).toBeInTheDocument();
      expect(screen.getByText('unknown / unknown')).toBeInTheDocument();
    });

    it('says the 24-hour minimum has not passed while the window is open', async () => {
      await renderWith({ releasable_now: false });
      expect(screen.getByText(/AWS’s 24-hour minimum/)).toBeInTheDocument();
    });

    it('says the moment has passed once the window closes', async () => {
      await renderWith({ releasable_now: true });
      expect(screen.getByText(/that moment has passed/)).toBeInTheDocument();
    });

    it('points at the wrapped destroy command when no release is scheduled', async () => {
      await renderWith({ release_scheduled: false });
      // One assertion on the whole Row value: the command also appears in the
      // cards above, so matching it alone finds several elements.
      expect(
        screen.getByText(/not scheduled yet — .*nyxgpt cloud destroy --yes.* terminates the Mac/)
      ).toBeInTheDocument();
    });

    it('reports a scheduled release as scheduled, not as released', async () => {
      await renderWith({ release_scheduled: true, releasable_now: false });
      expect(screen.getByText(/scheduled — a one-shot AWS schedule releases it/)).toBeInTheDocument();
    });

    // The distinction the panel exists to make: a schedule that has FIRED is
    // still not "released", because nothing here watched it happen.
    it('refuses to claim a fired schedule released', async () => {
      await renderWith({ release_scheduled: true, releasable_now: true });
      expect(screen.getByText(/the scheduled release has fired/)).toBeInTheDocument();
      expect(screen.getByText(/nothing here watched it/)).toBeInTheDocument();
    });

    // #4136. The heading is a claim about AWS, so it is only made when AWS made
    // it. This panel said "still billing" over a host that had been released
    // three days earlier, because the local record was all anything asked.
    it('does not say still billing over a host AWS has not confirmed', async () => {
      await renderUnconfirmed({
        verified_at: '',
        host_present: null,
        billing: false,
        usable: false,
        provenance:
          'recorded on this machine; NOT confirmed at AWS -- nothing on this machine has ' +
          'asked AWS about it in this run',
      });
      expect(
        screen.getAllByText(/nothing on this machine has asked AWS about it in this run/).length
      ).toBeGreaterThan(0);
      expect(
        screen.getByText(/Every row below is what this machine RECORDED/)
      ).toBeInTheDocument();
    });

    // #4181 finding 6. The panel used to say "Nothing has asked AWS about this
    // host yet" whatever the cause -- including a run whose boto3 was missing
    // and a run that asked an account that does not own the host, which reports
    // every host it does not own as absent. The reason is now carried in the
    // payload and rendered verbatim.
    it('names the reason AWS could not be asked, not a remedy for a different one', async () => {
      await renderUnconfirmed({
        verified_at: '',
        host_present: null,
        billing: false,
        usable: false,
        provenance:
          'recorded on this machine; NOT confirmed at AWS -- boto3 is not installed, so ' +
          'nothing here can ask AWS',
      });
      expect(screen.getAllByText(/boto3 is not installed/).length).toBeGreaterThan(0);
      expect(screen.queryByText(/Nothing has asked AWS about this host yet/)).toBeNull();
    });

    // #4181 finding 3. A confirmation proves the HOST exists; it does not make
    // a self-contradicting block describe that host. This panel printed "still
    // billing" immediately above its own "internally inconsistent" warning
    // about the same record, and rendered the release conclusions from the
    // fields that warning had just disqualified.
    it('withdraws the billing claim over a record that contradicts itself', async () => {
      await renderUnconfirmed({
        billing: false,
        usable: false,
        release_scheduled: true,
        releasable_now: true,
        provenance:
          'confirmed at AWS at 2026-10-02T10:00:00Z, but this record contradicts itself',
        incoherent: ['mac_release_scheduled_at is EARLIER than mac_allocated_at'],
      });
      expect(screen.getByText(/This record is internally inconsistent/)).toBeInTheDocument();
      expect(screen.getByText(/recorded as scheduled — not verified in this run/)).toBeInTheDocument();
      expect(screen.queryByText(/the scheduled release has fired/)).toBeNull();
    });

    it('names the moment AWS confirmed the host', async () => {
      await renderWith();
      // Twice on the page -- in the heading and in its own row -- so the row is
      // found by its label rather than by the timestamp alone.
      expect(screen.getByText('Confirmed at AWS')).toBeInTheDocument();
      // #4181: the moment AND the account it was confirmed in. An AWS account
      // reports every host it does not own as absent, so a confirmation that
      // cannot name its account is not one this panel may rely on.
      expect(
        screen.getByText('2026-10-02T10:00:00Z in nyxgpt (066835328281)')
      ).toBeInTheDocument();
    });

    // #4136. Reported, not used: each of these is detectable with no API call,
    // and means the block's fields came from different runs about different
    // hosts -- so no two rows below can be read together.
    it('reports an incoherent record instead of presenting it as agreed', async () => {
      await renderUnconfirmed({
        billing: false,
        usable: false,
        provenance:
          'confirmed at AWS at 2026-10-02T10:00:00Z, but this record contradicts itself',
        incoherent: [
          'mac_release_scheduled_at (2026-10-03T19:25:47+00:00) is EARLIER than ' +
            'mac_allocated_at (2026-10-03T19:27:05+00:00)',
        ],
      });
      expect(screen.getByText(/This record is internally inconsistent/)).toBeInTheDocument();
      expect(screen.getByText(/is EARLIER than/)).toBeInTheDocument();
    });

    it('labels the local figure as an estimate when AWS has no answer', async () => {
      await renderWith({
        accrued_cost: null,
        estimated_cost: 48.44,
        spend_error: 'Cost Explorer reported no Dedicated Host charges',
      });
      expect(screen.getByText(/Local ESTIMATE only: USD 48\.44/)).toBeInTheDocument();
      expect(
        screen.getByText(/it keeps counting whether or not AWS is still charging/)
      ).toBeInTheDocument();
    });

    // The `|| 'unknown'` fallbacks on the Releasable row (:1497, :1498). A host
    // AWS returned without a release timestamp still has to render both sides of
    // the window, because "we do not know when" is the case most worth seeing.
    it('says unknown rather than blank when AWS reported no release time', async () => {
      await renderWith({ release_at: '', releasable_now: false });
      expect(screen.getByText(/unknown \(AWS’s 24-hour minimum\)/)).toBeInTheDocument();
    });

    it('says unknown on a closed window with no release time either', async () => {
      await renderWith({ release_at: '', releasable_now: true });
      expect(screen.getByText(/unknown — that moment has passed/)).toBeInTheDocument();
    });

    // `cloud?.commands?.destroy ?? 'nyxgpt cloud destroy --yes'` (:1509): an
    // older deploy record carries no command table, and the row must still name
    // the command that schedules the release.
    it('names a default destroy command when the record carries no command table', async () => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(
        http.get('/api/v1/cloud/deploy', () =>
          HttpResponse.json({
            ...macHost({ release_scheduled: false }),
            commands: undefined,
          })
        )
      );
      render(<InfrastructurePage />);
      await waitFor(() => {
        expect(
          screen.getByRole('heading', { name: /EC2 Mac Dedicated Host — still billing/ })
        ).toBeInTheDocument();
      });
      expect(
        screen.getByText(/not scheduled yet — .*nyxgpt cloud destroy --yes.* terminates the Mac/)
      ).toBeInTheDocument();
    });

    it('says the spend is unknown when there is neither an AWS figure nor a rate', async () => {
      await renderWith({ accrued_cost: null, estimated_cost: null, hourly_rate: null });
      expect(
        screen.getByText(/unknown — no AWS figure and no rate was recorded for this host/)
      ).toBeInTheDocument();
    });

    it('omits the panel entirely when no Mac host is allocated', async () => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(
        http.get('/api/v1/cloud/deploy', () =>
          HttpResponse.json({ ...CLOUD_DEPLOY_UNKNOWN, known: true, mac_host: null })
        )
      );
      render(<InfrastructurePage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'AWS substrate' })).toBeInTheDocument();
      });
      expect(
        screen.queryByRole('heading', { name: /EC2 Mac Dedicated Host/ })
      ).not.toBeInTheDocument();
    });

    // `cloud.commands?.deploy ?? 'nyxgpt cloud deploy'` (page.tsx:1273): an older
    // deploy record carries no command table, and the card must still name a
    // command rather than printing nothing.
    it('names a default deploy command when the record carries no command table', async () => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(
        http.get('/api/v1/cloud/deploy', () =>
          HttpResponse.json({
            ...CLOUD_DEPLOY_UNKNOWN,
            source: 'deploy-attempt',
            known: true,
            deployed: false,
            host: '',
            instance_id: '',
            instance_type: '',
            commands: undefined,
            attempt: { status: 'failed', phase: 'infra', error: 'terraform init failed' },
          })
        )
      );
      render(<InfrastructurePage />);
      await waitFor(() => {
        expect(screen.getByText(/Nothing is recorded as provisioned by this attempt/)).toBeInTheDocument();
      });
      // Scoped to the attempt paragraph: the same command is named by the
      // cards above, so an unscoped match is ambiguous.
      const para = screen.getByText(/Nothing is recorded as provisioned by this attempt/);
      expect(within(para).getByText('nyxgpt cloud deploy')).toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------------------
  // The `??` and `: ''` fallbacks on the out-of-scope and attempt cards.
  //
  // Each of these is the page refusing to render a blank where an explanation
  // belongs. They were the last uncovered branches in the file, and they are
  // uncovered for the same reason every time: the fixtures all supply the
  // optional field, so the arm that runs when the api DOESN'T send it never
  // executes. An api that omits a reason is not hypothetical -- an older one
  // simply did not have the field.
  describe('fallbacks when the api sends no reason', () => {
    it('explains a native install being out of scope even with no reason given', async () => {
      server.use(
        http.get('/api/v1/infra/status', () =>
          HttpResponse.json({
            ...mockStatusInCluster,
            install_mode: { ...mockStatusInCluster.install_mode, out_of_scope_reason: undefined },
          })
        )
      );

      render(<InfrastructurePage />);

      await waitFor(() => {
        expect(
          screen.getByText(/Not in scope from here: this API is running inside a Kubernetes Pod\./)
        ).toBeInTheDocument();
      });
      expect(screen.getByText(/to survey a native install there/)).toBeInTheDocument();
    });

    it('explains Compose being out of scope even with no reason given', async () => {
      server.use(
        http.get('/api/v1/infra/status', () =>
          HttpResponse.json({ ...mockStatusInCluster, compose_probe_reason: undefined })
        )
      );

      render(<InfrastructurePage />);

      await waitFor(() => {
        expect(
          screen.getByText(/no host filesystem and no Docker socket/)
        ).toBeInTheDocument();
      });
    });

    it('reports an unreadable native probe with no reason attached', async () => {
      server.use(
        http.get('/api/v1/infra/status', () =>
          HttpResponse.json({
            ...mockStatusEmpty,
            mode: 'native',
            native: { api: 'started', web: 'started', cassandra: 'unknown' },
            native_probe_available: false,
            native_probe_reason: undefined,
          })
        )
      );

      render(<InfrastructurePage />);

      await waitFor(() => {
        expect(screen.getByText(/predates its/)).toBeInTheDocument();
      });
      // No "Reason:" clause, because there was no reason to print.
      expect(screen.queryByText(/^Reason:/)).not.toBeInTheDocument();
    });

    it('prints the Terraform probe reason when one is given', async () => {
      server.use(
        http.get('/api/v1/infra/status', () =>
          HttpResponse.json({
            ...mockStatusEmpty,
            mode: 'terraform',
            terraform: {
              probe_available: false,
              deployed: false,
              containers: {},
              probe_reason: '`docker ps` exited 1: cannot connect to the Docker daemon',
            },
          })
        )
      );

      render(<InfrastructurePage />);

      await waitFor(() => {
        expect(
          screen.getByText(/cannot connect to the Docker daemon/)
        ).toBeInTheDocument();
      });
    });

    // `cloud.attempt?.phase ? … : ''` and `cloud.attempt?.error ? … : '.'`
    // (:1254-1255). A recorded attempt that names neither a phase nor an error
    // still has to read as a sentence rather than trail off.
    it('reads as a sentence when an attempt names neither phase nor error', async () => {
      server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusEmpty)));
      server.use(
        http.get('/api/v1/cloud/deploy', () =>
          HttpResponse.json({
            ...CLOUD_DEPLOY_UNKNOWN,
            source: 'deploy-attempt',
            known: true,
            deployed: false,
            commands: CLOUD_LIFECYCLE_COMMANDS,
            attempt: { status: 'failed' },
          })
        )
      );

      render(<InfrastructurePage />);

      await waitFor(() => {
        expect(
          screen.getByText(/A deploy started on this machine and did not finish\./)
        ).toBeInTheDocument();
      });
    });
  });
});

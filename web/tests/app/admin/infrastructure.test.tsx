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
};

const mockStatusTerraform = {
  mode: 'terraform',
  native: {},
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

const mockStatusCannotDetermine = {
  mode: 'none',
  native: {},
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
    expect(screen.getByText(/No kubeconfig current-context configured/)).toBeInTheDocument();
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
    expect(screen.getByText(/no marker for this deployment/)).toBeInTheDocument();
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
  });

  it('tells the operator how to deploy the observability layer when the cluster has none (#3787)', async () => {
    server.use(http.get('/api/v1/infra/status', () => HttpResponse.json(mockStatusKubernetesServing)));

    render(<InfrastructurePage />);

    expect(
      await screen.findByRole('heading', { name: 'In-cluster observability' })
    ).toBeInTheDocument();
    expect(screen.getByText(/No observability workloads in the/)).toBeInTheDocument();
    expect(
      screen.getByText('nyxgpt ops observability --kubernetes --local')
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
      within(block).getByText('nyxgpt ops install --kubernetes --local')
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
});

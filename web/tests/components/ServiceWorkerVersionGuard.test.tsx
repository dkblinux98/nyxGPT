/**
 * ServiceWorkerVersionGuard tests (#3857)
 *
 * The guard is what stops an upgrade from stranding a client: on every load
 * it hands the running build's stamp to the reconciler, which drops the
 * previous build's asset caches. It must render nothing and must run exactly
 * once per mount.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import ServiceWorkerVersionGuard from '../../src/components/ServiceWorkerVersionGuard';

const reconcile = vi.fn(async () => 'unchanged' as const);
let fingerprint: string | undefined = 'build-1';

vi.mock('../../src/lib/serviceWorkerRecovery', () => ({
  reconcileServiceWorkerToBuild: (buildId: string | undefined) => reconcile(buildId),
}));
vi.mock('../../src/lib/buildFingerprint', () => ({
  buildFingerprint: () => fingerprint,
}));

beforeEach(() => {
  reconcile.mockClear();
  fingerprint = 'build-1';
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ServiceWorkerVersionGuard', () => {
  it('renders nothing', () => {
    const { container } = render(<ServiceWorkerVersionGuard />);
    expect(container.firstChild).toBeNull();
  });

  it('reconciles the service worker against the running build on mount', async () => {
    render(<ServiceWorkerVersionGuard />);
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith('build-1'));
  });

  it('passes the undefined stamp through when the build carries no markers', async () => {
    fingerprint = undefined;
    render(<ServiceWorkerVersionGuard />);
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(undefined));
  });
});

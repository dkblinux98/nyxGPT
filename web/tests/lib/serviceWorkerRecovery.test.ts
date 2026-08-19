/**
 * serviceWorkerRecovery tests (#3857)
 *
 * A service worker survives an ordinary reload, so once it is answering the
 * running build's chunk URLs from a previous build's caches the user has no
 * way out by refreshing. These cover both escape routes: the automatic
 * build-change reconciliation, and the manual purge behind the chunk-failure
 * surface. Both are best-effort by design and must never throw.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  BUILD_ID_STORAGE_KEY,
  hasControllingServiceWorker,
  reconcileServiceWorkerToBuild,
  recoverAndReload,
} from '../../src/lib/serviceWorkerRecovery';

type FakeRegistration = { update: ReturnType<typeof vi.fn>; unregister: ReturnType<typeof vi.fn> };

let deletedCaches: string[];
let cacheNames: string[];
let registrations: FakeRegistration[];

function makeRegistration(): FakeRegistration {
  return { update: vi.fn(async () => undefined), unregister: vi.fn(async () => true) };
}

/** Install a fake `caches` and `navigator.serviceWorker` on the global. */
function installBrowserDoubles(options: { controller?: unknown } = {}) {
  Object.defineProperty(globalThis, 'caches', {
    configurable: true,
    writable: true,
    value: {
      keys: vi.fn(async () => [...cacheNames]),
      delete: vi.fn(async (name: string) => {
        deletedCaches.push(name);
        return true;
      }),
    },
  });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    writable: true,
    value: {
      controller: 'controller' in options ? options.controller : null,
      getRegistrations: vi.fn(async () => [...registrations]),
    },
  });
}

function removeServiceWorker() {
  // @ts-expect-error - deleting an optional browser API for this test
  delete navigator.serviceWorker;
}

beforeEach(() => {
  deletedCaches = [];
  cacheNames = ['static-resources', 'workbox-precache-v2-http://127.0.0.1:3000/', 'api-cache'];
  registrations = [makeRegistration()];
  window.localStorage.clear();
  installBrowserDoubles();
});

afterEach(() => {
  // @ts-expect-error - remove the doubles again
  delete globalThis.caches;
  removeServiceWorker();
  vi.restoreAllMocks();
});

describe('hasControllingServiceWorker', () => {
  it('is false when no service worker controls the page', () => {
    expect(hasControllingServiceWorker()).toBe(false);
  });

  it('is true when one does', () => {
    installBrowserDoubles({ controller: {} });
    expect(hasControllingServiceWorker()).toBe(true);
  });

  it('is false when the browser has no service worker support', () => {
    removeServiceWorker();
    expect(hasControllingServiceWorker()).toBe(false);
  });
});

describe('reconcileServiceWorkerToBuild', () => {
  it('does nothing when the running build carries no stamp', async () => {
    await expect(reconcileServiceWorkerToBuild(undefined)).resolves.toBe('no-build-id');
    expect(deletedCaches).toEqual([]);
  });

  it('does nothing when the browser has no service worker support', async () => {
    removeServiceWorker();
    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('unsupported');
    expect(deletedCaches).toEqual([]);
  });

  it('does nothing when localStorage is unavailable', async () => {
    // Private-browsing / blocked-storage: with no record of the previous
    // build there is nothing to compare, so the guard must not guess.
    const real = window.localStorage;
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('storage disabled');
      },
    });

    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('unsupported');
    expect(deletedCaches).toEqual([]);

    Object.defineProperty(window, 'localStorage', { configurable: true, value: real });
  });

  it('drops the stale asset caches and updates the worker on a build change', async () => {
    window.localStorage.setItem(BUILD_ID_STORAGE_KEY, 'build-1');

    await expect(reconcileServiceWorkerToBuild('build-2')).resolves.toBe('refreshed');

    // The two buckets that can answer with a previous build's JS/CSS...
    expect(deletedCaches).toEqual([
      'static-resources',
      'workbox-precache-v2-http://127.0.0.1:3000/',
    ]);
    // ...and not the API response cache, which is unrelated to this failure.
    expect(deletedCaches).not.toContain('api-cache');
    expect(registrations[0].update).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(BUILD_ID_STORAGE_KEY)).toBe('build-2');
  });

  it('treats a first-ever visit as a build change', async () => {
    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('refreshed');
    expect(window.localStorage.getItem(BUILD_ID_STORAGE_KEY)).toBe('build-1');
  });

  it('leaves caches alone when the build is unchanged', async () => {
    window.localStorage.setItem(BUILD_ID_STORAGE_KEY, 'build-1');

    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('unchanged');

    expect(deletedCaches).toEqual([]);
    expect(registrations[0].update).not.toHaveBeenCalled();
  });

  it('still drops the caches when the worker update call fails', async () => {
    // Dropping the caches is the half that actually unblocks chunk loading,
    // so an update() failure must not abandon it.
    (navigator.serviceWorker.getRegistrations as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('no worker'),
    );
    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('refreshed');
    expect(deletedCaches).toContain('static-resources');
  });

  it('survives a Cache Storage that throws', async () => {
    (caches.keys as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('denied'));
    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('refreshed');
  });

  it('survives a browser with no Cache Storage', async () => {
    // @ts-expect-error - simulate a browser without Cache Storage
    delete globalThis.caches;
    await expect(reconcileServiceWorkerToBuild('build-1')).resolves.toBe('refreshed');
  });
});

describe('recoverAndReload', () => {
  it('unregisters every worker, deletes every cache, then reloads', async () => {
    registrations = [makeRegistration(), makeRegistration()];
    const reload = vi.fn();

    await recoverAndReload(reload);

    expect(registrations[0].unregister).toHaveBeenCalledTimes(1);
    expect(registrations[1].unregister).toHaveBeenCalledTimes(1);
    // Everything, including api-cache: this is the last-resort escape hatch.
    expect(deletedCaches).toEqual([
      'static-resources',
      'workbox-precache-v2-http://127.0.0.1:3000/',
      'api-cache',
    ]);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('reloads anyway when unregistering fails', async () => {
    (navigator.serviceWorker.getRegistrations as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('nope'),
    );
    const reload = vi.fn();

    await recoverAndReload(reload);

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('reloads on a browser with no service worker support', async () => {
    removeServiceWorker();
    const reload = vi.fn();

    await recoverAndReload(reload);

    expect(reload).toHaveBeenCalledTimes(1);
  });
});

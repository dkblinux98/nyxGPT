/**
 * Service-worker recovery for a client stranded on a stale build (#3857).
 *
 * `web/next.config.ts` registers a Workbox service worker whose
 * `runtimeCaching` includes a `StaleWhileRevalidate` rule for every `.js` and
 * `.css` request, in a `static-resources` cache with no expiration. Together
 * with the Workbox precache, that gives an old build's service worker a
 * durable say in how the running build's chunk URLs are answered -- and a
 * service worker survives an ordinary reload, so a user cannot get out of it
 * by refreshing.
 *
 * Two entry points here:
 *   - `reconcileServiceWorkerToBuild` runs on every load and drops the asset
 *     caches whenever the running build differs from the one the browser last
 *     saw, so an upgrade cannot leave a client serving the previous build's
 *     chunks.
 *   - `recoverAndReload` is the escape hatch behind the chunk-failure surface:
 *     unregister everything, delete every cache, reload from the network.
 *
 * Every function here is best-effort and never throws: recovery must not fail
 * in a way that leaves the user with no way forward.
 */

/** localStorage key holding the build id the browser last loaded. */
export const BUILD_ID_STORAGE_KEY = 'nyxgpt.build-id';

/**
 * Cache Storage buckets that can hold a previous build's JS/CSS: the
 * `static-resources` runtime cache from `next.config.ts` and Workbox's own
 * precaches (`workbox-precache-v2-...`).
 */
function isStaleAssetCache(cacheName: string): boolean {
  return cacheName === 'static-resources' || cacheName.startsWith('workbox-precache');
}

/**
 * True when a service worker is currently controlling this page.
 *
 * `navigator` is guarded rather than assumed: `ChunkErrorBoundary` calls this
 * from `getDerivedStateFromError`, which also runs during server rendering,
 * and Node does not define a global `navigator` before v21. Throwing there
 * would replace whatever error the boundary was handed with a ReferenceError
 * -- masking the real fault with a bug in the code meant to report it.
 */
export function hasControllingServiceWorker(): boolean {
  if (typeof navigator === 'undefined') return false;
  return 'serviceWorker' in navigator && navigator.serviceWorker.controller !== null;
}

/** Delete every Cache Storage bucket matching `predicate`. Never throws. */
async function deleteCaches(predicate: (name: string) => boolean): Promise<number> {
  if (typeof caches === 'undefined') return 0;
  try {
    const names = await caches.keys();
    const doomed = names.filter(predicate);
    await Promise.all(doomed.map((name) => caches.delete(name)));
    return doomed.length;
  } catch {
    return 0;
  }
}

/**
 * Outcome of a build reconciliation, returned so the caller (and the tests)
 * can tell the cases apart:
 *   - `no-build-id`  -- the running build is unstamped; nothing to compare
 *   - `unsupported`  -- no service worker / no storage in this environment
 *   - `unchanged`    -- same build as the last load; nothing to do
 *   - `refreshed`    -- a different build; asset caches dropped, SW updated
 */
export type ReconcileResult = 'no-build-id' | 'unsupported' | 'unchanged' | 'refreshed';

/**
 * Compare the running build id against the one the browser last recorded and,
 * if they differ, force the service worker to re-check the server and drop
 * every cache that could still be answering with the previous build's assets.
 */
export async function reconcileServiceWorkerToBuild(
  buildId: string | undefined,
): Promise<ReconcileResult> {
  if (!buildId) return 'no-build-id';
  if (!('serviceWorker' in navigator)) return 'unsupported';

  let seen: string | null = null;
  try {
    seen = window.localStorage.getItem(BUILD_ID_STORAGE_KEY);
  } catch {
    // Storage disabled (private mode, blocked cookies): without a record of
    // the previous build there is nothing to compare, so do not guess.
    return 'unsupported';
  }

  if (seen === buildId) return 'unchanged';

  await deleteCaches(isStaleAssetCache);
  try {
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map((registration) => registration.update()));
  } catch {
    // An update() that fails leaves the caches already dropped, which is the
    // half that actually unblocks chunk loading.
  }

  // Recorded only once the drop has actually happened. Stamping first would
  // make a page closed mid-recovery report `unchanged` on its next load and
  // never retry, leaving the stale caches in place permanently. A write that
  // fails here just means the next load repeats the drop -- the safe way to
  // be wrong.
  try {
    window.localStorage.setItem(BUILD_ID_STORAGE_KEY, buildId);
  } catch {
    // Storage became unavailable between the read and the write; the caches
    // are already gone, which is the part that unblocks the client.
  }
  return 'refreshed';
}

/**
 * Last-resort recovery, offered next to the chunk-failure surface: unregister
 * every service worker, delete every cache, then reload so the page is served
 * entirely from the network.
 */
export async function recoverAndReload(reload: () => void): Promise<void> {
  if ('serviceWorker' in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((registration) => registration.unregister()));
    } catch {
      // Fall through to the reload regardless -- a reload with the service
      // worker still in place is still better than staying on a dead page.
    }
  }
  await deleteCaches(() => true);
  reload();
}

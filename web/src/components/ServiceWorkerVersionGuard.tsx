'use client';

import { useEffect } from 'react';
import { buildFingerprint } from '../lib/buildFingerprint';
import { reconcileServiceWorkerToBuild } from '../lib/serviceWorkerRecovery';

/**
 * Drops the service worker's asset caches when the running build differs from
 * the one this browser last loaded (#3857).
 *
 * Without this, an upgrade can leave a client permanently broken with no way
 * out: the previous build's service worker keeps answering `.js`/`.css`
 * requests from its `static-resources` cache and its precache, a service
 * worker survives an ordinary reload, and a chunk that is served stale or not
 * at all leaves `next/dynamic` on its loading fallback forever.
 *
 * Renders nothing; safe to mount once in the root layout.
 */
export default function ServiceWorkerVersionGuard() {
  useEffect(() => {
    void reconcileServiceWorkerToBuild(buildFingerprint());
  }, []);

  return null;
}

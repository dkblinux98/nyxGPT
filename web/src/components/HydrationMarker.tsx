'use client';

import { useEffect } from 'react';
import { markHydrated } from '../lib/hydrationWatchdog';

/**
 * The other half of the hydration handshake (#3857).
 *
 * Running this effect proves the two things the document-inline watchdog is
 * waiting on: the client bundle was delivered, and React hydrated it. Setting
 * the flag disarms the watchdog; if the watchdog already painted (a client
 * that was slow rather than dead), `markHydrated` also removes its surface.
 *
 * Mounted once in the root layout so it covers every route -- including pages
 * with no `dynamic()` at all, whose server-rendered "Loading ..." states are
 * just as permanent when hydration never runs.
 */
export default function HydrationMarker() {
  useEffect(() => {
    markHydrated();
  }, []);

  return null;
}

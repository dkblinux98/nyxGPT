/**
 * Detects whether a failed `fetch()` was actually queued by the service
 * worker's workbox `backgroundSync` plugin (see `web/next.config.ts`)
 * rather than a genuine failure.
 *
 * Workbox's `NetworkOnly` strategy still rejects the calling `fetch()`
 * promise when the network is unreachable, even though its
 * `BackgroundSyncPlugin` has already queued the request for replay. Callers
 * that skip a rollback/error-toast on this signal must be certain the
 * request actually landed in the queue — otherwise a plain network error
 * (that was *not* queued) would silently keep an optimistic UI update that
 * will never be reconciled with the server.
 */
export function isQueuedForBackgroundSync(error: unknown): boolean {
  if (typeof navigator === 'undefined') return false;
  if (navigator.onLine) return false;
  if (!('serviceWorker' in navigator) || !navigator.serviceWorker?.controller) return false;
  // The service worker turns a queued-but-unreachable request into a
  // network-level failure, which the browser surfaces to fetch() callers
  // as a TypeError (e.g. "Failed to fetch").
  return error instanceof TypeError;
}

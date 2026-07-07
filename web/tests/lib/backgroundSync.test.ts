import { describe, it, expect, afterEach } from 'vitest';
import { isQueuedForBackgroundSync } from '../../src/app/lib/backgroundSync';

/**
 * Regression tests for issue #2681 code review: Workbox's NetworkOnly +
 * BackgroundSyncPlugin queues a failed request but still rejects the
 * calling fetch() promise. `isQueuedForBackgroundSync` is what call sites
 * (web/src/app/page.tsx, web/src/app/components/ChatPane.tsx) use to tell
 * a queued-for-later-replay failure apart from a real one, so they don't
 * roll back an optimistic update / show a false "failed" toast for a
 * mutation that actually succeeded (was queued).
 */

const originalOnLine = window.navigator.onLine;
const originalServiceWorker = Object.getOwnPropertyDescriptor(window.navigator, 'serviceWorker');

function setOnLine(value: boolean) {
  Object.defineProperty(window.navigator, 'onLine', { value, configurable: true });
}

function setServiceWorkerController(controller: unknown) {
  Object.defineProperty(window.navigator, 'serviceWorker', {
    value: controller === undefined ? undefined : { controller },
    configurable: true,
  });
}

afterEach(() => {
  setOnLine(originalOnLine);
  if (originalServiceWorker) {
    Object.defineProperty(window.navigator, 'serviceWorker', originalServiceWorker);
  } else {
    // @ts-expect-error - restoring to "not present" for environments without it
    delete window.navigator.serviceWorker;
  }
});

describe('isQueuedForBackgroundSync', () => {
  it('returns true for a network TypeError while offline with an active service worker', () => {
    setOnLine(false);
    setServiceWorkerController({});

    expect(isQueuedForBackgroundSync(new TypeError('Failed to fetch'))).toBe(true);
  });

  it('returns false when the browser reports itself online (a genuine failure)', () => {
    setOnLine(true);
    setServiceWorkerController({});

    expect(isQueuedForBackgroundSync(new TypeError('Failed to fetch'))).toBe(false);
  });

  it('returns false when there is no active service worker controlling the page', () => {
    setOnLine(false);
    setServiceWorkerController(undefined);

    expect(isQueuedForBackgroundSync(new TypeError('Failed to fetch'))).toBe(false);
  });

  it('returns false for a non-network error (e.g. a thrown HTTP error) even while offline', () => {
    setOnLine(false);
    setServiceWorkerController({});

    expect(isQueuedForBackgroundSync(new Error('Delete failed'))).toBe(false);
  });

  it('returns false for non-Error values', () => {
    setOnLine(false);
    setServiceWorkerController({});

    expect(isQueuedForBackgroundSync('some string')).toBe(false);
    expect(isQueuedForBackgroundSync(undefined)).toBe(false);
  });
});

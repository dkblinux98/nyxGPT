import { describe, it, expect, vi, beforeAll } from 'vitest';
import withPWA from '@ducanh2912/next-pwa';

/**
 * Background Sync Tests (issue #2681)
 *
 * Validates the workbox `backgroundSync` runtime-caching configuration in
 * next.config.ts, which queues session-mutation requests (rename, pin,
 * unpin, delete, title, sync-filename, document delete) made while offline
 * and replays them once connectivity returns.
 *
 * @ducanh2912/next-pwa is mocked so the options object passed into
 * `withPWA(options)` can be captured directly, without invoking the real
 * plugin (which touches the filesystem to emit a service worker).
 */

vi.mock('@ducanh2912/next-pwa', () => ({
  default: vi.fn(() => (config: unknown) => config),
}));

type RuntimeCachingEntry = {
  urlPattern: RegExp;
  method?: string;
  handler: string;
  options?: {
    backgroundSync?: {
      name: string;
      options?: { maxRetentionTime?: number };
    };
  };
};

let runtimeCaching: RuntimeCachingEntry[];

beforeAll(async () => {
  await import('../next.config');
  const mockedWithPWA = vi.mocked(withPWA);
  const pwaOptions = mockedWithPWA.mock.calls[0][0] as {
    workboxOptions: { runtimeCaching: RuntimeCachingEntry[] };
  };
  runtimeCaching = pwaOptions.workboxOptions.runtimeCaching;
});

function findBackgroundSyncEntries(): RuntimeCachingEntry[] {
  return runtimeCaching.filter((entry) => entry.options?.backgroundSync);
}

describe('Background sync – runtime caching configuration', () => {
  it('registers at least one runtimeCaching entry with a backgroundSync plugin', () => {
    expect(findBackgroundSyncEntries().length).toBeGreaterThan(0);
  });

  it('uses the NetworkOnly handler for background-synced routes', () => {
    for (const entry of findBackgroundSyncEntries()) {
      expect(entry.handler).toBe('NetworkOnly');
    }
  });

  it('queues session mutation POST requests (rename, pin, unpin, delete, title, sync-filename)', () => {
    const entry = findBackgroundSyncEntries().find((e) => e.method === 'POST');
    expect(entry, 'expected a POST runtimeCaching entry with backgroundSync').toBeDefined();

    const matches = [
      '/api/sessions/my-session/rename',
      '/api/sessions/my-session/pin',
      '/api/sessions/my-session/unpin',
      '/api/sessions/my-session/delete',
      '/api/sessions/my-session/title',
      '/api/sessions/my-session/sync-filename',
    ];
    for (const url of matches) {
      expect(entry!.urlPattern.test(url), `expected pattern to match ${url}`).toBe(true);
    }

    // Should not swallow unrelated GET-style session routes into the same queue.
    const nonMatches = ['/api/sessions', '/api/sessions/my-session/documents', '/api/sessions/init'];
    for (const url of nonMatches) {
      expect(entry!.urlPattern.test(url), `expected pattern to reject ${url}`).toBe(false);
    }
  });

  it('queues document DELETE requests', () => {
    const entry = findBackgroundSyncEntries().find((e) => e.method === 'DELETE');
    expect(entry, 'expected a DELETE runtimeCaching entry with backgroundSync').toBeDefined();
    expect(entry!.urlPattern.test('/api/sessions/my-session/documents/doc-123')).toBe(true);
    expect(entry!.urlPattern.test('/api/sessions/my-session/documents')).toBe(false);
  });

  it('retains queued requests for 24 hours so they survive short offline periods', () => {
    for (const entry of findBackgroundSyncEntries()) {
      expect(entry.options?.backgroundSync?.options?.maxRetentionTime).toBe(24 * 60);
    }
  });

  it('gives every background-synced route a named queue', () => {
    for (const entry of findBackgroundSyncEntries()) {
      expect(entry.options?.backgroundSync?.name).toBeTruthy();
    }
  });

  it('never reuses a backgroundSync queue name across routes (#3445)', () => {
    // Workbox creates one Queue per runtimeCaching entry with a
    // `backgroundSync` option and throws `duplicate-queue-name` if two
    // Queues in the same generated service worker share a name -- this
    // wedged every client's service worker until the DELETE route's queue
    // was renamed off the POST route's "session-mutations-queue".
    const names = findBackgroundSyncEntries().map((e) => e.options?.backgroundSync?.name);
    expect(new Set(names).size).toBe(names.length);
  });
});

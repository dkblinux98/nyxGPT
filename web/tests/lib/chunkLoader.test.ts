/**
 * withChunkTimeout tests (#3857)
 *
 * The defect these cover: a `next/dynamic` chunk import that never settles
 * leaves the `loading:` fallback on screen forever -- no error, no retry, and
 * no way for the user to tell a permanently broken client from a slow one.
 * withChunkTimeout must convert "still pending" into a rejection, while
 * leaving a loader that settles on its own completely untouched.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  CHUNK_LOAD_TIMEOUT_MS,
  ChunkLoadTimeoutError,
  withChunkTimeout,
} from '../../src/lib/chunkLoader';
import { isChunkLoadError } from '../../src/hooks/useAppUpdate';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('withChunkTimeout', () => {
  it('resolves with the loader value when the chunk arrives', async () => {
    const wrapped = withChunkTimeout(async () => 'chunk', 'Thing');
    await expect(wrapped()).resolves.toBe('chunk');
  });

  it('propagates the loader rejection unchanged', async () => {
    const failure = new Error('network down');
    const wrapped = withChunkTimeout(() => Promise.reject(failure), 'Thing');
    await expect(wrapped()).rejects.toBe(failure);
  });

  it('rejects with ChunkLoadTimeoutError when the import never settles', async () => {
    vi.useFakeTimers();
    // The failure mode from #3857: a request that is neither answered nor
    // refused, so the promise stays pending for the life of the page.
    const wrapped = withChunkTimeout(() => new Promise<string>(() => {}), 'ChatPane', 50);

    const pending = wrapped();
    const assertion = expect(pending).rejects.toBeInstanceOf(ChunkLoadTimeoutError);
    await vi.advanceTimersByTimeAsync(50);
    await assertion;
  });

  it('names the timeout error so existing stale-build detection still fires', async () => {
    vi.useFakeTimers();
    const wrapped = withChunkTimeout(() => new Promise<string>(() => {}), 'ChatPane', 50);

    const pending = wrapped().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(50);
    const error = (await pending) as ChunkLoadTimeoutError;

    expect(error.name).toBe('ChunkLoadError');
    expect(error.chunkName).toBe('ChatPane');
    expect(error.timeoutMs).toBe(50);
    expect(error.message).toContain('ChatPane');
    // #3445's detector must recognise a hung chunk exactly like a failed one.
    expect(isChunkLoadError(error)).toBe(true);
  });

  it('clears the timer once the loader settles, so no late rejection fires', async () => {
    vi.useFakeTimers();
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');

    const resolved = withChunkTimeout(async () => 'ok', 'Thing', 50);
    await expect(resolved()).resolves.toBe('ok');

    const rejected = withChunkTimeout(() => Promise.reject(new Error('nope')), 'Thing', 50);
    await expect(rejected()).rejects.toThrow('nope');

    expect(clearSpy).toHaveBeenCalledTimes(2);
  });

  it('defaults to a timeout generous enough for a real page load', () => {
    expect(CHUNK_LOAD_TIMEOUT_MS).toBeGreaterThanOrEqual(10_000);
  });

  it('does not start loading until the returned loader is called', () => {
    const load = vi.fn(async () => 'ok');
    const wrapped = withChunkTimeout(load, 'Thing');
    expect(load).not.toHaveBeenCalled();
    void wrapped();
    expect(load).toHaveBeenCalledTimes(1);
  });
});

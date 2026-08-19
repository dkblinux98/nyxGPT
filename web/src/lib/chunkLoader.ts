/**
 * Bounded loading for `next/dynamic` chunk imports (#3857).
 *
 * A `next/dynamic` component renders its `loading:` fallback until the
 * component's JS chunk resolves. If that request never settles -- a service
 * worker that intercepts it and never responds, a request left pending
 * against a web tier that was swapped underneath the page -- the fallback
 * renders forever: no error, no timeout, no retry. The screen is then
 * indistinguishable from "still working" while the client is permanently
 * broken, which is exactly what #3857 reported: skeleton rows and a spinner
 * on screen while every backend endpoint answered in under 110 ms.
 *
 * Racing the import against a timer turns "never settles" into a rejection.
 * React then throws it out of the lazy component's render, where
 * `ChunkErrorBoundary` can show a visible, actionable failure instead.
 */

/**
 * How long a chunk import may stay pending before it is treated as failed.
 *
 * Generous on purpose: nyxGPT's web tier is normally on the same host as the
 * browser, so a chunk that has not arrived in 20 s is not slow, it is stuck.
 */
export const CHUNK_LOAD_TIMEOUT_MS = 20_000;

/**
 * Rejection raised when a chunk import is still pending at the timeout.
 *
 * `name` is deliberately `ChunkLoadError` -- the name webpack uses when a
 * chunk fails outright -- so the stale-build detection already in
 * `useAppUpdate` (`isChunkLoadError`, #3445) treats a hung chunk exactly like
 * a failed one and its "reload to update" banner still fires.
 */
export class ChunkLoadTimeoutError extends Error {
  readonly chunkName: string;
  readonly timeoutMs: number;

  constructor(chunkName: string, timeoutMs: number) {
    super(`Loading chunk ${chunkName} failed: still pending after ${timeoutMs}ms`);
    this.name = 'ChunkLoadError';
    this.chunkName = chunkName;
    this.timeoutMs = timeoutMs;
  }
}

/**
 * Wrap a `next/dynamic` loader so a chunk that never arrives rejects instead
 * of hanging. A loader that resolves or rejects on its own is passed through
 * untouched (and cancels the timer), so this only changes the pending-forever
 * case.
 */
export function withChunkTimeout<T>(
  load: () => Promise<T>,
  chunkName: string,
  timeoutMs: number = CHUNK_LOAD_TIMEOUT_MS,
): () => Promise<T> {
  return () =>
    new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new ChunkLoadTimeoutError(chunkName, timeoutMs));
      }, timeoutMs);

      load().then(
        (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        (error: unknown) => {
          clearTimeout(timer);
          reject(error);
        },
      );
    });
}

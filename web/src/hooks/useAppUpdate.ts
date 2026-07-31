'use client';

import { useCallback, useEffect, useState } from 'react';

const CHUNK_LOAD_ERROR_PATTERN = /ChunkLoadError|Loading (chunk|CSS chunk) [\w.-]+ failed/i;

/**
 * True when `reason` looks like a webpack/Next.js dynamic-import chunk
 * failure -- the signature left behind when a client is still running an
 * old build whose chunk files no longer exist on disk after a rebuild
 * (#3445).
 */
export function isChunkLoadError(reason: unknown): boolean {
  if (reason instanceof Error) {
    return reason.name === 'ChunkLoadError' || CHUNK_LOAD_ERROR_PATTERN.test(reason.message);
  }
  return typeof reason === 'string' && CHUNK_LOAD_ERROR_PATTERN.test(reason);
}

/**
 * True when a capturing-phase `error` event target was a failed
 * `<script>`/`<link>` load for a Next.js build asset (`/_next/static/...`)
 * -- the same stale-chunk signature as `isChunkLoadError`, surfaced as a
 * resource error instead of an unhandled rejection.
 */
export function isStaleAssetLoadError(target: EventTarget | null): boolean {
  const src =
    target instanceof HTMLScriptElement
      ? target.src
      : target instanceof HTMLLinkElement
        ? target.href
        : undefined;
  return src !== undefined && src.includes('/_next/static/');
}

/**
 * useAppUpdate -- detects when the running client is stranded on a stale
 * build after a web rebuild (#3445) and exposes a `reload()` action so the
 * UI can offer an actionable prompt instead of an infinite spinner or a
 * silently wedged service worker.
 *
 * `updateAvailable` flips to `true` on any of:
 *   - a chunk/CSS-chunk load failure (unhandled promise rejection)
 *   - a failed resource load for a `/_next/static/` script or stylesheet
 *   - the service worker controller changing after this page was already
 *     under an existing SW's control (a genuine version swap -- the very
 *     first `clientsClaim` on an uncontrolled page is not a swap and is
 *     ignored so first-time visitors don't see a spurious prompt)
 */
export function useAppUpdate(): { updateAvailable: boolean; reload: () => void } {
  const [updateAvailable, setUpdateAvailable] = useState(false);

  useEffect(() => {
    function handleRejection(event: PromiseRejectionEvent) {
      if (isChunkLoadError(event.reason)) setUpdateAvailable(true);
    }

    function handleResourceError(event: Event) {
      if (isStaleAssetLoadError(event.target)) setUpdateAvailable(true);
    }

    window.addEventListener('unhandledrejection', handleRejection);
    window.addEventListener('error', handleResourceError, true);

    const hasServiceWorker = 'serviceWorker' in navigator;
    let hadController = hasServiceWorker && navigator.serviceWorker.controller !== null;
    function handleControllerChange() {
      if (!hadController) {
        hadController = true;
        return;
      }
      setUpdateAvailable(true);
    }
    if (hasServiceWorker) {
      navigator.serviceWorker.addEventListener('controllerchange', handleControllerChange);
    }

    return () => {
      window.removeEventListener('unhandledrejection', handleRejection);
      window.removeEventListener('error', handleResourceError, true);
      if (hasServiceWorker) {
        navigator.serviceWorker.removeEventListener('controllerchange', handleControllerChange);
      }
    };
  }, []);

  const reload = useCallback(() => {
    window.location.reload();
  }, []);

  return { updateAvailable, reload };
}

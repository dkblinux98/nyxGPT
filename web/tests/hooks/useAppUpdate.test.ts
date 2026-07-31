/**
 * useAppUpdate hook tests (#3445)
 *
 * Verifies the stranded-client recovery hook detects:
 *   - chunk/CSS-chunk load failures (unhandled promise rejections)
 *   - failed resource loads for /_next/static/ scripts and stylesheets
 *   - a genuine service worker version swap (controllerchange after this
 *     page was already controlled), while ignoring the first-ever
 *     clientsClaim on an uncontrolled page
 * and that `reload()` reloads the page.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { isChunkLoadError, isStaleAssetLoadError, useAppUpdate } from '../../src/hooks/useAppUpdate';

class FakeServiceWorkerContainer extends EventTarget {
  controller: unknown = null;
}

function installServiceWorker(initialController: unknown = null): FakeServiceWorkerContainer {
  const container = new FakeServiceWorkerContainer();
  container.controller = initialController;
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: container,
  });
  return container;
}

afterEach(() => {
  // @ts-expect-error - test-only cleanup of a property we may have defined
  delete navigator.serviceWorker;
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// isChunkLoadError
// ---------------------------------------------------------------------------

describe('isChunkLoadError', () => {
  it('recognizes an Error named ChunkLoadError', () => {
    const err = new Error('boom');
    err.name = 'ChunkLoadError';
    expect(isChunkLoadError(err)).toBe(true);
  });

  it('recognizes an Error whose message matches the loading-chunk pattern', () => {
    expect(isChunkLoadError(new Error('Loading chunk 4.abcd1234.js failed'))).toBe(true);
    expect(isChunkLoadError(new Error('Loading CSS chunk 2.abcd1234.css failed'))).toBe(true);
  });

  it('rejects an unrelated Error', () => {
    expect(isChunkLoadError(new Error('network error'))).toBe(false);
  });

  it('recognizes a matching string reason', () => {
    expect(isChunkLoadError('ChunkLoadError: Loading chunk 4 failed')).toBe(true);
  });

  it('rejects an unrelated string reason', () => {
    expect(isChunkLoadError('some other rejection')).toBe(false);
  });

  it('rejects a non-Error, non-string reason', () => {
    expect(isChunkLoadError(undefined)).toBe(false);
    expect(isChunkLoadError(42)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// isStaleAssetLoadError
// ---------------------------------------------------------------------------

describe('isStaleAssetLoadError', () => {
  it('recognizes a failed /_next/static/ script load', () => {
    const script = document.createElement('script');
    script.src = 'https://example.com/_next/static/chunks/main-app-abcd1234.js';
    expect(isStaleAssetLoadError(script)).toBe(true);
  });

  it('recognizes a failed /_next/static/ stylesheet load', () => {
    const link = document.createElement('link');
    link.href = 'https://example.com/_next/static/css/abcd1234.css';
    expect(isStaleAssetLoadError(link)).toBe(true);
  });

  it('rejects a script load outside /_next/static/', () => {
    const script = document.createElement('script');
    script.src = 'https://example.com/analytics.js';
    expect(isStaleAssetLoadError(script)).toBe(false);
  });

  it('rejects a non-script, non-link target', () => {
    expect(isStaleAssetLoadError(document.createElement('div'))).toBe(false);
    expect(isStaleAssetLoadError(null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// useAppUpdate
// ---------------------------------------------------------------------------

describe('useAppUpdate', () => {
  it('starts with updateAvailable false', () => {
    const { result } = renderHook(() => useAppUpdate());
    expect(result.current.updateAvailable).toBe(false);
  });

  it('sets updateAvailable when a chunk-load rejection is unhandled', () => {
    const { result } = renderHook(() => useAppUpdate());
    const event = new Event('unhandledrejection') as unknown as PromiseRejectionEvent;
    const err = new Error('boom');
    err.name = 'ChunkLoadError';
    Object.defineProperty(event, 'reason', { value: err });

    act(() => {
      window.dispatchEvent(event);
    });

    expect(result.current.updateAvailable).toBe(true);
  });

  it('ignores an unrelated unhandled rejection', () => {
    const { result } = renderHook(() => useAppUpdate());
    const event = new Event('unhandledrejection') as unknown as PromiseRejectionEvent;
    Object.defineProperty(event, 'reason', { value: new Error('network error') });

    act(() => {
      window.dispatchEvent(event);
    });

    expect(result.current.updateAvailable).toBe(false);
  });

  it('sets updateAvailable when a /_next/static/ script fails to load', () => {
    const { result } = renderHook(() => useAppUpdate());
    // Connect the element before setting `src` via a plain data property
    // (bypassing the `src` setter) so happy-dom never attempts a real
    // script load -- only the synthetic 'error' event below matters here.
    const script = document.createElement('script');
    document.body.appendChild(script);
    Object.defineProperty(script, 'src', {
      configurable: true,
      value: 'https://example.com/_next/static/chunks/main-app-abcd1234.js',
    });

    act(() => {
      script.dispatchEvent(new Event('error'));
    });

    expect(result.current.updateAvailable).toBe(true);
    document.body.removeChild(script);
  });

  it('ignores a resource error unrelated to /_next/static/', () => {
    const { result } = renderHook(() => useAppUpdate());
    const img = document.createElement('img');
    document.body.appendChild(img);

    act(() => {
      img.dispatchEvent(new Event('error'));
    });

    expect(result.current.updateAvailable).toBe(false);
    document.body.removeChild(img);
  });

  it('ignores the first controllerchange on a page with no prior controller', () => {
    const sw = installServiceWorker(null);
    const { result, unmount } = renderHook(() => useAppUpdate());

    act(() => {
      sw.dispatchEvent(new Event('controllerchange'));
    });

    expect(result.current.updateAvailable).toBe(false);
    unmount();
  });

  it('sets updateAvailable on a controllerchange after the first', () => {
    const sw = installServiceWorker(null);
    const { result, unmount } = renderHook(() => useAppUpdate());

    act(() => {
      sw.dispatchEvent(new Event('controllerchange'));
    });
    expect(result.current.updateAvailable).toBe(false);

    act(() => {
      sw.dispatchEvent(new Event('controllerchange'));
    });
    expect(result.current.updateAvailable).toBe(true);
    unmount();
  });

  it('treats a controllerchange as a real swap when the page was already controlled', () => {
    const sw = installServiceWorker({ id: 'existing-worker' });
    const { result, unmount } = renderHook(() => useAppUpdate());

    act(() => {
      sw.dispatchEvent(new Event('controllerchange'));
    });

    expect(result.current.updateAvailable).toBe(true);
    unmount();
  });

  it('does nothing when the browser has no serviceWorker support', () => {
    const { result, unmount } = renderHook(() => useAppUpdate());
    expect(result.current.updateAvailable).toBe(false);
    unmount();
  });

  it('removes all listeners on unmount', () => {
    const sw = installServiceWorker(null);
    const removeWindowSpy = vi.spyOn(window, 'removeEventListener');
    const removeSwSpy = vi.spyOn(sw, 'removeEventListener');

    const { unmount } = renderHook(() => useAppUpdate());
    unmount();

    const removedTypes = removeWindowSpy.mock.calls.map((call) => call[0]);
    expect(removedTypes).toContain('unhandledrejection');
    expect(removedTypes).toContain('error');
    expect(removeSwSpy).toHaveBeenCalledWith('controllerchange', expect.any(Function));
  });

  it('reloads the page via reload()', () => {
    const { result } = renderHook(() => useAppUpdate());
    const originalLocation = window.location;
    const reload = vi.fn();
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    window.location = { ...originalLocation, reload };

    act(() => {
      result.current.reload();
    });

    expect(reload).toHaveBeenCalledTimes(1);
    window.location = originalLocation;
  });
});

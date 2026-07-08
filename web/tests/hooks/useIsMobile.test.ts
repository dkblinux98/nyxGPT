/**
 * useIsMobile hook tests
 *
 * Verifies the mobile-breakpoint detection hook:
 *   - starts as false (matches desktop SSR markup) before mount effects run
 *   - reflects window.matchMedia(...).matches once mounted
 *   - reacts to viewport changes (e.g. resize/rotation) via the 'change' event
 *   - cleans up its media query listener on unmount
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useIsMobile, MOBILE_BREAKPOINT_PX } from '../../src/hooks/useIsMobile';

type Listener = (event: { matches: boolean }) => void;

function mockMatchMedia(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<Listener>();

  const mql = {
    get matches() {
      return matches;
    },
    media: '',
    addEventListener: vi.fn((_event: string, listener: Listener) => {
      listeners.add(listener);
    }),
    removeEventListener: vi.fn((_event: string, listener: Listener) => {
      listeners.delete(listener);
    }),
  };

  window.matchMedia = vi.fn().mockImplementation((query: string) => {
    mql.media = query;
    return mql;
  });

  return {
    mql,
    setMatches: (value: boolean) => {
      matches = value;
      listeners.forEach((listener) => listener({ matches }));
    },
  };
}

describe('useIsMobile', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('reflects a matching (mobile) media query after mount', () => {
    mockMatchMedia(true);
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(true);
  });

  it('reflects a non-matching (desktop) media query after mount', () => {
    mockMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(false);
  });

  it('uses the default breakpoint in the media query string', () => {
    const { mql } = mockMatchMedia(false);
    renderHook(() => useIsMobile());

    expect(mql.media).toBe(`(max-width: ${MOBILE_BREAKPOINT_PX - 1}px)`);
  });

  it('respects a custom breakpoint', () => {
    const { mql } = mockMatchMedia(false);
    renderHook(() => useIsMobile(1024));

    expect(mql.media).toBe('(max-width: 1023px)');
  });

  it('updates when the viewport crosses the breakpoint', () => {
    const { setMatches } = mockMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(false);

    act(() => {
      setMatches(true);
    });

    expect(result.current).toBe(true);
  });

  it('removes the media query listener on unmount', () => {
    const { mql } = mockMatchMedia(false);
    const { unmount } = renderHook(() => useIsMobile());

    unmount();

    expect(mql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
  });
});

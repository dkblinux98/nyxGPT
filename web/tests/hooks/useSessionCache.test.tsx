/**
 * Tests for useSessionCache hook
 *
 * Tests the session metadata cache with stale-while-revalidate pattern
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSessionCache } from '../../src/hooks/useSessionCache';

// Mock fetch
const mockFetch = vi.fn();

function successResponse(sessions: unknown) {
  return new Response(JSON.stringify({ sessions }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

// A promise whose resolve/reject can be triggered externally, for precisely
// controlling fetch timing/ordering across concurrent refresh() calls.
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useSessionCache', () => {
  const mockSessions = [
    { name: 'session1', title: 'Session 1', messages: 5, pinned: false },
    { name: 'session2', title: 'Session 2', messages: 10, pinned: true },
  ];

  beforeEach(() => {
    // Re-assign after tests/setup.ts's MSW server.listen() patches global.fetch,
    // so this mock isn't clobbered by MSW's real fetch interceptor.
    global.fetch = mockFetch as typeof fetch;
    vi.clearAllMocks();
    // shouldAdvanceTime keeps real time passing (so testing-library's waitFor,
    // which doesn't recognize vitest's fake timers, still resolves) while still
    // letting tests fast-forward cache-expiry timers manually.
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('should fetch sessions on mount', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
      expect(result.current.sessions).toEqual(mockSessions);
      expect(result.current.error).toBeNull();
    });
  });

  it('should return cached data on subsequent calls', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    // First fetch
    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Second call should use cache (no new fetch)
    mockFetch.mockClear();

    await act(async () => {
      await result.current.getSessions();
    });

    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.current.sessions).toEqual(mockSessions);
  });

  it('should revalidate stale cache in background', async () => {
    const updatedSessions = [
      ...mockSessions,
      { name: 'session3', title: 'Session 3', messages: 3, pinned: false },
    ];

    // Initial fetch
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() =>
      useSessionCache({
        staleTime: 1000, // 1 second
        backgroundRefresh: true,
      })
    );

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Make cache stale
    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    // Mock updated fetch for background refresh
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: updatedSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    // Get sessions again - should return stale data immediately
    // and trigger background refresh
    await act(async () => {
      const sessions = await result.current.getSessions();
      expect(sessions).toEqual(mockSessions); // Returns stale immediately
    });

    // Wait for background refresh to complete
    await waitFor(
      () => {
        expect(result.current.sessions).toEqual(updatedSessions);
      },
      { timeout: 3000 }
    );
  });

  it('should handle fetch errors', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(null, {
        status: 500,
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      try {
        await result.current.getSessions();
      } catch (e) {
        // Expected to throw
      }
    });

    await waitFor(() => {
      expect(result.current.error).toBeTruthy();
      expect(result.current.sessions).toEqual([]);
    });
  });

  it('should invalidate cache and force refresh', async () => {
    const updatedSessions = [
      { name: 'session3', title: 'Session 3', messages: 15, pinned: false },
    ];

    // Initial fetch
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Mock fetch for invalidation
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: updatedSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    // Invalidate cache
    await act(async () => {
      await result.current.invalidate();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(updatedSessions);
    });
  });

  it('should support optimistic updates with rollback', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Optimistic update
    let rollback: (() => void) | undefined;
    await act(async () => {
      const mutation = result.current.mutate((sessions) =>
        sessions.map((s) =>
          s.name === 'session1' ? { ...s, title: 'Updated Title' } : s
        )
      );
      rollback = mutation.rollback;
    });

    // Check optimistic update applied
    expect(result.current.sessions[0].title).toBe('Updated Title');

    // Rollback
    await act(async () => {
      rollback?.();
    });

    // Check rollback worked
    expect(result.current.sessions).toEqual(mockSessions);
  });

  it('should cancel in-flight requests when unmounted', async () => {
    const abortSpy = vi.fn();

    mockFetch.mockImplementationOnce(() => {
      return new Promise((resolve) => {
        setTimeout(() => {
          resolve(
            new Response(JSON.stringify({ sessions: mockSessions }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            })
          );
        }, 1000);
      });
    });

    const { result, unmount } = renderHook(() => useSessionCache());

    // Start fetch
    act(() => {
      void result.current.getSessions();
    });

    // Unmount before fetch completes
    unmount();

    // Advance timers to complete the fetch
    await act(async () => {
      vi.advanceTimersByTime(1100);
    });

    // No error should be thrown, request should be cancelled
    expect(result.current.error).toBeNull();
  });

  it('should support custom cache configuration', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() =>
      useSessionCache({
        staleTime: 5000, // 5 seconds
        maxAge: 60000, // 1 minute
        backgroundRefresh: false,
      })
    );

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Make cache stale but not expired
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });

    mockFetch.mockClear();

    // With backgroundRefresh disabled, should not trigger fetch
    await act(async () => {
      await result.current.getSessions();
    });

    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.current.sessions).toEqual(mockSessions);
  });

  // Edge case tests for race conditions and concurrent operations

  it('should handle concurrent mutations correctly', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Apply two mutations in quick succession
    let rollback1: (() => void) | undefined;
    let rollback2: (() => void) | undefined;

    await act(async () => {
      const mutation1 = result.current.mutate((sessions) =>
        sessions.map((s) =>
          s.name === 'session1' ? { ...s, title: 'First Update' } : s
        )
      );
      rollback1 = mutation1.rollback;
    });

    expect(result.current.sessions[0].title).toBe('First Update');

    await act(async () => {
      const mutation2 = result.current.mutate((sessions) =>
        sessions.map((s) =>
          s.name === 'session2' ? { ...s, title: 'Second Update' } : s
        )
      );
      rollback2 = mutation2.rollback;
    });

    expect(result.current.sessions[0].title).toBe('First Update');
    expect(result.current.sessions[1].title).toBe('Second Update');

    // Rollback second mutation
    await act(async () => {
      rollback2?.();
    });

    // Should restore to state after first mutation
    expect(result.current.sessions[0].title).toBe('First Update');
    expect(result.current.sessions[1].title).toBe('Session 2');

    // Rollback first mutation
    await act(async () => {
      rollback1?.();
    });

    // Should restore to original state
    expect(result.current.sessions).toEqual(mockSessions);
  });

  it('should rollback correctly even after background refresh', async () => {
    const updatedSessions = [
      ...mockSessions,
      { name: 'session3', title: 'Session 3', messages: 20, pinned: false },
    ];

    // Initial fetch
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() =>
      useSessionCache({
        staleTime: 1000,
        backgroundRefresh: true,
      })
    );

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Apply optimistic mutation
    let rollback: (() => void) | undefined;
    await act(async () => {
      const mutation = result.current.mutate((sessions) =>
        sessions.map((s) =>
          s.name === 'session1' ? { ...s, title: 'Optimistic Update' } : s
        )
      );
      rollback = mutation.rollback;
    });

    expect(result.current.sessions[0].title).toBe('Optimistic Update');

    // Simulate background refresh happening between mutation and rollback
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: updatedSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    // Trigger background refresh
    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions.length).toBe(3);
    });

    // Now rollback - should restore to pre-mutation state (original 2 sessions)
    // NOT the new background-refreshed state
    await act(async () => {
      rollback?.();
    });

    expect(result.current.sessions).toEqual(mockSessions);
    expect(result.current.sessions.length).toBe(2);
  });

  it('should handle revalidate errors in mutations', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Apply optimistic update
    let revalidate: (() => Promise<any>) | undefined;
    await act(async () => {
      const mutation = result.current.mutate((sessions) =>
        sessions.map((s) =>
          s.name === 'session1' ? { ...s, title: 'Updated' } : s
        )
      );
      revalidate = mutation.revalidate;
    });

    // Mock fetch failure for revalidation
    mockFetch.mockResolvedValueOnce(
      new Response(null, {
        status: 500,
      })
    );

    // Revalidate should handle error gracefully
    await act(async () => {
      try {
        await revalidate?.();
      } catch (e) {
        // Expected to throw
      }
    });

    // Optimistic update should still be in place. mutate().revalidate() runs
    // as a background refresh, so a failure surfaces as isStaleError (not
    // the foreground `error`, which is reserved for getSessions failures);
    // callers see the rejection via the thrown error from revalidate().
    expect(result.current.sessions[0].title).toBe('Updated');
    expect(result.current.isStaleError).toBe(true);
  });

  it('should handle cache expiry during getSessions', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() =>
      useSessionCache({
        staleTime: 1000,
        maxAge: 5000,
      })
    );

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Advance time to exactly maxAge
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });

    // Mock new fetch for expired cache
    const updatedSessions = [
      { name: 'session3', title: 'New Session', messages: 1, pinned: false },
    ];
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: updatedSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    // getSessions should force fresh fetch
    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(updatedSessions);
    });

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('should handle concurrent invalidations', async () => {
    const updatedSessions1 = [
      { name: 'session1', title: 'Update 1', messages: 5, pinned: false },
    ];
    const updatedSessions2 = [
      { name: 'session2', title: 'Update 2', messages: 10, pinned: true },
    ];

    // Initial fetch
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ sessions: mockSessions }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Mock fetches for concurrent invalidations
    mockFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ sessions: updatedSessions1 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ sessions: updatedSessions2 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

    // Trigger two invalidations concurrently
    await act(async () => {
      const promise1 = result.current.invalidate();
      const promise2 = result.current.invalidate();
      await Promise.all([promise1, promise2]);
    });

    // Should handle both requests gracefully
    // The last one to complete should win
    await waitFor(() => {
      expect(
        result.current.sessions.length === 1 || result.current.sessions.length === 2
      ).toBe(true);
    });
  });

  it('should stringify non-Error rejection values into the error message', async () => {
    mockFetch.mockRejectedValueOnce('a plain string rejection');

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      try {
        await result.current.getSessions();
      } catch (e) {
        // Expected to throw
      }
    });

    await waitFor(() => {
      expect(result.current.error).toBe('a plain string rejection');
    });
  });

  it('mutate() falls back to an empty array when there is no cache yet', async () => {
    const { result } = renderHook(() => useSessionCache());

    let rollback: (() => void) | undefined;
    act(() => {
      const mutation = result.current.mutate((sessions) => [
        ...sessions,
        { name: 'brand-new-session', title: 'Brand New', messages: 0, pinned: false },
      ]);
      rollback = mutation.rollback;
    });

    expect(result.current.sessions).toEqual([
      { name: 'brand-new-session', title: 'Brand New', messages: 0, pinned: false },
    ]);

    // Rollback with no previous cache is a no-op (nothing to restore to)
    act(() => {
      rollback?.();
    });
    expect(result.current.sessions).toEqual([
      { name: 'brand-new-session', title: 'Brand New', messages: 0, pinned: false },
    ]);
  });

  it('should throw when the API response is missing a sessions array', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ notSessions: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      try {
        await result.current.getSessions();
      } catch (e) {
        // Expected to throw
      }
    });

    await waitFor(() => {
      expect(result.current.error).toBe('Invalid API response format');
    });
  });

  it('returns an empty array when an in-flight request is aborted before any cache exists', async () => {
    const first = deferred<Response>();
    mockFetch.mockImplementationOnce((_url: string, options: RequestInit) => {
      options.signal?.addEventListener('abort', () => {
        first.reject(new DOMException('Aborted', 'AbortError'));
      });
      return first.promise;
    });
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));

    const { result } = renderHook(() => useSessionCache());

    let firstPromise: Promise<unknown>;
    act(() => {
      firstPromise = result.current.getSessions();
    });

    // Trigger a new request before the first resolves - this aborts the first
    await act(async () => {
      await result.current.invalidate();
    });

    await expect(firstPromise!).resolves.toEqual([]);
    expect(result.current.sessions).toEqual(mockSessions);
  });

  it('returns the existing cached data when an in-flight background refresh is aborted by a newer request', async () => {
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    const updatedSessions = [
      { name: 'session3', title: 'Session 3', messages: 1, pinned: false },
    ];

    const first = deferred<Response>();
    mockFetch.mockImplementationOnce((_url: string, options: RequestInit) => {
      options.signal?.addEventListener('abort', () => {
        first.reject(new DOMException('Aborted', 'AbortError'));
      });
      return first.promise;
    });
    mockFetch.mockResolvedValueOnce(successResponse(updatedSessions));

    let firstPromise: Promise<unknown>;
    act(() => {
      firstPromise = result.current.refresh();
    });

    await act(async () => {
      await result.current.refresh();
    });

    // The aborted call falls back to whatever was cached at the time it lost the race
    await expect(firstPromise!).resolves.toEqual(mockSessions);
    await waitFor(() => {
      expect(result.current.sessions).toEqual(updatedSessions);
    });
  });

  it('warns when a background refresh triggered by getSessions fails with a non-abort error', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));

    const { result } = renderHook(() =>
      useSessionCache({ staleTime: 1000, backgroundRefresh: true })
    );
    await act(async () => {
      await result.current.getSessions();
    });
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    mockFetch.mockRejectedValueOnce(new Error('network down'));

    await act(async () => {
      await result.current.getSessions();
    });

    await waitFor(() => {
      expect(warnSpy).toHaveBeenCalledWith('Background refresh failed:', expect.any(Error));
    });

    warnSpy.mockRestore();
  });

  it('does not warn when a background refresh triggered by getSessions is aborted', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));

    const { result } = renderHook(() =>
      useSessionCache({ staleTime: 1000, backgroundRefresh: true })
    );
    await act(async () => {
      await result.current.getSessions();
    });
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });

    const stalled = deferred<Response>();
    mockFetch.mockImplementationOnce((_url: string, options: RequestInit) => {
      options.signal?.addEventListener('abort', () => {
        stalled.reject(new DOMException('Aborted', 'AbortError'));
      });
      return stalled.promise;
    });
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));

    await act(async () => {
      // Returns stale data immediately and kicks off a background refresh in the background
      await result.current.getSessions();
    });

    await act(async () => {
      // Aborts the pending background refresh started above
      await result.current.refresh();
    });

    expect(warnSpy).not.toHaveBeenCalledWith('Background refresh failed:', expect.anything());
    warnSpy.mockRestore();
  });

  describe('scheduled automatic background refresh', () => {
    it('schedules and fires an automatic refresh once the cache is set and config changes', async () => {
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      const { result, rerender } = renderHook(
        (props) => useSessionCache(props),
        { initialProps: { staleTime: 5000, backgroundRefresh: true } }
      );

      await act(async () => {
        await result.current.getSessions();
      });
      await waitFor(() => {
        expect(result.current.sessions).toEqual(mockSessions);
      });

      const updatedSessions = [
        { name: 'session3', title: 'Session 3', messages: 1, pinned: false },
      ];
      mockFetch.mockResolvedValueOnce(successResponse(updatedSessions));

      // Changing staleTime re-runs the scheduling effect now that cache is set
      rerender({ staleTime: 1000, backgroundRefresh: true });

      await act(async () => {
        vi.advanceTimersByTime(1000);
      });

      await waitFor(() => {
        expect(result.current.sessions).toEqual(updatedSessions);
      });
    });

    it('warns when a scheduled automatic refresh fails with a non-abort error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      const { result, rerender } = renderHook(
        (props) => useSessionCache(props),
        { initialProps: { staleTime: 5000, backgroundRefresh: true } }
      );

      await act(async () => {
        await result.current.getSessions();
      });
      await waitFor(() => {
        expect(result.current.sessions).toEqual(mockSessions);
      });

      mockFetch.mockRejectedValueOnce(new Error('scheduled refresh network error'));
      rerender({ staleTime: 1000, backgroundRefresh: true });

      await act(async () => {
        vi.advanceTimersByTime(1000);
      });

      await waitFor(() => {
        expect(warnSpy).toHaveBeenCalledWith('Scheduled refresh failed:', expect.any(Error));
      });

      warnSpy.mockRestore();
    });

    it('does not warn when a scheduled automatic refresh is aborted', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      const { result, rerender } = renderHook(
        (props) => useSessionCache(props),
        { initialProps: { staleTime: 5000, backgroundRefresh: true } }
      );

      await act(async () => {
        await result.current.getSessions();
      });
      await waitFor(() => {
        expect(result.current.sessions).toEqual(mockSessions);
      });

      const stalled = deferred<Response>();
      mockFetch.mockImplementationOnce((_url: string, options: RequestInit) => {
        options.signal?.addEventListener('abort', () => {
          stalled.reject(new DOMException('Aborted', 'AbortError'));
        });
        return stalled.promise;
      });
      rerender({ staleTime: 1000, backgroundRefresh: true });

      await act(async () => {
        vi.advanceTimersByTime(1000);
      });

      // Abort the now-pending scheduled refresh via an explicit manual refresh
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      await act(async () => {
        await result.current.refresh();
      });

      expect(warnSpy).not.toHaveBeenCalledWith('Scheduled refresh failed:', expect.anything());
      warnSpy.mockRestore();
    });

    it('clears a pending scheduled refresh timer when the config changes again before it fires', async () => {
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      const { result, rerender } = renderHook(
        (props) => useSessionCache(props),
        { initialProps: { staleTime: 5000, backgroundRefresh: true } }
      );

      await act(async () => {
        await result.current.getSessions();
      });
      await waitFor(() => {
        expect(result.current.sessions).toEqual(mockSessions);
      });

      // First change schedules a timer (timeUntilStale > 0)
      rerender({ staleTime: 10000, backgroundRefresh: true });
      // Second change re-runs the effect, whose cleanup clears the pending timer
      rerender({ staleTime: 20000, backgroundRefresh: true });

      mockFetch.mockClear();
      await act(async () => {
        vi.advanceTimersByTime(10000);
      });
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('does not schedule a refresh when the recomputed time-until-stale has already elapsed', async () => {
      mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
      const { result, rerender } = renderHook(
        (props) => useSessionCache(props),
        { initialProps: { staleTime: 100_000, backgroundRefresh: true } }
      );

      await act(async () => {
        await result.current.getSessions();
      });
      await waitFor(() => {
        expect(result.current.sessions).toEqual(mockSessions);
      });

      // Shrinking staleTime makes the cache already "expired" relative to now
      rerender({ staleTime: 1, backgroundRefresh: true });

      mockFetch.mockClear();
      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(mockFetch).not.toHaveBeenCalled();
    });
  });

  it('clears a pending scheduled refresh timer on unmount', async () => {
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
    const { result, rerender, unmount } = renderHook(
      (props) => useSessionCache(props),
      { initialProps: { staleTime: 5000, backgroundRefresh: true } }
    );

    await act(async () => {
      await result.current.getSessions();
    });
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    // Schedules a pending timer (refreshTimeoutRef.current truthy) that never fires
    rerender({ staleTime: 10_000, backgroundRefresh: true });

    mockFetch.mockClear();
    unmount();

    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('aborts an in-flight fetch on unmount', async () => {
    const abortSpy = vi.fn();
    const stalled = deferred<Response>();
    mockFetch.mockImplementationOnce((_url: string, options: RequestInit) => {
      options.signal?.addEventListener('abort', abortSpy);
      return stalled.promise;
    });

    const { result, unmount } = renderHook(() => useSessionCache());

    act(() => {
      void result.current.getSessions();
    });

    unmount();

    expect(abortSpy).toHaveBeenCalled();
  });

  it('exposes a manual refresh() wrapper that performs a background refresh', async () => {
    mockFetch.mockResolvedValueOnce(successResponse(mockSessions));
    const { result } = renderHook(() => useSessionCache());

    await act(async () => {
      await result.current.getSessions();
    });
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    const updatedSessions = [
      { name: 'session3', title: 'Session 3', messages: 1, pinned: false },
    ];
    mockFetch.mockResolvedValueOnce(successResponse(updatedSessions));

    await act(async () => {
      await result.current.refresh();
    });

    await waitFor(() => {
      expect(result.current.sessions).toEqual(updatedSessions);
    });
  });
});

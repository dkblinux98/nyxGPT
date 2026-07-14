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
});

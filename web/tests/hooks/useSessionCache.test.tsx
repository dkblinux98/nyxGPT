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
global.fetch = mockFetch as typeof fetch;

describe('useSessionCache', () => {
  const mockSessions = [
    { name: 'session1', title: 'Session 1', messages: 5, pinned: false },
    { name: 'session2', title: 'Session 2', messages: 10, pinned: true },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
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
});

/**
 * Session metadata cache hook with stale-while-revalidate pattern
 *
 * Features:
 * - Client-side caching of session metadata
 * - Stale-while-revalidate: return cached data immediately, fetch in background
 * - Automatic cache invalidation with TTL
 * - Background refresh for stale entries
 * - Optimistic updates with rollback
 */

import { useCallback, useEffect, useRef, useState } from 'react';

type Session = {
  name: string;
  modified?: string;
  messages?: number;
  pinned?: boolean;
  tags?: string[];
  title?: string;
  summary?: string;
  token_estimate?: number;
  model?: string;
};

type CacheEntry = {
  data: Session[];
  timestamp: number;
  isStale: boolean;
};

type CacheConfig = {
  /** Time in ms before cache is considered stale (default: 30s) */
  staleTime?: number;
  /** Time in ms before cache is invalidated (default: 5 minutes) */
  maxAge?: number;
  /** Enable automatic background refresh when stale (default: true) */
  backgroundRefresh?: boolean;
};

const DEFAULT_CONFIG: Required<CacheConfig> = {
  staleTime: 30_000, // 30 seconds
  maxAge: 300_000, // 5 minutes
  backgroundRefresh: true,
};

/**
 * Session metadata cache hook
 *
 * Returns cached sessions immediately if available (even if stale),
 * and automatically fetches fresh data in the background.
 */
export function useSessionCache(config: CacheConfig = {}) {
  const cfg = { ...DEFAULT_CONFIG, ...config };

  const [sessions, setSessions] = useState<Session[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isStaleError, setIsStaleError] = useState(false);

  const cacheRef = useRef<CacheEntry | null>(null);
  const fetchControllerRef = useRef<AbortController | null>(null);
  const refreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * Check if cache entry is stale
   */
  const isStale = useCallback((entry: CacheEntry | null): boolean => {
    if (!entry) return true;
    const age = Date.now() - entry.timestamp;
    return age > cfg.staleTime;
  }, [cfg.staleTime]);

  /**
   * Check if cache entry is expired
   */
  const isExpired = useCallback((entry: CacheEntry | null): boolean => {
    if (!entry) return true;
    const age = Date.now() - entry.timestamp;
    return age > cfg.maxAge;
  }, [cfg.maxAge]);

  /**
   * Fetch sessions from API
   */
  const fetchSessions = useCallback(async (signal?: AbortSignal): Promise<Session[]> => {
    const res = await fetch('/api/sessions', {
      cache: 'no-store',
      signal,
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();

    // Validate API response structure
    if (!data || !Array.isArray(data.sessions)) {
      throw new Error('Invalid API response format');
    }

    return data.sessions;
  }, []);

  /**
   * Update cache with fresh data
   */
  const updateCache = useCallback((data: Session[]) => {
    cacheRef.current = {
      data,
      timestamp: Date.now(),
      isStale: false,
    };
    setSessions(data);
  }, []);

  /**
   * Refresh sessions from API and update cache
   */
  const refresh = useCallback(async (background = false): Promise<Session[]> => {
    // Cancel any in-flight requests
    if (fetchControllerRef.current) {
      fetchControllerRef.current.abort();
    }

    const controller = new AbortController();
    fetchControllerRef.current = controller;

    try {
      if (background) {
        setIsRefreshing(true);
      } else {
        setIsLoading(true);
      }
      setError(null);

      const data = await fetchSessions(controller.signal);
      updateCache(data);
      // Clear stale error flag on successful refresh
      setIsStaleError(false);
      return data;
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        // Request was cancelled, don't update error state
        throw e;
      }
      const errorMsg = e instanceof Error ? e.message : String(e);
      if (background) {
        // Set stale error flag for background refresh failures
        setIsStaleError(true);
      } else {
        setError(errorMsg);
      }
      throw e;
    } finally {
      if (background) {
        setIsRefreshing(false);
      } else {
        setIsLoading(false);
      }
      fetchControllerRef.current = null;
    }
  }, [fetchSessions, updateCache]);

  /**
   * Get sessions with stale-while-revalidate pattern
   */
  const getSessions = useCallback(async (): Promise<Session[]> => {
    const cache = cacheRef.current;

    // If cache is expired, force fresh fetch
    if (isExpired(cache)) {
      return refresh(false);
    }

    // If cache exists and is fresh, return it immediately
    if (cache && !isStale(cache)) {
      setSessions(cache.data);
      setIsLoading(false);
      return cache.data;
    }

    // If cache is stale but exists, return it immediately
    // and fetch fresh data in background
    if (cache && isStale(cache)) {
      setSessions(cache.data);
      setIsLoading(false);

      if (cfg.backgroundRefresh) {
        // Fetch fresh data in background
        refresh(true).catch((e) => {
          // Ignore errors for background refresh
          if (!(e instanceof Error && e.name === 'AbortError')) {
            console.warn('Background refresh failed:', e);
          }
        });
      }

      return cache.data;
    }

    // No cache, fetch fresh data
    return refresh(false);
  }, [cfg.backgroundRefresh, isExpired, isStale, refresh]);

  /**
   * Invalidate cache and force refresh
   */
  const invalidate = useCallback(async (): Promise<Session[]> => {
    cacheRef.current = null;
    return refresh(false);
  }, [refresh]);

  /**
   * Update cache optimistically with a mutation
   */
  const mutate = useCallback((updater: (sessions: Session[]) => Session[]) => {
    // Capture entire cache entry (including timestamp) to prevent race conditions
    const previousCache = cacheRef.current;
    // Always use ref, no state fallback to avoid race conditions with async state updates
    const currentSessions = previousCache?.data ?? [];
    const optimisticSessions = updater(currentSessions);

    updateCache(optimisticSessions);

    return {
      /**
       * Rollback to previous state
       * Restores entire cache entry atomically to prevent race conditions
       * with background refreshes
       */
      rollback: () => {
        if (previousCache) {
          // Restore entire cache entry, not just data
          cacheRef.current = previousCache;
          setSessions(previousCache.data);
          // Clear error state on rollback
          setError(null);
        }
      },
      /**
       * Revalidate by fetching fresh data
       */
      revalidate: () => refresh(true),
    };
  }, [updateCache, refresh]);

  /**
   * Schedule automatic background refresh when cache becomes stale
   */
  useEffect(() => {
    if (!cfg.backgroundRefresh || !cacheRef.current) return;

    const cache = cacheRef.current;
    const timeUntilStale = cfg.staleTime - (Date.now() - cache.timestamp);

    if (timeUntilStale > 0) {
      refreshTimeoutRef.current = setTimeout(() => {
        refresh(true).catch((e) => {
          if (!(e instanceof Error && e.name === 'AbortError')) {
            console.warn('Scheduled refresh failed:', e);
          }
        });
      }, timeUntilStale);
    }

    return () => {
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
        refreshTimeoutRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // Intentionally omitting 'sessions' from deps to prevent unnecessary timer churn.
    // The effect only reads from cacheRef.current, not the sessions state.
    // refresh is stable (memoized with useCallback), so this is safe.
  }, [cfg.backgroundRefresh, cfg.staleTime, refresh]);

  /**
   * Cleanup on unmount
   */
  useEffect(() => {
    return () => {
      if (fetchControllerRef.current) {
        fetchControllerRef.current.abort();
      }
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
    };
  }, []);

  // All returned functions (getSessions, invalidate, mutate, refresh) are memoized
  // with useCallback and have stable identities across re-renders. This prevents
  // unnecessary re-renders in components that depend on these functions.
  return {
    /** Current sessions (may be stale) */
    sessions,
    /** True if initial load is in progress */
    isLoading,
    /** True if background refresh is in progress */
    isRefreshing,
    /** Error message if fetch failed */
    error,
    /** True if cache exists but is stale */
    isStale: cacheRef.current ? isStale(cacheRef.current) : false,
    /** True if background refresh failed (data may be stale) */
    isStaleError,
    /** Fetch sessions with stale-while-revalidate */
    getSessions,
    /** Force refresh and invalidate cache */
    invalidate,
    /** Optimistically update cache */
    mutate,
    /** Manually trigger background refresh */
    refresh: () => refresh(true),
  };
}

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import dynamic from 'next/dynamic';
import ThemeToggle from '../components/ThemeToggle';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import { SessionListSkeleton } from '../components/SkeletonLoader';
import { SessionListErrorBoundary } from '../components/SessionListErrorBoundary';
import { SessionCacheErrorBoundary } from '../components/SessionCacheErrorBoundary';
import { UnifiedSearch, UnifiedSearchRef } from '../components/UnifiedSearch';
import { useToast } from '../contexts/ToastContext';
import { useTheme } from '../contexts/ThemeContext';
import { useSessionCache } from '../hooks/useSessionCache';
import { useIsMobile } from '../hooks/useIsMobile';
import { useSidebarVisibility } from '../hooks/useSidebarVisibility';
import { isQueuedForBackgroundSync } from './lib/backgroundSync';

// Route-based code splitting: ChatPane is large (2000+ lines) and only needed
// after initial render; lazy-load it to reduce the initial bundle size.
const ChatPane = dynamic(() => import('./components/ChatPane'), {
  ssr: false,
  loading: () => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <LoadingSpinner />
    </div>
  ),
});

// VirtualizedSessionList depends on react-virtuoso; split into its own chunk so
// the virtuoso library is not included in the initial JS payload.
const VirtualizedSessionList = dynamic(
  () =>
    import('../components/VirtualizedSessionList').then((mod) => ({
      default: mod.VirtualizedSessionList,
    })),
  {
    ssr: false,
    loading: () => <SessionListSkeleton />,
  }
);

type Info = {
  ollama_base_url: string;
  default_model: string;
  sessions_dir: string;
  release_version: string | null;
};

type SessionsResponse = {
  sessions: Array<{
    name: string;
    modified?: string;
    messages?: number;
    pinned?: boolean;
    tags?: string[];
    title?: string;
    summary?: string;
    token_estimate?: number;
    model?: string;
  }>;
};

function Home() {
  const toast = useToast();
  const { theme, setTheme } = useTheme();
  const [info, setInfo] = useState<Info | null>(null);
  const [showSettingsMenu, setShowSettingsMenu] = useState<boolean>(false);
  const [showAdminSubmenu, setShowAdminSubmenu] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingInfo, setLoadingInfo] = useState<boolean>(true);

  // Use session cache hook with stale-while-revalidate
  const {
    sessions,
    isLoading: loadingSessions,
    isRefreshing: retryingSessions,
    error: sessionsError,
    getSessions,
    invalidate: invalidateSessions,
    mutate: mutateSessions,
  } = useSessionCache({
    staleTime: 30_000, // 30 seconds
    maxAge: 300_000, // 5 minutes
    backgroundRefresh: true,
  });

  const [selectedSession, setSelectedSession] = useState<string>('default');

  // Mobile layout: the sidebar becomes a collapsible overlay instead of a
  // permanent column, collapsed by default on mobile until the user
  // explicitly toggles it (see useSidebarVisibility for details).
  const isMobile = useIsMobile();
  const [sidebarVisible, setSidebarVisible] = useSidebarVisibility(isMobile);

  // Message search state
  const [scrollToMessageIndex, setScrollToMessageIndex] = useState<number | null>(null);

  // Filter state
  const [filterModel, setFilterModel] = useState<string>('');
  const [filterPinned, setFilterPinned] = useState<string>('all'); // 'all', 'pinned', 'unpinned'
  const [filterTags, setFilterTags] = useState<string>('');

  // Context menu state
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    sessionName: string;
  } | null>(null);
  const [deletingSession, setDeletingSession] = useState<string | null>(null);
  const [exportingSession, setExportingSession] = useState<string | null>(null);

  // Pending operations state for visual feedback
  const [pendingSessions, setPendingSessions] = useState<Set<string>>(new Set());

  // Refs for keyboard shortcuts
  const searchRef = useRef<UnifiedSearchRef>(null);
  const feedbackTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const srTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settingsMenuRef = useRef<HTMLDivElement>(null);

  // Accessibility: Screen reader announcements
  const [srAnnouncement, setSrAnnouncement] = useState<string>('');

  // Accessibility: Visual feedback for shortcuts
  const [shortcutFeedback, setShortcutFeedback] = useState<string>('');

  // Platform detection for keyboard shortcuts display
  const isMac = typeof navigator !== 'undefined' && navigator.platform.toUpperCase().indexOf('MAC') >= 0;
  const modKey = isMac ? '⌘' : 'Ctrl';

  // Fetch API info with retry support
  const fetchInfo = useCallback(async () => {
    setLoadingInfo(true);
    setError(null);

    try {
      const res = await fetch('/api/info');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setInfo(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingInfo(false);
    }
  }, []);

  useEffect(() => {
    void fetchInfo();
  }, [fetchInfo]);

  // Initialize session cache on mount
  useEffect(() => {
    void getSessions().then((sessionList) => {
      // Keep selection stable; if current selection disappears, fall back.
      const names = new Set(sessionList.map((s) => s.name));
      if (!names.has(selectedSession)) {
        setSelectedSession(names.has('default') ? 'default' : (sessionList[0]?.name ?? 'default'));
      }
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Filter sessions
  const filteredSessions = sessions.filter((session) => {
    // Model filter
    const matchesModel = !filterModel || session.model === filterModel;

    // Pinned filter
    const matchesPinned =
      filterPinned === 'all' ||
      (filterPinned === 'pinned' && session.pinned) ||
      (filterPinned === 'unpinned' && !session.pinned);

    // Tags filter
    const matchesTags =
      !filterTags ||
      session.tags?.some((tag) => tag.toLowerCase().includes(filterTags.toLowerCase()));

    return matchesModel && matchesPinned && matchesTags;
  });

  // Get unique models for filter dropdown
  const uniqueModels = Array.from(
    new Set(sessions.map((s) => s.model).filter((m): m is string => !!m))
  ).sort();

  // Clear all filters
  const clearFilters = () => {
    setFilterModel('');
    setFilterPinned('all');
    setFilterTags('');
  };

  // Refresh session list (invalidate cache and fetch fresh)
  const refreshSessions = useCallback(async () => {
    try {
      await invalidateSessions();
    } catch (e) {
      // Error is already handled by the cache hook
      console.error('Failed to refresh sessions:', e);
    }
  }, [invalidateSessions]);

  // Create new chat with optimistic update
  const createNewChat = useCallback(async () => {
    // Generate automatic session name based on timestamp
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').substring(0, 19);
    const sessionName = `chat-${timestamp}`;
    const previousSelection = selectedSession;

    // Optimistic update: add new session to cache immediately
    const newSession = {
      name: sessionName,
      messages: 0,
      pinned: false,
      tags: [],
      title: 'New Chat',
      modified: new Date().toISOString(),
    };

    const { rollback, revalidate } = mutateSessions((sessions) => [newSession, ...sessions]);
    setSelectedSession(sessionName);

    try {
      // Create the session on the backend (no system prompt - backend handles defaults)
      const res = await fetch('/api/sessions/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: sessionName,
        }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: { message: 'Unknown error' } }));
        // Handle wrapped error format {"error": {"message": "..."}} or legacy {"detail": "..."}
        const detail = errorData.error?.message
          || (typeof errorData.detail === 'string' ? errorData.detail : null)
          || (Array.isArray(errorData.detail)
            ? errorData.detail.map((e: { msg?: string }) => e.msg || JSON.stringify(e)).join(', ')
            : null)
          || `HTTP ${res.status}`;
        console.error('Session init error:', errorData);
        throw new Error(detail);
      }

      // Revalidate the cache to get actual server state
      await revalidate();
    } catch (e) {
      // Rollback on failure: restore previous cache state
      rollback();
      setSelectedSession(previousSelection);
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to create new chat: ${msg}`);
      console.error('Failed to create new chat:', e);
    }
  }, [mutateSessions, setSelectedSession, selectedSession, toast]);

  // Delete session with optimistic update
  const deleteSession = async (sessionName: string) => {
    try {
      // Check if we can delete (must have more than 1 session)
      if (sessions.length <= 1) {
        toast.warning('Cannot delete the last session');
        return;
      }

      if (!confirm(`Delete session "${sessionName}"? This cannot be undone.`)) {
        return;
      }

      setDeletingSession(sessionName);

      const previousSelection = selectedSession;

      // Optimistic update: remove session from cache immediately
      const { rollback, revalidate } = mutateSessions((sessions) =>
        sessions.filter((s) => s.name !== sessionName)
      );

      // If deleting the selected session, switch to another immediately
      if (sessionName === selectedSession) {
        const remainingSessions = sessions.filter((s) => s.name !== sessionName);
        setSelectedSession(remainingSessions[0]?.name || 'default');
      }

      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/delete`, {
          method: 'POST',
        });

        if (!res.ok) {
          const error = await res.text();
          throw new Error(error || 'Delete failed');
        }
        toast.success(`Session deleted successfully`);
        // Revalidate to ensure consistency
        await revalidate();
      } catch (e) {
        if (isQueuedForBackgroundSync(e)) {
          // Request was queued by the service worker for replay once back
          // online; keep the optimistic update instead of rolling it back.
          toast.info('You are offline — session deletion will complete when you reconnect');
          return;
        }
        // Rollback on failure: restore previous cache state
        rollback();
        setSelectedSession(previousSelection);
        const errorMsg = `Failed to delete session: ${e instanceof Error ? e.message : String(e)}`;
        toast.error(errorMsg);
      } finally {
        setDeletingSession(null);
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in deleteSession:', error);
      toast.error('An unexpected error occurred while deleting the session. Please refresh the page.');
    }
  };

  // Rename session with optimistic update
  const renameSession = async (sessionName: string) => {
    try {
      // Get current session title
      const currentSession = sessions.find((s) => s.name === sessionName);
      const currentTitle = currentSession?.title || sessionName;

      const newName = prompt('Enter new session name or title:', currentTitle);

      if (!newName || newName.trim() === '' || newName === currentTitle) return;

      // Check if operation is already in progress
      if (pendingSessions.has(sessionName)) {
        console.warn('Operation already in progress for session:', sessionName);
        return;
      }

      // Mark as pending for visual feedback
      setPendingSessions((prev) => new Set(prev).add(sessionName));

      const previousSelection = selectedSession;

      // Optimistic update: update session title in cache immediately
      const { rollback, revalidate } = mutateSessions((sessions) =>
        sessions.map((s) =>
          s.name === sessionName ? { ...s, title: newName.trim() } : s
        )
      );

      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            new_name: newName.trim(),
            sync_filename: true,
          }),
        });

        if (!res.ok) {
          const errorData = await res.json();
          throw new Error(errorData.detail || 'Rename failed');
        }

        const data = await res.json();

        // If filename changed, update selected session
        if (data.new_name !== sessionName && sessionName === selectedSession) {
          setSelectedSession(data.new_name);
        }

        toast.success('Session renamed successfully');
        // Revalidate to ensure consistency
        await revalidate();
      } catch (e) {
        if (isQueuedForBackgroundSync(e)) {
          // Request was queued by the service worker for replay once back
          // online; keep the optimistic update instead of rolling it back.
          toast.info('You are offline — rename will complete when you reconnect');
          return;
        }
        // Rollback on failure: restore previous cache state
        rollback();
        setSelectedSession(previousSelection);
        toast.error(`Failed to rename session: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        // Remove pending state
        setPendingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sessionName);
          return next;
        });
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in renameSession:', error);
      toast.error('An unexpected error occurred while renaming the session. Please refresh the page.');
    }
  };

  // Export session
  const exportSession = async (sessionName: string, format: 'markdown' | 'json' | 'html') => {
    setExportingSession(sessionName);
    try {
      const url = `/api/v1/sessions/${encodeURIComponent(sessionName)}/export?format=${format}`;

      const res = await fetch(url);
      if (!res.ok) {
        const errorText = await res.text();
        throw new Error(`Export failed (${res.status} ${res.statusText}): ${errorText}`);
      }

      // Get filename from Content-Disposition header or use default
      const contentDisposition = res.headers.get('Content-Disposition');
      let filename = `${sessionName}.${format === 'markdown' ? 'md' : format}`;
      if (contentDisposition) {
        const match = contentDisposition.match(/filename="(.+?)"/);
        if (match) filename = match[1];
      }

      // Download the file
      const blob = await res.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } finally {
        setTimeout(() => window.URL.revokeObjectURL(downloadUrl), 100);
      }
      toast.success('Session exported successfully');
    } catch (e) {
      toast.error(`Failed to export session: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setExportingSession(null);
    }
  };

  // Toggle pin status with optimistic update
  const togglePin = async (sessionName: string) => {
    try {
      // Check if operation is already in progress
      if (pendingSessions.has(sessionName)) {
        console.warn('Operation already in progress for session:', sessionName);
        return;
      }

      // Get current session state
      const currentSession = sessions.find((s) => s.name === sessionName);
      const isPinned = currentSession?.pinned || false;
      const action = isPinned ? 'unpin' : 'pin';

      // Mark as pending for visual feedback
      setPendingSessions((prev) => new Set(prev).add(sessionName));

      // Optimistic update: toggle pin status in cache immediately
      const { rollback, revalidate } = mutateSessions((sessions) =>
        sessions.map((s) =>
          s.name === sessionName ? { ...s, pinned: !isPinned } : s
        )
      );

      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/${action}`, {
          method: 'POST',
        });

        if (!res.ok) throw new Error(`Failed to ${action} session`);
        toast.success(`Session ${action === 'pin' ? 'pinned' : 'unpinned'} successfully`);
        // Revalidate to ensure consistency
        await revalidate();
      } catch (e) {
        if (isQueuedForBackgroundSync(e)) {
          // Request was queued by the service worker for replay once back
          // online; keep the optimistic update instead of rolling it back.
          toast.info(`You are offline — ${action} will complete when you reconnect`);
          return;
        }
        // Rollback on failure: restore previous cache state
        rollback();
        toast.error(`Failed to ${action} session: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        // Remove pending state
        setPendingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sessionName);
          return next;
        });
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in togglePin:', error);
      toast.error('An unexpected error occurred while toggling pin status. Please refresh the page.');
    }
  };

  // Close context menu when clicking outside
  useEffect(() => {
    const handleClick = () => setContextMenu(null);
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setContextMenu(null);
    };

    if (contextMenu) {
      document.addEventListener('click', handleClick);
      document.addEventListener('keydown', handleEscape);
      return () => {
        document.removeEventListener('click', handleClick);
        document.removeEventListener('keydown', handleEscape);
      };
    }
  }, [contextMenu]);

  // Close settings menu when clicking outside
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (settingsMenuRef.current && !settingsMenuRef.current.contains(e.target as Node)) {
        setShowSettingsMenu(false);
        setShowAdminSubmenu(false);
      }
    };
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowSettingsMenu(false);
        setShowAdminSubmenu(false);
      }
    };

    if (showSettingsMenu) {
      document.addEventListener('mousedown', handleClick);
      document.addEventListener('keydown', handleEscape);
      return () => {
        document.removeEventListener('mousedown', handleClick);
        document.removeEventListener('keydown', handleEscape);
      };
    }
  }, [showSettingsMenu]);

  // Helper function to announce actions to screen readers
  const announce = useCallback((message: string) => {
    setSrAnnouncement(message);
    setShortcutFeedback(message);

    // Clear existing timeouts first
    if (feedbackTimeoutRef.current) {
      clearTimeout(feedbackTimeoutRef.current);
      feedbackTimeoutRef.current = null;
    }
    if (srTimeoutRef.current) {
      clearTimeout(srTimeoutRef.current);
      srTimeoutRef.current = null;
    }

    // Then set new timeouts and null refs when they fire
    feedbackTimeoutRef.current = setTimeout(() => {
      setShortcutFeedback('');
      feedbackTimeoutRef.current = null;
    }, 2000);

    srTimeoutRef.current = setTimeout(() => {
      setSrAnnouncement('');
      srTimeoutRef.current = null;
    }, 1000);
  }, []);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (feedbackTimeoutRef.current) clearTimeout(feedbackTimeoutRef.current);
      if (srTimeoutRef.current) clearTimeout(srTimeoutRef.current);
    };
  }, []);

  // Handle search result selection
  const handleSearchResultClick = useCallback((sessionName: string, messageIndex: number) => {
    // Switch to the selected session
    setSelectedSession(sessionName);

    // Set the message index to scroll to
    setScrollToMessageIndex(messageIndex);

    // Clear the scroll target after a short delay to allow for future scrolls
    setTimeout(() => {
      setScrollToMessageIndex(null);
    }, 1000);

    announce(`Navigating to message ${messageIndex + 1} in session ${sessionName}`);
  }, [announce]);

  // Global keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Check for Cmd (Mac) or Ctrl (Windows/Linux)
      const isMod = e.metaKey || e.ctrlKey;

      // Cmd/Ctrl + K: New chat
      if (isMod && e.key === 'k') {
        e.preventDefault();
        announce('Creating new chat');
        void createNewChat();
        return;
      }

      // Cmd/Ctrl + F: Focus search
      if (isMod && e.key === 'f') {
        e.preventDefault();
        searchRef.current?.focus();
        announce('Search focused');
        return;
      }

      // Cmd/Ctrl + /: Toggle sidebar
      if (isMod && e.key === '/') {
        e.preventDefault();
        setSidebarVisible((prev) => {
          const newState = !prev;
          announce(newState ? 'Sidebar shown' : 'Sidebar hidden');
          return newState;
        });
        return;
      }

      // / key: Focus search (when not typing in input)
      if (e.key === '/' && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) {
        e.preventDefault();
        searchRef.current?.focus();
        announce('Search focused');
        return;
      }

      // Esc: Close context menu (already handled above, but adding for completeness)
      if (e.key === 'Escape' && contextMenu) {
        e.preventDefault();
        setContextMenu(null);
        announce('Menu closed');
        return;
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [contextMenu, createNewChat, announce, setSidebarVisible]);

  // Highlight search matches in text
  const highlightText = useCallback((text: string, search: string) => {
    if (!search) return text;
    const parts = text.split(new RegExp(`(${search})`, 'gi'));
    return parts.map((part, i) =>
      part.toLowerCase() === search.toLowerCase() ? (
        <mark key={i} style={{ background: 'var(--highlight)', padding: '0 2px' }}>
          {part}
        </mark>
      ) : (
        part
      )
    );
  }, []);

  // Handle context menu for virtualized list
  const handleContextMenu = useCallback((e: React.MouseEvent, sessionName: string) => {
    e.preventDefault();
    setContextMenu({
      x: e.clientX,
      y: e.clientY,
      sessionName,
    });
  }, []);

  // Selecting a session on mobile should dismiss the sidebar overlay so the
  // chat is immediately visible, matching typical mobile chat app behavior.
  const selectSession = useCallback((sessionName: string) => {
    setSelectedSession(sessionName);
    if (isMobile) setSidebarVisible(false);
  }, [isMobile, setSidebarVisible]);

  return (
    <main
      style={{
        display: 'flex',
        height: '100vh',
        fontFamily: 'system-ui, sans-serif',
        position: 'relative',
      }}
    >
      {/* Screen reader announcements */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        style={{
          position: 'absolute',
          left: '-10000px',
          width: '1px',
          height: '1px',
          overflow: 'hidden',
        }}
      >
        {srAnnouncement}
      </div>

      {/* Visual keyboard shortcut feedback */}
      {shortcutFeedback && (
        <div
          role="alert"
          style={{
            position: 'fixed',
            bottom: 20,
            right: 20,
            background: 'var(--feedback-bg)',
            color: 'var(--feedback-text)',
            padding: '8px 16px',
            borderRadius: 6,
            fontSize: 14,
            fontWeight: 500,
            zIndex: 1000,
            boxShadow: '0 2px 8px rgba(0,0,0,0.2)',
            opacity: 1,
            transform: 'translateY(0)',
            transition: 'opacity 0.2s ease-in, transform 0.2s ease-in',
          }}
        >
          {shortcutFeedback}
        </div>
      )}

      {/* Backdrop: dismisses the sidebar overlay on mobile when tapped */}
      {isMobile && sidebarVisible && (
        <div
          onClick={() => setSidebarVisible(false)}
          aria-hidden="true"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.5)',
            zIndex: 150,
          }}
        />
      )}

      <SessionListErrorBoundary>
      {sidebarVisible && (
        <aside
          style={{
            width: isMobile ? '85vw' : 320,
            maxWidth: isMobile ? 320 : undefined,
            height: '100vh',
            borderRight: '1px solid var(--border)',
            padding: '1rem',
            background: 'var(--sidebar-bg)',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            ...(isMobile
              ? { position: 'fixed' as const, top: 0, left: 0, zIndex: 200, boxShadow: '2px 0 16px rgba(0,0,0,0.25)' }
              : {}),
          }}
        >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Image
            src="/nyxGPT-logo.png"
            alt="nyxGPT logo"
            width={200}
            height={100}
            priority
            style={{ objectFit: 'contain' }}
          />
          <div style={{ display: 'flex', gap: 8 }}>
            {/* Toggle Sidebar button */}
            <button
              onClick={() => setSidebarVisible((prev) => !prev)}
              aria-label={`Toggle sidebar (${modKey}+/)`}
              title={`Toggle sidebar (${modKey}+/)`}
              style={{
                padding: isMobile ? 12 : 8,
                background: 'transparent',
                border: '1px solid var(--border)',
                borderRadius: 6,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                outline: '2px solid transparent',
                outlineOffset: 2,
                transition: 'outline 0.2s ease, background 0.2s ease',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--button-hover)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
              }}
              onFocus={(e) => {
                e.currentTarget.style.outline = '2px solid var(--foreground)';
              }}
              onBlur={(e) => {
                e.currentTarget.style.outline = '2px solid transparent';
              }}
            >
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{ color: 'var(--foreground)' }}
              >
                {/* Outer rectangle */}
                <rect x="3" y="3" width="18" height="18" rx="2" />
                {/* Vertical divider */}
                <line x1="9" y1="3" x2="9" y2="21" />
                {/* Horizontal lines in left panel */}
                <line x1="5" y1="8" x2="7" y2="8" />
                <line x1="5" y1="12" x2="7" y2="12" />
              </svg>
            </button>
            {/* New Chat icon button */}
            <button
              onClick={() => void createNewChat()}
              aria-label={`Create new chat (${modKey}+K)`}
              title={`Create new chat (${modKey}+K)`}
              style={{
                padding: isMobile ? 12 : 8,
                background: 'transparent',
                border: '1px solid var(--border)',
                borderRadius: 6,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                outline: '2px solid transparent',
                outlineOffset: 2,
                transition: 'outline 0.2s ease, background 0.2s ease',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--button-hover)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
              }}
              onFocus={(e) => {
                e.currentTarget.style.outline = '2px solid var(--foreground)';
              }}
              onBlur={(e) => {
                e.currentTarget.style.outline = '2px solid transparent';
              }}
            >
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{ color: 'var(--foreground)' }}
              >
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
              </svg>
            </button>
          </div>
        </div>

        {/* Unified Search */}
        <UnifiedSearch
          ref={searchRef}
          sessions={sessions}
          onSelectSession={selectSession}
          onMessageClick={handleSearchResultClick}
          modKey={modKey}
        />

        {/* Filter controls */}
        <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <select
            value={filterModel}
            onChange={(e) => setFilterModel(e.target.value)}
            style={{
              padding: '6px 8px',
              border: '1px solid var(--border)',
              borderRadius: 4,
              fontSize: 12,
              background: 'var(--input-bg)',
              color: 'var(--foreground)',
            }}
          >
            <option value="">All models</option>
            {uniqueModels.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>

          <select
            value={filterPinned}
            onChange={(e) => setFilterPinned(e.target.value)}
            style={{
              padding: '6px 8px',
              border: '1px solid var(--border)',
              borderRadius: 4,
              fontSize: 12,
              background: 'var(--input-bg)',
              color: 'var(--foreground)',
            }}
          >
            <option value="all">All sessions</option>
            <option value="pinned">Pinned only</option>
            <option value="unpinned">Unpinned only</option>
          </select>

          <input
            type="text"
            placeholder="Filter by tag..."
            value={filterTags}
            onChange={(e) => setFilterTags(e.target.value)}
            style={{
              padding: '6px 8px',
              border: '1px solid var(--border)',
              borderRadius: 4,
              fontSize: 12,
              background: 'var(--input-bg)',
              color: 'var(--foreground)',
            }}
          />

          {(filterModel || filterPinned !== 'all' || filterTags) && (
            <button
              onClick={clearFilters}
              style={{
                padding: '6px 8px',
                border: '1px solid var(--border)',
                borderRadius: 4,
                fontSize: 12,
                background: 'var(--button-bg)',
                color: 'var(--foreground)',
                cursor: 'pointer',
              }}
            >
              Clear filters
            </button>
          )}
        </div>

        {/* Session list container - uses flex to fill remaining space */}
        <div style={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden'
        }}>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 10, flexShrink: 0 }}>
            Selected: <strong>{selectedSession}</strong>
            {filteredSessions.length !== sessions.length && (
              <span style={{ marginLeft: 8 }}>
                ({filteredSessions.length} of {sessions.length})
              </span>
            )}
          </div>

          {sessionsError && (
            <div style={{ marginBottom: 10, flexShrink: 0 }}>
              <ErrorMessage
                title="Failed to load sessions"
                message={sessionsError}
                onRetry={() => void invalidateSessions()}
                retrying={retryingSessions}
              />
            </div>
          )}

          {loadingSessions && !sessionsError && (
            <SessionListSkeleton />
          )}

          {!loadingSessions && !sessionsError && (
            <>
              {filteredSessions.length === 0 && sessions.length > 0 && (
                <div style={{ fontSize: 12, opacity: 0.75 }}>
                  No sessions match your filters.
                </div>
              )}
              {filteredSessions.length > 0 && (
                <VirtualizedSessionList
                  sessions={filteredSessions}
                  selectedSession={selectedSession}
                  onSelectSession={selectSession}
                  onContextMenu={handleContextMenu}
                  pendingSessions={pendingSessions}
                  highlightText={highlightText}
                />
              )}
              {sessions.length === 0 && !sessionsError && !loadingSessions && (
                <div style={{ fontSize: 12, opacity: 0.75 }}>
                  No sessions found yet.
                </div>
              )}
            </>
          )}
        </div>

        {/* Context Menu */}
        {contextMenu && (
          <div
            style={{
              position: 'fixed',
              top: contextMenu.y,
              left: contextMenu.x,
              background: 'var(--sidebar-bg)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
              zIndex: 1000,
              minWidth: 180,
              padding: '6px 0',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Rename */}
            <button
              onClick={() => {
                void renameSession(contextMenu.sessionName);
                setContextMenu(null);
              }}
              style={{
                width: '100%',
                padding: '8px 16px',
                border: 'none',
                background: 'transparent',
                textAlign: 'left',
                cursor: 'pointer',
                fontSize: 14,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                color: 'var(--foreground)',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <span>✏️</span> Rename
            </button>

            {/* Export submenu */}
            <div style={{ position: 'relative' }}>
              <div
                style={{
                  padding: '8px 16px',
                  fontSize: 14,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 8,
                  cursor: 'pointer',
                  color: 'var(--foreground)',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = 'var(--button-hover)';
                  const submenu = e.currentTarget.nextElementSibling as HTMLElement;
                  if (submenu) submenu.style.display = 'block';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = 'transparent';
                  const submenu = e.currentTarget.nextElementSibling as HTMLElement;
                  if (submenu) {
                    setTimeout(() => {
                      if (!submenu.matches(':hover')) submenu.style.display = 'none';
                    }, 100);
                  }
                }}
              >
                <div>
                  <span>📥</span> Export
                </div>
                <span style={{ fontSize: 10 }}>▶</span>
              </div>
              <div
                style={{
                  position: 'absolute',
                  left: '100%',
                  top: 0,
                  background: 'var(--sidebar-bg)',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  minWidth: 140,
                  padding: '6px 0',
                  display: 'none',
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.display = 'none';
                }}
              >
                {(['markdown', 'json', 'html'] as const).map((format) => (
                  <button
                    key={format}
                    onClick={() => {
                      void exportSession(contextMenu.sessionName, format);
                      setContextMenu(null);
                    }}
                    disabled={exportingSession === contextMenu.sessionName}
                    style={{
                      width: '100%',
                      padding: '8px 16px',
                      border: 'none',
                      background: 'transparent',
                      textAlign: 'left',
                      cursor: exportingSession === contextMenu.sessionName ? 'wait' : 'pointer',
                      fontSize: 14,
                      color: 'var(--foreground)',
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                  >
                    {format.charAt(0).toUpperCase() + format.slice(1)}
                  </button>
                ))}
              </div>
            </div>

            {/* Divider */}
            <div style={{ height: 1, background: 'var(--border-light)', margin: '6px 0' }} />

            {/* Pin/Unpin */}
            <button
              onClick={() => {
                void togglePin(contextMenu.sessionName);
                setContextMenu(null);
              }}
              style={{
                width: '100%',
                padding: '8px 16px',
                border: 'none',
                background: 'transparent',
                textAlign: 'left',
                cursor: 'pointer',
                fontSize: 14,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                color: 'var(--foreground)',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <span>📌</span>{' '}
              {sessions.find((s) => s.name === contextMenu.sessionName)?.pinned ? 'Unpin' : 'Pin'}
            </button>

            {/* Delete */}
            <button
              onClick={() => {
                void deleteSession(contextMenu.sessionName);
                setContextMenu(null);
              }}
              disabled={deletingSession === contextMenu.sessionName}
              style={{
                width: '100%',
                padding: '8px 16px',
                border: 'none',
                background: 'transparent',
                textAlign: 'left',
                cursor: deletingSession === contextMenu.sessionName ? 'wait' : 'pointer',
                fontSize: 14,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                color: '#d32f2f',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = '#ffebee')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <span>🗑️</span> Delete
            </button>
          </div>
        )}

        {/* Settings menu */}
        <div ref={settingsMenuRef} style={{ position: 'relative', marginTop: 16 }}>
          <button
            onClick={() => {
              setShowSettingsMenu(!showSettingsMenu);
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '8px 12px',
              background: 'transparent',
              border: 'none',
              color: 'var(--foreground)',
              fontSize: 13,
              fontWeight: 500,
              borderRadius: 4,
              cursor: 'pointer',
              width: '100%',
              transition: 'background 0.2s ease',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = 'var(--button-hover)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = 'transparent';
            }}
          >
            <span>⚙️</span>
            <span>Settings</span>
          </button>

          {showSettingsMenu && (
            <div
              style={{
                position: 'absolute',
                bottom: '100%',
                left: 0,
                marginBottom: 4,
                background: 'var(--sidebar-bg)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                minWidth: 180,
                padding: '6px 0',
                zIndex: 1000,
              }}
            >
              {/* Admin (collapsible group) */}
              <button
                onClick={() => {
                  setShowAdminSubmenu((prev) => !prev);
                }}
                aria-expanded={showAdminSubmenu}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 8,
                  padding: '8px 16px',
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--foreground)',
                  fontSize: 14,
                  cursor: 'pointer',
                  width: '100%',
                  textAlign: 'left',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span>🛠️</span>
                  <span>Admin</span>
                </span>
                <span
                  style={{
                    fontSize: 10,
                    transform: showAdminSubmenu ? 'rotate(90deg)' : 'none',
                    transition: 'transform 0.15s ease',
                  }}
                >
                  ▶
                </span>
              </button>

              {showAdminSubmenu && (
                <div role="group" aria-label="Admin">
                  {/* Dashboard */}
                  <a
                    href="/admin/dashboard"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>📊</span>
                    <span>Dashboard</span>
                  </a>

                  {/* Configuration Wizard */}
                  <a
                    href="/admin"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>🧙</span>
                    <span>Configuration Wizard</span>
                  </a>

                  {/* Resource Usage */}
                  <a
                    href="/settings"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>📊</span>
                    <span>Resource Usage</span>
                  </a>

                  {/* View Logs */}
                  <a
                    href="/admin/logs"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>📋</span>
                    <span>View Logs</span>
                  </a>

                  {/* Usage Analytics */}
                  <a
                    href="/admin/analytics"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>📈</span>
                    <span>Usage Analytics</span>
                  </a>

                  {/* Observability */}
                  <a
                    href="/admin/observability"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>👁️</span>
                    <span>Observability</span>
                  </a>

                  {/* Manage Models */}
                  <a
                    href="/models"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>🤖</span>
                    <span>Manage Models</span>
                  </a>

                  {/* RAG Collections */}
                  <a
                    href="/admin/collections"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>📚</span>
                    <span>RAG Collections</span>
                  </a>

                  {/* RAG Playground */}
                  <a
                    href="/admin/playground"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>🧪</span>
                    <span>RAG Playground</span>
                  </a>

                  {/* Blue/Green Deployment */}
                  <a
                    href="/admin/deploy"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>🚀</span>
                    <span>Deployment</span>
                  </a>

                  {/* Canary Deployment */}
                  <a
                    href="/admin/canary"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px 8px 32px',
                      textDecoration: 'none',
                      color: 'var(--foreground)',
                      fontSize: 14,
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                    onClick={() => setShowSettingsMenu(false)}
                  >
                    <span>🐤</span>
                    <span>Canary Rollout</span>
                  </a>
                </div>
              )}

              {/* Divider */}
              <div style={{ height: 1, background: 'var(--border-light)', margin: '6px 0' }} />

              {/* Theme submenu */}
              <div style={{ padding: '4px 16px', fontSize: 12, opacity: 0.7, fontWeight: 600 }}>
                Theme
              </div>
              <button
                onClick={() => {
                  setTheme('light');
                  setShowSettingsMenu(false);
                }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '8px 16px',
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--foreground)',
                  fontSize: 14,
                  cursor: 'pointer',
                  width: '100%',
                  textAlign: 'left',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <span>☀️</span>
                <span>Light</span>
                {theme === 'light' && <span style={{ marginLeft: 'auto' }}>✓</span>}
              </button>
              <button
                onClick={() => {
                  setTheme('dark');
                  setShowSettingsMenu(false);
                }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '8px 16px',
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--foreground)',
                  fontSize: 14,
                  cursor: 'pointer',
                  width: '100%',
                  textAlign: 'left',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <span>🌙</span>
                <span>Dark</span>
                {theme === 'dark' && <span style={{ marginLeft: 'auto' }}>✓</span>}
              </button>
            </div>
          )}
        </div>

        {/* Keyboard shortcuts help */}
        <div
          style={{
            marginTop: 8,
            paddingTop: 12,
            borderTop: '1px solid var(--border-light)',
            fontSize: 11,
            opacity: 0.7,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Keyboard Shortcuts</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3, minWidth: 48, display: 'inline-block' }}>{modKey}+K</kbd> New
              chat
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3, minWidth: 48, display: 'inline-block' }}>{modKey}+/</kbd> Toggle
              sidebar
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3, minWidth: 48, display: 'inline-block' }}>{modKey}+F</kbd> Search
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3, minWidth: 48, display: 'inline-block' }}>Esc</kbd> Close
              menus
            </div>
          </div>
        </div>
      </aside>
      )}
      </SessionListErrorBoundary>

      <section style={{ flex: 1, minWidth: 0, padding: isMobile ? '1rem' : '1.5rem', overflowY: 'auto', background: 'var(--background)', color: 'var(--foreground)' }}>
        {!sidebarVisible && (
          <button
            onClick={() => setSidebarVisible(true)}
            aria-label={`Show sidebar (${modKey}+/)`}
            title={`Show sidebar (${modKey}+/)`}
            style={{
              position: 'fixed',
              top: 16,
              left: 16,
              padding: '8px 12px',
              background: '#E45801',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              fontSize: 14,
              fontWeight: 600,
              cursor: 'pointer',
              zIndex: 100,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              outline: '2px solid transparent',
              outlineOffset: 2,
              transition: 'outline 0.2s ease',
            }}
            onFocus={(e) => {
              e.currentTarget.style.outline = '2px solid #fff';
            }}
            onBlur={(e) => {
              e.currentTarget.style.outline = '2px solid transparent';
            }}
          >
            ☰ Menu
          </button>
        )}

        <div style={{ height: 'calc(100vh - 80px)' }}>
          <ChatPane
            sessionName={selectedSession}
            onSessionUpdated={refreshSessions}
            scrollToMessageIndex={scrollToMessageIndex}
            releaseVersion={info?.release_version}
          />
        </div>
      </section>

    </main>
  );
}

// Wrap with error boundary to handle cache initialization errors
export default function WrappedHome() {
  return (
    <SessionCacheErrorBoundary>
      <Home />
    </SessionCacheErrorBoundary>
  );
}

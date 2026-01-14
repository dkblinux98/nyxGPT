'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import ChatPane from './components/ChatPane';
import ThemeToggle from '../components/ThemeToggle';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import { SessionListSkeleton } from '../components/SkeletonLoader';
import { SessionListErrorBoundary } from '../components/SessionListErrorBoundary';
import { SearchModal } from '../components/SearchModal';
import { VirtualizedSessionList } from '../components/VirtualizedSessionList';
import { useToast } from '../contexts/ToastContext';

type Info = {
  ollama_base_url: string;
  default_model: string;
  sessions_dir: string;
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

export default function Home() {
  const toast = useToast();
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingInfo, setLoadingInfo] = useState<boolean>(true);
  const [retryingInfo, setRetryingInfo] = useState<boolean>(false);

  const [sessions, setSessions] = useState<SessionsResponse['sessions']>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState<boolean>(true);
  const [retryingSessions, setRetryingSessions] = useState<boolean>(false);
  const [selectedSession, setSelectedSession] = useState<string>('default');
  const [sidebarVisible, setSidebarVisible] = useState<boolean>(true);

  // Message search state
  const [showSearchModal, setShowSearchModal] = useState<boolean>(false);
  const [scrollToMessageIndex, setScrollToMessageIndex] = useState<number | null>(null);

  // Search and filter state
  const [searchText, setSearchText] = useState<string>('');
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
  const searchInputRef = useRef<HTMLInputElement>(null);
  const feedbackTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const srTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Accessibility: Screen reader announcements
  const [srAnnouncement, setSrAnnouncement] = useState<string>('');

  // Accessibility: Visual feedback for shortcuts
  const [shortcutFeedback, setShortcutFeedback] = useState<string>('');

  // Platform detection for keyboard shortcuts display
  const isMac = typeof navigator !== 'undefined' && navigator.platform.toUpperCase().indexOf('MAC') >= 0;
  const modKey = isMac ? '⌘' : 'Ctrl';

  // Fetch API info with retry support
  const fetchInfo = useCallback(async (isRetry = false) => {
    if (isRetry) {
      setRetryingInfo(true);
    } else {
      setLoadingInfo(true);
    }
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
      setRetryingInfo(false);
    }
  }, []);

  // Fetch sessions with retry support
  const fetchSessions = useCallback(async (isRetry = false) => {
    if (isRetry) {
      setRetryingSessions(true);
    } else {
      setLoadingSessions(true);
    }
    setSessionsError(null);

    try {
      const res = await fetch('/api/sessions');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: SessionsResponse = await res.json();
      setSessions(data.sessions || []);
      // Keep selection stable; if current selection disappears, fall back.
      const names = new Set((data.sessions || []).map((s) => s.name));
      if (!names.has(selectedSession)) {
        setSelectedSession(names.has('default') ? 'default' : (data.sessions?.[0]?.name ?? 'default'));
      }
      setSessionsError(null);
    } catch (e) {
      setSessionsError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingSessions(false);
      setRetryingSessions(false);
    }
  }, [selectedSession]);

  useEffect(() => {
    void fetchInfo();
  }, [fetchInfo]);

  useEffect(() => {
    void fetchSessions();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Filter and search sessions
  const filteredSessions = sessions.filter((session) => {
    // Text search across name, title, and summary
    const searchLower = searchText.toLowerCase();
    const matchesSearch =
      !searchText ||
      session.name.toLowerCase().includes(searchLower) ||
      session.title?.toLowerCase().includes(searchLower) ||
      session.summary?.toLowerCase().includes(searchLower);

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

    return matchesSearch && matchesModel && matchesPinned && matchesTags;
  });

  // Get unique models for filter dropdown
  const uniqueModels = Array.from(
    new Set(sessions.map((s) => s.model).filter((m): m is string => !!m))
  ).sort();

  // Clear all filters
  const clearFilters = () => {
    setSearchText('');
    setFilterModel('');
    setFilterPinned('all');
    setFilterTags('');
  };

  // Refresh session list
  const refreshSessions = useCallback(async () => {
    try {
      // Add timestamp to prevent browser caching
      const res = await fetch(`/api/sessions?t=${Date.now()}`, {
        cache: 'no-store',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: SessionsResponse = await res.json();
      setSessions(data.sessions || []);
    } catch (e) {
      setSessionsError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Create new chat with optimistic update
  const createNewChat = useCallback(async () => {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').substring(0, 19);
    const defaultName = `session-${timestamp}`;
    const sessionName = prompt('Enter session name:', defaultName);

    if (!sessionName || !sessionName.trim()) return;

    const trimmedName = sessionName.trim();

    // Optimistic update: add new session to local state immediately
    const previousSessions = [...sessions];
    const previousSelection = selectedSession;
    const newSession = {
      name: trimmedName,
      messages: 0,
      pinned: false,
      tags: [],
      title: trimmedName,
      modified: new Date().toISOString(),
    };

    setSessions([newSession, ...sessions]);
    setSelectedSession(trimmedName);

    try {
      // Create the session on the backend
      const res = await fetch('/api/sessions/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: trimmedName,
          system: 'You are a helpful assistant.',
        }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(errorData.detail || `HTTP ${res.status}`);
      }

      // Refresh the sessions list to get actual server state
      await refreshSessions();
      toast.success('New chat created successfully');
    } catch (e) {
      // Rollback on failure: restore previous state
      setSessions(previousSessions);
      setSelectedSession(previousSelection);
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to create new chat: ${msg}`);
      console.error('Failed to create new chat:', e);
    }
  }, [refreshSessions, setSelectedSession, sessions, selectedSession]);

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
      announce(`Deleting session ${sessionName}`);

      // Capture previous state for rollback
      let previousSessions: typeof sessions = [];
      let previousSelection = '';

      // Optimistic update: remove session immediately from local state using functional update
      setSessions(prevSessions => {
        previousSessions = [...prevSessions];
        const remainingSessions = prevSessions.filter((s) => s.name !== sessionName);
        return remainingSessions;
      });

      // If deleting the selected session, switch to another immediately using functional update
      setSelectedSession(prevSelected => {
        previousSelection = prevSelected;
        if (sessionName === prevSelected) {
          // Access the updated sessions from the closure
          const remainingSessions = previousSessions.filter((s) => s.name !== sessionName);
          return remainingSessions[0]?.name || 'default';
        }
        return prevSelected;
      });

      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/delete`, {
          method: 'POST',
        });

        if (!res.ok) {
          const error = await res.text();
          throw new Error(error || 'Delete failed');
        }
        announce(`Session ${sessionName} deleted successfully`);
        toast.success(`Session deleted successfully`);
      } catch (e) {
        // Rollback on failure: restore previous state
        setSessions(previousSessions);
        setSelectedSession(previousSelection);
        const errorMsg = `Failed to delete session: ${e instanceof Error ? e.message : String(e)}`;
        toast.error(errorMsg);
        announce(errorMsg);
      } finally {
        setDeletingSession(null);
        // Always refresh to ensure consistency regardless of success/failure
        await refreshSessions();
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in deleteSession:', error);
      const unexpectedErrorMsg = 'An unexpected error occurred while deleting the session. Please refresh the page.';
      toast.error(unexpectedErrorMsg);
      announce(unexpectedErrorMsg);
      // Attempt to refresh sessions to restore consistent state
      try {
        await refreshSessions();
      } catch (refreshError) {
        console.error('Failed to refresh after error:', refreshError);
      }
    }
  };

  // Rename session with optimistic update
  const renameSession = async (sessionName: string) => {
    try {
      // Use functional update to get current session
      let currentTitle = '';
      setSessions(prevSessions => {
        const session = prevSessions.find((s) => s.name === sessionName);
        currentTitle = session?.title || sessionName;
        return prevSessions;
      });

      const newName = prompt('Enter new session name or title:', currentTitle);

      if (!newName || newName.trim() === '' || newName === currentTitle) return;

      // Check if operation is already in progress
      if (pendingSessions.has(sessionName)) {
        console.warn('Operation already in progress for session:', sessionName);
        return;
      }

      // Mark as pending for visual feedback
      setPendingSessions((prev) => {
        const next = new Set(prev).add(sessionName);
        announce(`Renaming session ${sessionName} to ${newName.trim()}`);
        return next;
      });

      // Capture previous state for rollback
      let previousSessions: typeof sessions = [];
      let previousSelection = '';

      // Optimistic update: update session title immediately using functional update
      setSessions(prevSessions => {
        previousSessions = [...prevSessions];
        const optimisticSessions = prevSessions.map((s) =>
          s.name === sessionName ? { ...s, title: newName.trim() } : s
        );
        return optimisticSessions;
      });

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

      // If filename changed, update selected session using functional update
      setSelectedSession(prevSelected => {
        previousSelection = prevSelected;
        if (data.new_name !== sessionName && sessionName === prevSelected) {
          return data.new_name;
        }
        return prevSelected;
      });
      announce(`Session renamed successfully to ${newName.trim()}`);
      toast.success('Session renamed successfully');
      } catch (e) {
        // Rollback on failure: restore previous state
        setSessions(previousSessions);
        setSelectedSession(previousSelection);
        const errorMsg = `Failed to rename session: ${e instanceof Error ? e.message : String(e)}`;
        toast.error(errorMsg);
        announce(errorMsg);
      } finally {
        // Remove pending state
        setPendingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sessionName);
          return next;
        });
        // Always refresh to ensure consistency regardless of success/failure
        await refreshSessions();
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in renameSession:', error);
      const unexpectedErrorMsg = 'An unexpected error occurred while renaming the session. Please refresh the page.';
      toast.error(unexpectedErrorMsg);
      announce(unexpectedErrorMsg);
      // Attempt to refresh sessions to restore consistent state
      try {
        await refreshSessions();
      } catch (refreshError) {
        console.error('Failed to refresh after error:', refreshError);
      }
    }
  };

  // Export session
  const exportSession = async (sessionName: string, format: 'markdown' | 'json' | 'html') => {
    setExportingSession(sessionName);
    try {
      const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';
      const url = `${apiBaseUrl}/api/v1/sessions/${encodeURIComponent(sessionName)}/export?format=${format}`;

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

      // Use functional update to get current session state
      let isPinned = false;
      let action = '';
      setSessions(prevSessions => {
        const session = prevSessions.find((s) => s.name === sessionName);
        isPinned = session?.pinned || false;
        action = isPinned ? 'unpin' : 'pin';
        return prevSessions;
      });

      // Mark as pending for visual feedback
      setPendingSessions((prev) => {
        const next = new Set(prev).add(sessionName);
        announce(`${action === 'pin' ? 'Pinning' : 'Unpinning'} session ${sessionName}`);
        return next;
      });

      // Capture previous state for rollback
      let previousSessions: typeof sessions = [];

      // Optimistic update: toggle pin status immediately in local state using functional update
      setSessions(prevSessions => {
        previousSessions = [...prevSessions];
        const optimisticSessions = prevSessions.map((s) =>
          s.name === sessionName ? { ...s, pinned: !isPinned } : s
        );
        return optimisticSessions;
      });

      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/${action}`, {
          method: 'POST',
        });

        if (!res.ok) throw new Error(`Failed to ${action} session`);
        announce(`Session ${sessionName} ${action === 'pin' ? 'pinned' : 'unpinned'} successfully`);
        toast.success(`Session ${action === 'pin' ? 'pinned' : 'unpinned'} successfully`);
      } catch (e) {
        // Rollback on failure: restore previous state
        setSessions(previousSessions);
        const errorMsg = `Failed to ${action} session: ${e instanceof Error ? e.message : String(e)}`;
        toast.error(errorMsg);
        announce(errorMsg);
      } finally {
        // Remove pending state
        setPendingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sessionName);
          return next;
        });
        // Always refresh to ensure consistency regardless of success/failure
        await refreshSessions();
      }
    } catch (error) {
      // Catch any unexpected errors in state updates or operations
      console.error('Unexpected error in togglePin:', error);
      const unexpectedErrorMsg = 'An unexpected error occurred while toggling pin status. Please refresh the page.';
      toast.error(unexpectedErrorMsg);
      announce(unexpectedErrorMsg);
      // Attempt to refresh sessions to restore consistent state
      try {
        await refreshSessions();
      } catch (refreshError) {
        console.error('Failed to refresh after error:', refreshError);
      }
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

      // Cmd/Ctrl + F: Open search modal
      if (isMod && e.key === 'f') {
        e.preventDefault();
        setShowSearchModal(true);
        announce('Search modal opened');
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
        searchInputRef.current?.focus();
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
  }, [contextMenu, createNewChat, announce]);

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

      <SessionListErrorBoundary>
      {sidebarVisible && (
        <aside
          style={{
            width: 320,
            borderRight: '1px solid var(--border)',
            padding: '1rem',
            overflowY: 'auto',
            background: 'var(--sidebar-bg)',
          }}
        >
        <h1 style={{ margin: 0 }}>myGPT</h1>
        <p style={{ marginTop: 6, marginBottom: 8, opacity: 0.8 }}>
          Local web UI (early)
        </p>

        {/* Theme Toggle */}
        <div style={{ marginBottom: 12 }}>
          <ThemeToggle />
        </div>

        {/* New Chat button */}
        <button
          onClick={() => void createNewChat()}
          aria-label={`Create new chat (${modKey}+K)`}
          title={`Create new chat (${modKey}+K)`}
          style={{
            width: '100%',
            padding: '10px 12px',
            marginBottom: 12,
            background: 'var(--success)',
            color: 'white',
            border: 'none',
            borderRadius: 6,
            fontSize: 14,
            fontWeight: 600,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 8,
            outline: '2px solid transparent',
            outlineOffset: 2,
            transition: 'outline 0.2s ease',
          }}
          onFocus={(e) => {
            e.currentTarget.style.outline = '2px solid var(--foreground)';
          }}
          onBlur={(e) => {
            e.currentTarget.style.outline = '2px solid transparent';
          }}
        >
          <span style={{ fontSize: 16 }}>+</span> New Chat
        </button>

        {/* Search Messages button */}
        <button
          onClick={() => setShowSearchModal(true)}
          aria-label={`Search messages (${modKey}+F)`}
          title={`Search messages (${modKey}+F)`}
          style={{
            width: '100%',
            padding: '10px 12px',
            marginBottom: 12,
            background: 'var(--background)',
            color: 'var(--foreground)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            fontSize: 14,
            fontWeight: 500,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 8,
            outline: '2px solid transparent',
            outlineOffset: 2,
            transition: 'outline 0.2s ease, background 0.2s ease',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'var(--hover-bg)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'var(--background)';
          }}
          onFocus={(e) => {
            e.currentTarget.style.outline = '2px solid var(--foreground)';
          }}
          onBlur={(e) => {
            e.currentTarget.style.outline = '2px solid transparent';
          }}
        >
          <span style={{ fontSize: 16 }}>🔍</span> Search Messages
          <span style={{ fontSize: 11, opacity: 0.6, marginLeft: 'auto' }}>
            {modKey}+F
          </span>
        </button>

        {/* Navigation Menu */}
        <nav style={{ marginBottom: 16 }} aria-label="Main navigation">
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              background: 'var(--input-bg)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: 8,
            }}
          >
            <a
              href="/admin"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '8px 12px',
                textDecoration: 'none',
                color: 'var(--foreground)',
                fontSize: 13,
                fontWeight: 500,
                borderRadius: 4,
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
            </a>
            <a
              href="/models"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '8px 12px',
                textDecoration: 'none',
                color: 'var(--foreground)',
                fontSize: 13,
                fontWeight: 500,
                borderRadius: 4,
                transition: 'background 0.2s ease',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--button-hover)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
              }}
            >
              <span>🤖</span>
              <span>Manage Models</span>
            </a>
          </div>
        </nav>

        {/* Search input */}
        <input
          ref={searchInputRef}
          type="text"
          placeholder="Search sessions..."
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          aria-label="Search sessions (press / to focus)"
          title="Search sessions (press / to focus)"
          style={{
            width: '100%',
            padding: '8px 10px',
            marginBottom: 8,
            border: '1px solid var(--border)',
            borderRadius: 6,
            fontSize: 14,
            boxSizing: 'border-box',
            outline: '2px solid transparent',
            outlineOffset: 2,
            transition: 'outline 0.2s ease',
            background: 'var(--input-bg)',
            color: 'var(--foreground)',
          }}
          onFocus={(e) => {
            e.currentTarget.style.outline = '2px solid var(--success)';
          }}
          onBlur={(e) => {
            e.currentTarget.style.outline = '2px solid transparent';
          }}
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

          {(searchText || filterModel || filterPinned !== 'all' || filterTags) && (
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

        <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 10 }}>
          Selected: <strong>{selectedSession}</strong>
          {filteredSessions.length !== sessions.length && (
            <span style={{ marginLeft: 8 }}>
              ({filteredSessions.length} of {sessions.length})
            </span>
          )}
        </div>

        {sessionsError && (
          <div style={{ marginBottom: 10 }}>
            <ErrorMessage
              title="Failed to load sessions"
              message={sessionsError}
              onRetry={() => void fetchSessions(true)}
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
                onSelectSession={setSelectedSession}
                onContextMenu={handleContextMenu}
                searchText={searchText}
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

        {/* Keyboard shortcuts help */}
        <div
          style={{
            marginTop: 16,
            paddingTop: 12,
            borderTop: '1px solid var(--border-light)',
            fontSize: 11,
            opacity: 0.7,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Keyboard Shortcuts</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3 }}>{modKey}+K</kbd> New
              chat
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3 }}>{modKey}+/</kbd> Toggle
              sidebar
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3 }}>{modKey}+F</kbd> Search
              messages
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3 }}>/</kbd> Filter
              sessions
            </div>
            <div>
              <kbd style={{ background: 'var(--button-hover)', padding: '2px 4px', borderRadius: 3 }}>Esc</kbd> Close
              menus
            </div>
          </div>
        </div>
      </aside>
      )}
      </SessionListErrorBoundary>

      <section style={{ flex: 1, padding: '1.5rem', overflowY: 'auto', background: 'var(--background)', color: 'var(--foreground)' }}>
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
              background: 'var(--success)',
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

        <h2 style={{ marginTop: 0 }}>Backend</h2>

        {error && (
          <ErrorMessage
            title="Failed to load backend info"
            message={error}
            onRetry={() => void fetchInfo(true)}
            retrying={retryingInfo}
          />
        )}

        {loadingInfo && !error && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '1rem 0' }}>
            <LoadingSpinner size="small" />
            <span style={{ fontSize: 14, opacity: 0.7 }}>Loading API info…</span>
          </div>
        )}

        {info && !loadingInfo && (
          <ul>
            <li>
              <strong>Ollama base URL:</strong> {info.ollama_base_url}
            </li>
            <li>
              <strong>Default model:</strong> {info.default_model}
            </li>
            <li>
              <strong>Sessions dir:</strong> {info.sessions_dir}
            </li>
          </ul>
        )}

        <hr style={{ margin: '1.5rem 0' }} />

        <h2>Chat</h2>
        <div style={{ height: 'calc(100vh - 220px)' }}>
          <ChatPane
            sessionName={selectedSession}
            onSessionUpdated={refreshSessions}
            scrollToMessageIndex={scrollToMessageIndex}
          />
        </div>
      </section>

      {/* Search Modal */}
      <SearchModal
        isOpen={showSearchModal}
        onClose={() => setShowSearchModal(false)}
        onResultClick={handleSearchResultClick}
      />
    </main>
  );
}

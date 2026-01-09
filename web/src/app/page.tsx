'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import ChatPane from './components/ChatPane';

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
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [sessions, setSessions] = useState<SessionsResponse['sessions']>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [selectedSession, setSelectedSession] = useState<string>('default');
  const [sidebarVisible, setSidebarVisible] = useState<boolean>(true);

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

  useEffect(() => {
    fetch('/api/info')
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(setInfo)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    fetch('/api/sessions')
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: SessionsResponse) => {
        setSessions(data.sessions || []);
        // Keep selection stable; if current selection disappears, fall back.
        const names = new Set((data.sessions || []).map((s) => s.name));
        if (!names.has(selectedSession)) {
          setSelectedSession(names.has('default') ? 'default' : (data.sessions?.[0]?.name ?? 'default'));
        }
      })
      .catch((e) => setSessionsError(e.message));
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

  // Create new chat
  const createNewChat = useCallback(async () => {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').substring(0, 19);
    const defaultName = `session-${timestamp}`;
    const sessionName = prompt('Enter session name:', defaultName);

    if (!sessionName || !sessionName.trim()) return;

    const trimmedName = sessionName.trim();

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

      // Select the new session
      setSelectedSession(trimmedName);

      // Refresh the sessions list to show it in the sidebar
      await refreshSessions();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Failed to create new chat: ${msg}`);
      console.error('Failed to create new chat:', e);
    }
  }, [refreshSessions, setSelectedSession]);

  // Delete session
  const deleteSession = async (sessionName: string) => {
    if (sessions.length <= 1) {
      alert('Cannot delete the last session');
      return;
    }

    if (!confirm(`Delete session "${sessionName}"? This cannot be undone.`)) {
      return;
    }

    setDeletingSession(sessionName);
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/delete`, {
        method: 'POST',
      });

      if (!res.ok) {
        const error = await res.text();
        throw new Error(error || 'Delete failed');
      }

      // If we deleted the selected session, switch to another
      if (sessionName === selectedSession) {
        const remaining = sessions.filter((s) => s.name !== sessionName);
        setSelectedSession(remaining[0]?.name || 'default');
      }

      // Refresh session list
      await refreshSessions();
    } catch (e) {
      alert(`Failed to delete session: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDeletingSession(null);
    }
  };

  // Rename session
  const renameSession = async (sessionName: string) => {
    const session = sessions.find((s) => s.name === sessionName);
    const currentTitle = session?.title || sessionName;
    const newName = prompt('Enter new session name or title:', currentTitle);

    if (!newName || newName.trim() === '' || newName === currentTitle) return;

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

      // Refresh session list
      await refreshSessions();
    } catch (e) {
      alert(`Failed to rename session: ${e instanceof Error ? e.message : String(e)}`);
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
    } catch (e) {
      alert(`Failed to export session: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setExportingSession(null);
    }
  };

  // Toggle pin status
  const togglePin = async (sessionName: string) => {
    const session = sessions.find((s) => s.name === sessionName);
    const isPinned = session?.pinned || false;
    const action = isPinned ? 'unpin' : 'pin';

    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/${action}`, {
        method: 'POST',
      });

      if (!res.ok) throw new Error(`Failed to ${action} session`);

      await refreshSessions();
    } catch (e) {
      alert(`Failed to ${action} session: ${e instanceof Error ? e.message : String(e)}`);
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

    // Clear any existing timeouts
    if (feedbackTimeoutRef.current) clearTimeout(feedbackTimeoutRef.current);
    if (srTimeoutRef.current) clearTimeout(srTimeoutRef.current);

    // Set new timeouts and store IDs
    feedbackTimeoutRef.current = setTimeout(() => setShortcutFeedback(''), 2000);
    srTimeoutRef.current = setTimeout(() => setSrAnnouncement(''), 1000);
  }, []);

  // Cleanup timeouts on unmount
  useEffect(() => {
    return () => {
      if (feedbackTimeoutRef.current) clearTimeout(feedbackTimeoutRef.current);
      if (srTimeoutRef.current) clearTimeout(srTimeoutRef.current);
    };
  }, []);

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
  const highlightText = (text: string, search: string) => {
    if (!search) return text;
    const parts = text.split(new RegExp(`(${search})`, 'gi'));
    return parts.map((part, i) =>
      part.toLowerCase() === search.toLowerCase() ? (
        <mark key={i} style={{ background: '#ffeb3b', padding: '0 2px' }}>
          {part}
        </mark>
      ) : (
        part
      )
    );
  };

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
            background: 'rgba(0, 0, 0, 0.8)',
            color: 'white',
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

      {sidebarVisible && (
        <aside
          style={{
            width: 320,
            borderRight: '1px solid #ddd',
            padding: '1rem',
            overflowY: 'auto',
          }}
        >
        <h1 style={{ margin: 0 }}>myGPT</h1>
        <p style={{ marginTop: 6, marginBottom: 8, opacity: 0.8 }}>
          Local web UI (early)
        </p>

        {/* New Chat button */}
        <button
          onClick={() => void createNewChat()}
          aria-label={`Create new chat (${modKey}+K)`}
          title={`Create new chat (${modKey}+K)`}
          style={{
            width: '100%',
            padding: '10px 12px',
            marginBottom: 12,
            background: '#4CAF50',
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
            e.currentTarget.style.outline = '2px solid #333';
          }}
          onBlur={(e) => {
            e.currentTarget.style.outline = '2px solid transparent';
          }}
        >
          <span style={{ fontSize: 16 }}>+</span> New Chat
        </button>

        <div style={{ marginBottom: 16 }}>
          <a
            href="/models"
            style={{
              display: 'inline-block',
              padding: '6px 12px',
              background: '#f4f4f4',
              border: '1px solid #ddd',
              borderRadius: 6,
              textDecoration: 'none',
              color: '#333',
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            Manage Models
          </a>
        </div>

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
            border: '1px solid #ddd',
            borderRadius: 6,
            fontSize: 14,
            boxSizing: 'border-box',
            outline: '2px solid transparent',
            outlineOffset: 2,
            transition: 'outline 0.2s ease',
          }}
          onFocus={(e) => {
            e.currentTarget.style.outline = '2px solid #4CAF50';
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
              border: '1px solid #ddd',
              borderRadius: 4,
              fontSize: 12,
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
              border: '1px solid #ddd',
              borderRadius: 4,
              fontSize: 12,
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
              border: '1px solid #ddd',
              borderRadius: 4,
              fontSize: 12,
            }}
          />

          {(searchText || filterModel || filterPinned !== 'all' || filterTags) && (
            <button
              onClick={clearFilters}
              style={{
                padding: '6px 8px',
                border: '1px solid #ddd',
                borderRadius: 4,
                fontSize: 12,
                background: '#f8f8f8',
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
          <div style={{ color: 'red', fontSize: 12, marginBottom: 10 }}>
            Error loading /api/sessions: {sessionsError}
          </div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {filteredSessions.map((s) => {
            const isActive = s.name === selectedSession;
            const displayText = s.title?.trim() ? s.title : s.name;
            return (
              <button
                key={s.name}
                onClick={() => setSelectedSession(s.name)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  setContextMenu({
                    x: e.clientX,
                    y: e.clientY,
                    sessionName: s.name,
                  });
                }}
                style={{
                  textAlign: 'left',
                  padding: '10px 10px',
                  borderRadius: 8,
                  border: '1px solid ' + (isActive ? '#999' : '#e5e5e5'),
                  background: isActive ? '#f4f4f4' : 'white',
                  cursor: 'pointer',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14 }}>
                  {s.pinned && <span>📌 </span>}
                  {searchText ? highlightText(displayText, searchText) : displayText}
                </div>
                <div style={{ fontSize: 12, opacity: 0.75, marginTop: 2 }}>
                  {s.messages ?? 0} msg · {s.model ?? ''}
                </div>
                {!!(s.tags && s.tags.length) && (
                  <div style={{ fontSize: 12, opacity: 0.7, marginTop: 4 }}>
                    {s.tags.slice(0, 5).join(', ')}
                  </div>
                )}
              </button>
            );
          })}

          {filteredSessions.length === 0 && sessions.length > 0 && (
            <div style={{ fontSize: 12, opacity: 0.75 }}>
              No sessions match your filters.
            </div>
          )}

          {sessions.length === 0 && !sessionsError && (
            <div style={{ fontSize: 12, opacity: 0.75 }}>
              No sessions found yet.
            </div>
          )}
        </div>

        {/* Context Menu */}
        {contextMenu && (
          <div
            style={{
              position: 'fixed',
              top: contextMenu.y,
              left: contextMenu.x,
              background: 'white',
              border: '1px solid #ddd',
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
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = '#f5f5f5')}
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
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = '#f5f5f5';
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
                  background: 'white',
                  border: '1px solid #ddd',
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
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = '#f5f5f5')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                  >
                    {format.charAt(0).toUpperCase() + format.slice(1)}
                  </button>
                ))}
              </div>
            </div>

            {/* Divider */}
            <div style={{ height: 1, background: '#e5e5e5', margin: '6px 0' }} />

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
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = '#f5f5f5')}
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
            borderTop: '1px solid #e5e5e5',
            fontSize: 11,
            opacity: 0.7,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Keyboard Shortcuts</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div>
              <kbd style={{ background: '#f5f5f5', padding: '2px 4px', borderRadius: 3 }}>{modKey}+K</kbd> New
              chat
            </div>
            <div>
              <kbd style={{ background: '#f5f5f5', padding: '2px 4px', borderRadius: 3 }}>{modKey}+/</kbd> Toggle
              sidebar
            </div>
            <div>
              <kbd style={{ background: '#f5f5f5', padding: '2px 4px', borderRadius: 3 }}>/</kbd> Search
            </div>
            <div>
              <kbd style={{ background: '#f5f5f5', padding: '2px 4px', borderRadius: 3 }}>Esc</kbd> Close
              menus
            </div>
          </div>
        </div>
      </aside>
      )}

      <section style={{ flex: 1, padding: '1.5rem', overflowY: 'auto' }}>
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
              background: '#4CAF50',
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
          <div style={{ color: 'red' }}>Error loading /api/info: {error}</div>
        )}

        {!info && !error && <div>Loading API info…</div>}

        {info && (
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
          <ChatPane sessionName={selectedSession} onSessionUpdated={refreshSessions} />
        </div>
      </section>
    </main>
  );
}

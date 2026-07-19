'use client';

import { useState, useEffect, useCallback, useRef, ReactNode, forwardRef, useImperativeHandle } from 'react';

export interface MessageSearchResult {
  session_name: string;
  session_title: string | null;
  message_index: number;
  role: string;
  content_preview: string;
  timestamp: string | null;
  matches: number;
}

export interface Session {
  name: string;
  title?: string;
  summary?: string;
  modified?: string;
  messages?: number;
  pinned?: boolean;
  tags?: string[];
  model?: string;
}

export interface UnifiedSearchProps {
  sessions: Session[];
  onSelectSession: (sessionName: string) => void;
  onMessageClick: (sessionName: string, messageIndex: number) => void;
  modKey: string;
}

export interface UnifiedSearchRef {
  focus: () => void;
}

export function highlightMatches(text: string, query: string): ReactNode {
  if (!query) return text;

  const searchText = text.toLowerCase();
  const searchQuery = query.toLowerCase();
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let matchCount = 0;

  let index = searchText.indexOf(searchQuery, lastIndex);
  while (index !== -1) {
    if (index > lastIndex) {
      parts.push(
        <span key={`text-${matchCount}`}>
          {text.substring(lastIndex, index)}
        </span>
      );
    }

    parts.push(
      <mark
        key={`match-${matchCount}`}
        style={{
          background: 'var(--highlight)',
          color: 'var(--foreground)',
          padding: '1px 2px',
          borderRadius: 2,
        }}
      >
        {text.substring(index, index + query.length)}
      </mark>
    );

    lastIndex = index + query.length;
    index = searchText.indexOf(searchQuery, lastIndex);
    matchCount++;
  }

  if (lastIndex < text.length) {
    parts.push(
      <span key={`text-${matchCount}`}>
        {text.substring(lastIndex)}
      </span>
    );
  }

  return <>{parts}</>;
}

export const UnifiedSearch = forwardRef<UnifiedSearchRef, UnifiedSearchProps>(
  function UnifiedSearch({ sessions, onSelectSession, onMessageClick, modKey }, ref) {
  const [query, setQuery] = useState('');
  const [isOpen, setIsOpen] = useState(false);
  const [messageResults, setMessageResults] = useState<MessageSearchResult[]>([]);
  const [isSearchingMessages, setIsSearchingMessages] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Expose focus method to parent
  useImperativeHandle(ref, () => ({
    focus: () => {
      inputRef.current?.focus();
    },
  }));
  const debounceTimerRef = useRef<NodeJS.Timeout | undefined>(undefined);

  // Filter sessions based on query
  const filteredSessions = query.trim()
    ? sessions.filter((session) => {
        const searchLower = query.toLowerCase();
        return (
          session.name.toLowerCase().includes(searchLower) ||
          session.title?.toLowerCase().includes(searchLower) ||
          session.summary?.toLowerCase().includes(searchLower)
        );
      }).slice(0, 5) // Limit to 5 sessions
    : [];

  // Search messages via API
  // Only ever invoked with a non-blank query (the debounce effect clears
  // results for blank input instead of calling this).
  const searchMessages = useCallback(async (searchQuery: string) => {
    setIsSearchingMessages(true);
    try {
      const params = new URLSearchParams({
        query: searchQuery,
        case_sensitive: 'false',
        limit: '5',
      });

      const res = await fetch(`/api/v1/sessions/search?${params}`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const data = await res.json();
      setMessageResults(data.results || []);
    } catch (e) {
      console.error('Message search failed:', e);
      setMessageResults([]);
    } finally {
      setIsSearchingMessages(false);
    }
  }, []);

  // Debounced message search
  useEffect(() => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    if (query.trim()) {
      debounceTimerRef.current = setTimeout(() => {
        searchMessages(query);
      }, 300);
    } else {
      setMessageResults([]);
    }

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [query, searchMessages]);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsOpen(false);
        inputRef.current?.blur();
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, []);

  const handleSessionClick = (sessionName: string) => {
    onSelectSession(sessionName);
    setQuery('');
    setIsOpen(false);
  };

  const handleMessageClick = (result: MessageSearchResult) => {
    onMessageClick(result.session_name, result.message_index);
    setQuery('');
    setIsOpen(false);
  };

  const showDropdown = isOpen && Boolean(query.trim());

  return (
    <div ref={containerRef} style={{ position: 'relative', marginBottom: 12 }}>
      {/* Search Input */}
      <div style={{ position: 'relative' }}>
        <span
          style={{
            position: 'absolute',
            left: 10,
            top: '50%',
            transform: 'translateY(-50%)',
            fontSize: 16,
            opacity: 0.6,
            pointerEvents: 'none',
          }}
        >
          🔍
        </span>
        <input
          ref={inputRef}
          type="text"
          placeholder="Search sessions & messages..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={`Search sessions and messages (${modKey}+F)`}
          title={`Search sessions and messages (${modKey}+F)`}
          style={{
            width: '100%',
            padding: '10px 12px 10px 36px',
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
            setIsOpen(true);
            e.currentTarget.style.outline = '2px solid var(--success)';
          }}
          onBlur={(e) => {
            e.currentTarget.style.outline = '2px solid transparent';
          }}
        />
      </div>

      {/* Dropdown Results */}
      {showDropdown && (
        <div
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            marginTop: 4,
            background: 'var(--sidebar-bg)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
            zIndex: 1000,
            maxHeight: 400,
            overflowY: 'auto',
          }}
        >
          {/* Sessions Section */}
          {filteredSessions.length > 0 && (
            <div>
              <div
                style={{
                  padding: '8px 12px',
                  fontSize: 11,
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  opacity: 0.6,
                  borderBottom: '1px solid var(--border)',
                }}
              >
                Sessions
              </div>
              {filteredSessions.map((session) => (
                <div
                  key={session.name}
                  onClick={() => handleSessionClick(session.name)}
                  style={{
                    padding: '10px 12px',
                    cursor: 'pointer',
                    borderBottom: '1px solid var(--border-light)',
                    transition: 'background 0.15s ease',
                  }}
                  onMouseOver={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                  onMouseOut={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 2 }}>
                    {session.pinned && <span style={{ marginRight: 4 }}>📌</span>}
                    {highlightMatches(session.title || session.name, query)}
                  </div>
                  {session.summary && (
                    <div style={{ fontSize: 12, opacity: 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {highlightMatches(session.summary, query)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Messages Section */}
          {(messageResults.length > 0 || isSearchingMessages) && (
            <div>
              <div
                style={{
                  padding: '8px 12px',
                  fontSize: 11,
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  opacity: 0.6,
                  borderBottom: '1px solid var(--border)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                Messages
                {isSearchingMessages && (
                  <span style={{ fontSize: 10, opacity: 0.7 }}>searching...</span>
                )}
              </div>
              {messageResults.map((result, index) => (
                <div
                  key={`${result.session_name}-${result.message_index}-${index}`}
                  onClick={() => handleMessageClick(result)}
                  style={{
                    padding: '10px 12px',
                    cursor: 'pointer',
                    borderBottom: '1px solid var(--border-light)',
                    transition: 'background 0.15s ease',
                  }}
                  onMouseOver={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
                  onMouseOut={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ fontSize: 14 }}>
                      {result.role === 'user' ? '👤' : result.role === 'assistant' ? '🤖' : '⚙️'}
                    </span>
                    <span style={{ fontSize: 12, fontWeight: 500 }}>
                      {result.session_title || result.session_name}
                    </span>
                    {result.matches > 1 && (
                      <span
                        style={{
                          fontSize: 10,
                          padding: '1px 5px',
                          background: 'var(--success)',
                          color: 'white',
                          borderRadius: 10,
                        }}
                      >
                        {result.matches}
                      </span>
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      opacity: 0.85,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {highlightMatches(result.content_preview, query)}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* No Results */}
          {!isSearchingMessages && filteredSessions.length === 0 && messageResults.length === 0 && (
            <div
              style={{
                padding: '20px 12px',
                textAlign: 'center',
                fontSize: 13,
                opacity: 0.6,
              }}
            >
              No results found for &quot;{query}&quot;
            </div>
          )}
        </div>
      )}
    </div>
  );
});

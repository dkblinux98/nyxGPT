'use client';

import { useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { useToast } from '../contexts/ToastContext';
import LoadingSpinner from './LoadingSpinner';

export interface SearchResult {
  session_name: string;
  session_title: string | null;
  message_index: number;
  role: string;
  content: string;
  content_preview: string;
  timestamp: string | null;
  matches: number;
}

export interface SearchFilters {
  caseSensitive: boolean;
  roleFilter: string | null;
}

export interface SearchModalProps {
  isOpen: boolean;
  onClose: () => void;
  onResultClick: (sessionName: string, messageIndex: number) => void;
}

function highlightMatches(text: string, query: string, caseSensitive: boolean): ReactNode {
  if (!query) return text;

  const searchText = caseSensitive ? text : text.toLowerCase();
  const searchQuery = caseSensitive ? query : query.toLowerCase();
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let matchCount = 0;

  let index = searchText.indexOf(searchQuery, lastIndex);
  while (index !== -1) {
    // Add text before match
    if (index > lastIndex) {
      parts.push(
        <span key={`text-${matchCount}`}>
          {text.substring(lastIndex, index)}
        </span>
      );
    }

    // Add highlighted match
    parts.push(
      <mark
        key={`match-${matchCount}`}
        style={{
          background: 'var(--highlight)',
          color: 'var(--foreground)',
          padding: '2px 0',
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

  // Add remaining text
  if (lastIndex < text.length) {
    parts.push(
      <span key={`text-${matchCount}`}>
        {text.substring(lastIndex)}
      </span>
    );
  }

  return <>{parts}</>;
}

export function SearchModal({ isOpen, onClose, onResultClick }: SearchModalProps) {
  const toast = useToast();
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState<SearchFilters>({
    caseSensitive: false,
    roleFilter: null,
  });
  const [results, setResults] = useState<SearchResult[]>([]);
  const [totalResults, setTotalResults] = useState(0);
  const [isSearching, setIsSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);

  const searchInputRef = useRef<HTMLInputElement>(null);
  const debounceTimerRef = useRef<NodeJS.Timeout | undefined>(undefined);

  // Auto-focus search input when modal opens
  useEffect(() => {
    if (isOpen && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [isOpen]);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Perform search
  const performSearch = useCallback(async (searchQuery: string, searchFilters: SearchFilters) => {
    if (!searchQuery.trim()) {
      setResults([]);
      setTotalResults(0);
      setHasSearched(false);
      return;
    }

    setIsSearching(true);
    setError(null);
    setHasSearched(true);

    try {
      const params = new URLSearchParams({
        query: searchQuery,
        case_sensitive: searchFilters.caseSensitive.toString(),
        limit: '50',
      });

      if (searchFilters.roleFilter) {
        params.append('role_filter', searchFilters.roleFilter);
      }

      const res = await fetch(`/api/v1/sessions/search?${params}`);

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(errorData.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      setResults(data.results);
      setTotalResults(data.total_results);
    } catch (e) {
      const errorMsg = e instanceof Error ? e.message : String(e);
      setError(`Search failed: ${errorMsg}`);
      toast.error(`Search failed: ${errorMsg}`);
    } finally {
      setIsSearching(false);
    }
  }, [toast]);

  // Debounced search on query change
  useEffect(() => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    debounceTimerRef.current = setTimeout(() => {
      if (query.trim()) {
        performSearch(query, filters);
      } else {
        setResults([]);
        setTotalResults(0);
        setHasSearched(false);
      }
    }, 300);

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [query, filters, performSearch]);

  const handleResultClick = (result: SearchResult) => {
    onResultClick(result.session_name, result.message_index);
    onClose();
  };

  const handleFilterChange = (key: keyof SearchFilters, value: boolean | string | null) => {
    setFilters(prev => ({ ...prev, [key]: value }));
  };

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          background: 'rgba(0, 0, 0, 0.5)',
          backdropFilter: 'blur(4px)',
          zIndex: 1000,
        }}
      />

      {/* Modal */}
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'fixed',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          width: 'min(800px, 90vw)',
          maxHeight: '80vh',
          background: 'var(--background)',
          border: '1px solid var(--border)',
          borderRadius: 12,
          boxShadow: '0 8px 32px rgba(0, 0, 0, 0.2)',
          zIndex: 1001,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '16px 20px',
            borderBottom: '1px solid var(--border)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>
            Search Messages
          </h2>
          <button
            onClick={onClose}
            aria-label="Close search"
            style={{
              background: 'transparent',
              border: 'none',
              fontSize: 24,
              color: 'var(--foreground)',
              cursor: 'pointer',
              padding: '0 4px',
              opacity: 0.7,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = '1')}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = '0.7')}
          >
            ×
          </button>
        </div>

        {/* Search Form */}
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)' }}>
          <input
            ref={searchInputRef}
            type="text"
            placeholder="Search message content..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{
              width: '100%',
              padding: '10px 12px',
              border: '1px solid var(--border)',
              borderRadius: 6,
              background: 'var(--input-bg)',
              color: 'var(--foreground)',
              fontSize: 14,
              marginBottom: 12,
            }}
          />

          {/* Filters */}
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
            {/* Case Sensitive Checkbox */}
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={filters.caseSensitive}
                onChange={(e) => handleFilterChange('caseSensitive', e.target.checked)}
                style={{ cursor: 'pointer' }}
              />
              <span>Case sensitive</span>
            </label>

            {/* Role Filter */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span style={{ fontSize: 13, opacity: 0.8 }}>Role:</span>
              <select
                value={filters.roleFilter || ''}
                onChange={(e) => handleFilterChange('roleFilter', e.target.value || null)}
                style={{
                  padding: '4px 8px',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  background: 'var(--input-bg)',
                  color: 'var(--foreground)',
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                <option value="">All</option>
                <option value="user">User</option>
                <option value="assistant">Assistant</option>
                <option value="system">System</option>
              </select>
            </div>

            {/* Results Count */}
            {hasSearched && !isSearching && (
              <span style={{ fontSize: 13, opacity: 0.7, marginLeft: 'auto' }}>
                {totalResults === 0 ? 'No results' : `${totalResults} result${totalResults !== 1 ? 's' : ''}`}
              </span>
            )}
          </div>
        </div>

        {/* Results List */}
        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '16px 20px',
          }}
        >
          {isSearching && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
              <LoadingSpinner size="medium" label="Searching..." />
            </div>
          )}

          {error && !isSearching && (
            <div
              style={{
                padding: 16,
                background: 'rgba(239, 68, 68, 0.1)',
                border: '1px solid rgba(239, 68, 68, 0.3)',
                borderRadius: 8,
                color: 'var(--error)',
                fontSize: 14,
              }}
            >
              {error}
            </div>
          )}

          {!isSearching && !error && hasSearched && results.length === 0 && (
            <div
              style={{
                textAlign: 'center',
                padding: '40px 20px',
                color: 'var(--foreground)',
                opacity: 0.6,
                fontSize: 14,
              }}
            >
              No messages found matching &quot;{query}&quot;
            </div>
          )}

          {!isSearching && !error && results.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {results.map((result, index) => (
                <div
                  key={`${result.session_name}-${result.message_index}-${index}`}
                  onClick={() => handleResultClick(result)}
                  style={{
                    padding: 12,
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    cursor: 'pointer',
                    background: 'var(--input-bg)',
                    transition: 'all 0.15s ease',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = 'var(--success)';
                    e.currentTarget.style.background = 'rgba(76, 175, 80, 0.05)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = 'var(--border)';
                    e.currentTarget.style.background = 'var(--input-bg)';
                  }}
                >
                  {/* Session Title and Role */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    <span style={{ fontSize: 16 }}>
                      {result.role === 'user' ? '👤' : result.role === 'assistant' ? '🤖' : '⚙️'}
                    </span>
                    <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>
                      {result.session_title || result.session_name}
                    </span>
                    {result.matches > 1 && (
                      <span
                        style={{
                          fontSize: 11,
                          padding: '2px 6px',
                          background: 'var(--success)',
                          color: 'white',
                          borderRadius: 12,
                          fontWeight: 500,
                        }}
                      >
                        {result.matches} matches
                      </span>
                    )}
                  </div>

                  {/* Preview with Highlights */}
                  <div
                    style={{
                      fontSize: 13,
                      lineHeight: 1.5,
                      color: 'var(--foreground)',
                      opacity: 0.9,
                      wordBreak: 'break-word',
                    }}
                  >
                    {highlightMatches(result.content_preview, query, filters.caseSensitive)}
                  </div>

                  {/* Timestamp */}
                  {result.timestamp && (
                    <div style={{ fontSize: 11, opacity: 0.6, marginTop: 6 }}>
                      {new Date(result.timestamp).toLocaleString()}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {!isSearching && !error && !hasSearched && (
            <div
              style={{
                textAlign: 'center',
                padding: '40px 20px',
                color: 'var(--foreground)',
                opacity: 0.6,
                fontSize: 14,
              }}
            >
              Enter a search term to find messages across all sessions
            </div>
          )}
        </div>
      </div>
    </>
  );
}

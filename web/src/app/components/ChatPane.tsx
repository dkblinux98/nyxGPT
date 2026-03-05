'use client';

import { Component, useCallback, useEffect, useRef, useState } from 'react';
import { Virtuoso, VirtuosoHandle } from 'react-virtuoso';
import LoadingSpinner from '../../components/LoadingSpinner';
import { useToast } from '../../contexts/ToastContext';

// Error Boundary for Virtuoso rendering
class VirtuosoErrorBoundary extends Component<
  {
    children: React.ReactNode;
    sessionName: string;
    messages: ChatMessage[];
    itemContent: (idx: number, m: ChatMessage) => React.ReactNode;
  },
  { hasError: boolean; error: Error | null; useFallback: boolean }
> {
  constructor(props: {
    children: React.ReactNode;
    sessionName: string;
    messages: ChatMessage[];
    itemContent: (idx: number, m: ChatMessage) => React.ReactNode;
  }) {
    super(props);
    this.state = { hasError: false, error: null, useFallback: false };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // Log to console for debugging (sanitized for production)
    console.error('[VirtuosoErrorBoundary] Rendering error:', {
      message: error.message,
      componentStack: errorInfo.componentStack?.split('\n')[0], // Only log first line
    });

    // In production, send to telemetry service
    if (typeof window !== 'undefined' && (window as any).telemetry) {
      (window as any).telemetry.captureException(error, {
        context: 'VirtuosoErrorBoundary',
        sessionName: this.props.sessionName,
      });
    }
  }

  componentDidUpdate(prevProps: { sessionName: string }) {
    // Clear error state when session changes
    if (prevProps.sessionName !== this.props.sessionName && this.state.hasError) {
      this.setState({ hasError: false, error: null, useFallback: false });
    }
  }

  // Sanitize error message for user display (no stack traces, no internal paths)
  private getSafeErrorMessage(error: Error | null): string {
    if (!error) return 'An unknown error occurred';

    const message = error.message || 'Unknown error';

    // Remove file paths and line numbers
    const sanitized = message.replace(/\s*at\s+.*$/gm, '').replace(/\/[^\s]+\//g, '');

    // Limit length to prevent information leakage
    return sanitized.length > 200 ? sanitized.substring(0, 200) + '...' : sanitized;
  }

  render() {
    if (this.state.hasError) {
      const safeMessage = this.getSafeErrorMessage(this.state.error);

      // If user chose fallback, render without virtualization
      if (this.state.useFallback) {
        return (
          <div style={{ height: '100%', overflowY: 'auto', padding: 12 }}>
            <div style={{
              padding: 8,
              marginBottom: 12,
              background: 'var(--warning-bg)',
              border: '1px solid var(--warning-border)',
              borderRadius: 6,
              fontSize: 12,
            }}>
              ⚠️ Rendering in fallback mode (virtual scrolling disabled)
            </div>
            {this.props.messages.map((m, idx) => (
              <div key={idx}>
                {this.props.itemContent(idx, m)}
              </div>
            ))}
          </div>
        );
      }

      // Error state with recovery options
      return (
        <div style={{ padding: 12, color: 'var(--error)' }}>
          <strong>Failed to render messages</strong>
          <div style={{ fontSize: 12, marginTop: 8, opacity: 0.8, color: 'var(--foreground)' }}>
            {safeMessage}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button
              onClick={() => this.setState({ hasError: false, error: null, useFallback: false })}
              style={{
                padding: '6px 12px',
                borderRadius: 4,
                border: '1px solid var(--border)',
                background: 'var(--button-hover)',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              Retry
            </button>
            <button
              onClick={() => this.setState({ hasError: false, error: null, useFallback: true })}
              style={{
                padding: '6px 12px',
                borderRadius: 4,
                border: '1px solid var(--border)',
                background: 'var(--warning-bg)',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              Use Fallback Mode
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  ragChunks?: RagChunk[];
  rag_chunks?: RagChunk[]; // Backend uses snake_case
  id?: string;
  timestamp?: string;
  edited_at?: string;
  original_content?: string;
};

type RagChunk = {
  text: string;
  score: number;
  similarity_score?: number | null;  // Original vector similarity (0-1), used for UI display
  doc_id?: string | null;
  chunk_id?: number | null;
};

type RagConfig = {
  min_score: number;
  good_score_threshold: number;
  medium_score_threshold: number;
};

type Props = {
  sessionName: string;
  onSessionUpdated?: () => void;
  scrollToMessageIndex?: number | null;
  releaseVersion?: string | null;
};

// Helper function to get score quality and color
function getScoreQuality(score: number, config: RagConfig | null): { quality: string; color: string; label: string } {
  if (!config) {
    return { quality: 'unknown', color: '#6b7280', label: 'N/A' };
  }

  if (score >= config.good_score_threshold) {
    return { quality: 'high', color: '#10b981', label: 'High' }; // green
  } else if (score >= config.medium_score_threshold) {
    return { quality: 'medium', color: '#f59e0b', label: 'Medium' }; // yellow/orange
  } else {
    return { quality: 'low', color: '#ef4444', label: 'Low' }; // red
  }
}

function RagCitationsCollapsible({
  sessionName,
  messageIndex,
  initialChunks,
  onChunksLoaded,
}: {
  sessionName: string;
  messageIndex: number;
  initialChunks?: RagChunk[];
  onChunksLoaded?: (chunks: RagChunk[]) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [chunks, setChunks] = useState<RagChunk[] | null>(initialChunks || null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ragConfig, setRagConfig] = useState<RagConfig | null>(null);
  const [expandedChunks, setExpandedChunks] = useState<Set<number>>(new Set());

  const handleToggle = async () => {
    const newExpanded = !expanded;
    setExpanded(newExpanded);

    // Lazy load chunks on first expand if not already loaded
    if (newExpanded && !chunks && !loading) {
      setLoading(true);
      setError(null);

      try {
        const chunksRes = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/messages/${messageIndex}/rag`);

        if (!chunksRes.ok) {
          throw new Error(`Failed to load RAG chunks: ${chunksRes.status}`);
        }

        const data = await chunksRes.json();
        const loadedChunks = data.chunks || [];
        setChunks(loadedChunks);

        // Notify parent to cache the loaded chunks
        if (onChunksLoaded) {
          onChunksLoaded(loadedChunks);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        console.error('Failed to load RAG chunks:', err);
      } finally {
        setLoading(false);
      }
    }

    // Fetch config separately if not yet loaded (retry on each expand if missing)
    if (newExpanded && !ragConfig) {
      try {
        const configRes = await fetch('/api/v1/rag/config');
        if (configRes.ok) {
          const configData = await configRes.json();
          setRagConfig(configData);
        }
      } catch {
        // Config fetch is non-critical, ignore errors
      }
    }
  };

  const chunkCount = chunks?.length || 0;
  const hasChunks = chunkCount > 0 || initialChunks === undefined; // Show indicator if chunks unknown

  return (
    <div
      style={{
        marginBottom: 8,
        padding: 8,
        background: 'var(--rag-bg)',
        border: '1px solid var(--rag-border)',
        borderRadius: 6,
        fontSize: 12,
      }}
    >
      <button
        onClick={handleToggle}
        style={{
          background: 'none',
          border: 'none',
          padding: 0,
          cursor: 'pointer',
          color: 'var(--rag-text)',
          fontWeight: 500,
          display: 'flex',
          alignItems: 'center',
          gap: 4,
        }}
      >
        <span>{expanded ? '▼' : '▶'}</span>
        <span>
          {loading
            ? 'Loading RAG sources...'
            : chunks
            ? `${chunkCount} RAG ${chunkCount === 1 ? 'source' : 'sources'} retrieved`
            : 'RAG sources available'}
        </span>
      </button>

      {expanded && !loading && !error && chunks && (
        <div style={{ marginTop: 8 }}>
          {/* Score explanation */}
          <div style={{
            fontSize: 11,
            color: 'var(--foreground)',
            opacity: 0.7,
            marginBottom: 8,
            padding: '6px 8px',
            background: 'var(--background)',
            borderRadius: 4,
            border: '1px solid var(--border)',
          }}>
            <strong>Confidence scores:</strong> Higher scores indicate stronger relevance.
            {ragConfig && (
              <span>
                {' '}≥{ragConfig.good_score_threshold.toFixed(1)} = <span style={{ color: '#10b981', fontWeight: 500 }}>High</span>,
                {' '}≥{ragConfig.medium_score_threshold.toFixed(1)} = <span style={{ color: '#f59e0b', fontWeight: 500 }}>Medium</span>,
                {' '}&lt;{ragConfig.medium_score_threshold.toFixed(1)} = <span style={{ color: '#ef4444', fontWeight: 500 }}>Low</span>
              </span>
            )}
          </div>

          {chunks.map((chunk, idx) => {
            // Use similarity_score for display (original vector score)
            // If null, result was found by keyword search only
            const hasVectorScore = chunk.similarity_score != null;
            const displayScore = chunk.similarity_score ?? chunk.score;
            const scoreInfo = hasVectorScore
              ? getScoreQuality(displayScore, ragConfig)
              : { quality: 'keyword', color: '#6366f1', label: 'Keyword' }; // indigo for keyword-only
            return (
              <div
                key={idx}
                style={{
                  marginTop: idx > 0 ? 8 : 0,
                  padding: 8,
                  background: 'var(--input-bg)',
                  borderRadius: 4,
                  border: '1px solid #e0f2fe',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                  <span style={{ fontWeight: 500, color: 'var(--rag-text)' }}>
                    Source {idx + 1}
                  </span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span
                      style={{
                        color: 'white',
                        background: scoreInfo.color,
                        padding: '2px 6px',
                        borderRadius: 4,
                        fontSize: 11,
                        fontWeight: 600,
                      }}
                      title={`Confidence: ${scoreInfo.label}`}
                    >
                      {scoreInfo.label}
                    </span>
                    <span style={{ color: 'var(--foreground)', opacity: 0.6, fontSize: 12 }}>
                      {displayScore.toFixed(3)}
                    </span>
                  </div>
                </div>
                {chunk.doc_id && (
                  <div style={{ fontSize: 11, color: 'var(--foreground)', opacity: 0.6, marginBottom: 4 }}>
                    Doc: {chunk.doc_id}
                    {chunk.chunk_id !== null && chunk.chunk_id !== undefined && ` (chunk ${chunk.chunk_id})`}
                  </div>
                )}
                <div style={{ color: 'var(--foreground)', whiteSpace: 'pre-wrap' }}>
                  {expandedChunks.has(idx) || chunk.text.length <= 200 ? (
                    chunk.text
                  ) : (
                    chunk.text.substring(0, 200) + '...'
                  )}
                </div>
                {chunk.text.length > 200 && (
                  <button
                    onClick={() => {
                      setExpandedChunks((prev) => {
                        const next = new Set(prev);
                        if (next.has(idx)) {
                          next.delete(idx);
                        } else {
                          next.add(idx);
                        }
                        return next;
                      });
                    }}
                    style={{
                      marginTop: 6,
                      padding: '4px 8px',
                      fontSize: 11,
                      border: '1px solid var(--border)',
                      borderRadius: 4,
                      background: 'var(--background)',
                      color: 'var(--foreground)',
                      cursor: 'pointer',
                    }}
                  >
                    {expandedChunks.has(idx) ? 'Show less' : 'Show full source'}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {expanded && loading && (
        <div style={{ marginTop: 8, opacity: 0.6, textAlign: 'center' }}>
          Loading...
        </div>
      )}

      {expanded && error && (
        <div style={{ marginTop: 8, color: 'var(--error)', fontSize: 11 }}>
          Error: {error}
        </div>
      )}
    </div>
  );
}

export default function ChatPane({ sessionName, onSessionUpdated, scrollToMessageIndex, releaseVersion }: Props) {
  const toast = useToast();
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<'idle' | 'connecting' | 'streaming' | 'error'>('idle');
  const [lastError, setLastError] = useState<string | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [showModelDropdown, setShowModelDropdown] = useState<boolean>(false);
  const isStreamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [highlightedMessageIndex, setHighlightedMessageIndex] = useState<number | null>(null);

  // Track scroll state to prevent infinite re-renders
  const lastMessageCountRef = useRef(0);
  const isAtBottomRef = useRef(true);

  // RAG state
  const [ragEnabled, setRagEnabled] = useState<boolean>(false);
  const [ragStatus, setRagStatus] = useState<'idle' | 'uploading' | 'error'>('idle');
  const [ragError, setRagError] = useState<string | null>(null);
  const [ragChunksCache, setRagChunksCache] = useState<Map<number, RagChunk[]>>(new Map());

  // RAG filters state
  const [showRagFilters, setShowRagFilters] = useState<boolean>(false);
  const [ragFilters, setRagFilters] = useState<{
    doc_ids?: string[];
    filename?: string;
    tags?: string[];
    date_from?: string;
    date_to?: string;
  }>({});
  const [availableDocuments, setAvailableDocuments] = useState<Array<{
    doc_id: string;
    filename: string | null;
    chunks: number;
    tags: string[] | null;
    ingested_at: string | null;
  }>>([]);

  // Attached documents state (force-include for RAG)
  const [attachedDocIds, setAttachedDocIds] = useState<string[]>([]);
  const [showAttachedDocs, setShowAttachedDocs] = useState<boolean>(false);
  const [attachDocInput, setAttachDocInput] = useState<string>('');

  // Upload menu state
  const [showUploadMenu, setShowUploadMenu] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Rename state
  const [sessionTitle, setSessionTitle] = useState<string>('');
  const [renaming, setRenaming] = useState<boolean>(false);

  // Edit state
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Pagination state
  const [totalMessages, setTotalMessages] = useState<number>(0);
  const [loadedOffset, setLoadedOffset] = useState<number>(0);
  const [isLoadingMore, setIsLoadingMore] = useState<boolean>(false);
  const [hasMore, setHasMore] = useState<boolean>(true);
  const [firstItemIndex, setFirstItemIndex] = useState<number>(0);
  const PAGE_SIZE = 50;

  // Fetch available models on mount
  useEffect(() => {
    async function fetchModels() {
      try {
        const res = await fetch('/api/models');
        if (res.ok) {
          const data = await res.json();
          const models = data.models || [];
          setAvailableModels(models);
          // Set first model as default if none selected
          if (models.length > 0 && !selectedModel) {
            setSelectedModel(models[0]);
          }
        } else {
          console.error('Failed to fetch models:', res.status, res.statusText);
        }
      } catch (err) {
        console.error('Failed to fetch models:', err);
      }
    }
    fetchModels();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // New session selected: clear local transcript UI and load historical messages
    setMessages([]);
    setStatus('idle');
    setLastError(null);
    setInput('');
    setSelectedModel(''); // Reset model selector immediately
    setTotalMessages(0);
    setLoadedOffset(0);
    setHasMore(true);
    isStreamingRef.current = false;
    abortRef.current?.abort();
    abortRef.current = null;

    // Load persisted RAG filters from session storage
    try {
      const savedFilters = sessionStorage.getItem(`rag_filters_${sessionName}`);
      if (savedFilters) {
        setRagFilters(JSON.parse(savedFilters));
      } else {
        setRagFilters({});
      }
    } catch (err) {
      console.error('Failed to load RAG filters from session storage:', err);
      setRagFilters({});
    }

    // Fetch session metadata (RAG status, title, and model)
    fetch(`/api/sessions/${encodeURIComponent(sessionName)}/metadata`)
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          setRagEnabled(data.rag_enabled || false);
          setSessionTitle(data.title || '');
          setSelectedModel(data.model || '');
          setRagError(null);

          // Fetch available documents if RAG is enabled
          if (data.rag_enabled) {
            fetchAvailableDocuments();
          }
        }
      })
      .catch((err) => {
        console.error('Failed to fetch session metadata:', err);
        setRagEnabled(false); // Default to disabled if fetch fails
        setSessionTitle(''); // Default to empty title
        setSelectedModel(''); // Default to empty (will use first available model)
      });

    // Fetch attached documents for this session
    setAttachedDocIds([]);
    setShowAttachedDocs(false);
    fetch(`/api/sessions/${encodeURIComponent(sessionName)}/documents`)
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          setAttachedDocIds(data.attached_doc_ids || []);
        }
      })
      .catch((err) => {
        console.error('Failed to fetch attached documents:', err);
      });

    // Load most recent messages with pagination
    async function loadInitialMessages() {
      try {
        const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionName)}`);
        if (res.ok) {
          const data = await res.json();
          if (data.messages && Array.isArray(data.messages)) {
            const total = data.total || data.messages.length;
            setTotalMessages(total);

            // Calculate offset to load most recent PAGE_SIZE messages
            const offset = Math.max(0, total - PAGE_SIZE);

            if (offset > 0) {
              // Load paginated recent messages (Medium Issue 3: Add error handling)
              try {
                const paginatedRes = await fetch(
                  `/api/v1/sessions/${encodeURIComponent(sessionName)}?offset=${offset}&limit=${PAGE_SIZE}`
                );
                if (paginatedRes.ok) {
                  const paginatedData = await paginatedRes.json();
                  setMessages(paginatedData.messages || []);
                  setLoadedOffset(offset);
                  setHasMore(offset > 0);
                  setFirstItemIndex(offset);
                } else {
                  // Fallback: use all messages from initial fetch if pagination fails
                  console.warn('Pagination fetch failed, falling back to all messages');
                  setMessages(data.messages);
                  setLoadedOffset(0);
                  setHasMore(false);
                  setFirstItemIndex(0);
                }
              } catch (paginationErr) {
                // Fallback: use all messages from initial fetch if pagination fails
                console.error('Pagination fetch error, falling back:', paginationErr);
                setMessages(data.messages);
                setLoadedOffset(0);
                setHasMore(false);
                setFirstItemIndex(0);
              }
            } else {
              // All messages fit in one page
              setMessages(data.messages);
              setLoadedOffset(0);
              setHasMore(false);
              setFirstItemIndex(0);
            }
          }
        }
      } catch (err) {
        console.error('Failed to load session messages:', err);
      }
    }

    void loadInitialMessages();
  }, [sessionName, PAGE_SIZE]);

  // Save RAG filters to session storage when they change
  useEffect(() => {
    try {
      if (Object.keys(ragFilters).length > 0) {
        sessionStorage.setItem(`rag_filters_${sessionName}`, JSON.stringify(ragFilters));
      } else {
        sessionStorage.removeItem(`rag_filters_${sessionName}`);
      }
    } catch (err) {
      console.error('Failed to save RAG filters to session storage:', err);
    }
  }, [ragFilters, sessionName]);

  // Scroll to message when scrollToMessageIndex changes
  useEffect(() => {
    if (scrollToMessageIndex !== null && scrollToMessageIndex !== undefined) {
      // Wait a bit for messages to render
      setTimeout(() => {
        virtuosoRef.current?.scrollToIndex({
          index: scrollToMessageIndex,
          align: 'center',
          behavior: 'smooth',
        });
        // Highlight the message
        setHighlightedMessageIndex(scrollToMessageIndex);
        // Remove highlight after 2 seconds
        setTimeout(() => {
          setHighlightedMessageIndex(null);
        }, 2000);
      }, 100);
    }
  }, [scrollToMessageIndex, loadedOffset]);

  // Close model dropdown on Escape
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && showModelDropdown) {
        setShowModelDropdown(false);
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [showModelDropdown]);

  // Close upload menu on click outside or Escape
  useEffect(() => {
    const handleClick = () => setShowUploadMenu(false);
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShowUploadMenu(false);
    };
    if (showUploadMenu) {
      document.addEventListener('click', handleClick);
      document.addEventListener('keydown', handleEscape);
      return () => {
        document.removeEventListener('click', handleClick);
        document.removeEventListener('keydown', handleEscape);
      };
    }
  }, [showUploadMenu]);

  // Load older messages function
  // Helper to reset pagination state (Medium Issue 6: Called after message mutations)
  const resetPaginationState = useCallback((newMessages: ChatMessage[]) => {
    setTotalMessages(newMessages.length);
    setLoadedOffset(0);
    setHasMore(false);
    setFirstItemIndex(0);
  }, []);

  // Load older messages when scrolling to top (Critical Issue 2: Use Virtuoso's startReached)
  const loadOlderMessages = useCallback(async () => {
    if (isLoadingMore || !hasMore) return;

    const newOffset = Math.max(0, loadedOffset - PAGE_SIZE);
    if (newOffset === loadedOffset) {
      setHasMore(false);
      return;
    }

    setIsLoadingMore(true);

    try {
      const res = await fetch(
        `/api/v1/sessions/${encodeURIComponent(sessionName)}?offset=${newOffset}&limit=${PAGE_SIZE}`
      );
      if (res.ok) {
        const data = await res.json();
        const olderMessages = data.messages || [];

        // Prepend older messages and adjust firstItemIndex for Virtuoso
        setMessages((prev) => [...olderMessages, ...prev]);
        setLoadedOffset(newOffset);
        setHasMore(newOffset > 0);
        setFirstItemIndex(newOffset);
      }
    } catch (err) {
      console.error('Failed to load older messages:', err);
      toast.error('Failed to load older messages');
    } finally {
      setIsLoadingMore(false);
    }
  }, [isLoadingMore, hasMore, loadedOffset, sessionName, PAGE_SIZE, toast]);

  // Auto-scroll to bottom when streaming new messages (ref-based to prevent infinite re-renders)
  useEffect(() => {
    // Only scroll if:
    // 1. Currently streaming
    // 2. Messages were added (not just updated)
    // 3. User is at bottom (hasn't manually scrolled away)
    if (isStreaming && messages.length > lastMessageCountRef.current && isAtBottomRef.current) {
      virtuosoRef.current?.scrollToIndex({
        index: messages.length - 1,
        align: 'end',
        behavior: 'auto',
      });
    }
    lastMessageCountRef.current = messages.length;
  }, [isStreaming, messages.length]);

  async function fetchAvailableDocuments() {
    try {
      const res = await fetch('/api/v1/rag/documents');
      if (res.ok) {
        const data = await res.json();
        setAvailableDocuments(data.documents || []);
      }
    } catch (err) {
      console.error('Failed to fetch available documents:', err);
    }
  }

  async function attachDocument(docId: string) {
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/documents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId }),
      });
      if (!res.ok) throw new Error('Failed to attach document');
      const data = await res.json();
      setAttachedDocIds(data.attached_doc_ids || []);
    } catch (err) {
      console.error('Failed to attach document:', err);
    }
  }

  async function detachDocument(docId: string) {
    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sessionName)}/documents/${encodeURIComponent(docId)}`,
        { method: 'DELETE' }
      );
      if (!res.ok) throw new Error('Failed to detach document');
      const data = await res.json();
      setAttachedDocIds(data.attached_doc_ids || []);
    } catch (err) {
      console.error('Failed to detach document:', err);
    }
  }

  async function toggleRag() {
    try {
      const endpoint = ragEnabled ? 'disable' : 'enable';
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/rag/${endpoint}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error(`Failed to ${endpoint} RAG`);
      const newRagEnabled = !ragEnabled;
      setRagEnabled(newRagEnabled);
      setRagError(null);

      // Fetch available documents when enabling RAG
      if (newRagEnabled) {
        fetchAvailableDocuments();
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setRagError(msg);
      setRagStatus('error');
    }
  }

  async function uploadFile(file: File) {
    setRagStatus('uploading');
    setRagError(null);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch('/api/rag/upload', {
        method: 'POST',
        body: formData,
      });
      if (!res.ok) throw new Error('Upload failed');

      const data = await res.json();
      console.log('File uploaded:', data.doc_id);
      setRagStatus('idle');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setRagError(msg);
      setRagStatus('error');
    }
  }

  async function renameSession() {
    const newName = prompt('Enter new session name or title:', sessionTitle || sessionName);
    if (!newName || newName.trim() === '') return;

    setRenaming(true);
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
      setSessionTitle(newName.trim());

      // If filename was synced, we might need to reload
      // For now, just update the title display
      if (data.new_name !== sessionName) {
        // Filename changed - reload the page to update URL
        window.location.href = `/?session=${encodeURIComponent(data.new_name)}`;
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to rename session: ${msg}`);
    } finally {
      setRenaming(false);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || isStreamingRef.current) return;

    // Clear edit state if we were editing
    if (editingIndex !== null) {
      setEditingIndex(null);
    }

    // Check if this is the first message (for auto-titling)
    const isFirstMessage = messages.length === 0;

    // Optimistic append user message
    setMessages((prev) => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '' }]);
    setStatus('connecting');
    setInput('');
    setIsStreaming(true);
    isStreamingRef.current = true;
    setLastError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      // Build request body with optional rag_filters
      const requestBody: any = {
        session: sessionName,
        prompt: text,
        model: selectedModel || undefined,
        rag_enabled: ragEnabled,
      };

      // Add rag_filters if any filters are set
      if (ragEnabled && (ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to)) {
        requestBody.rag_filters = ragFilters;
      }

      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
        signal: controller.signal,
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      if (!res.body) {
        throw new Error('No response body (streaming not supported)');
      }

      setStatus('streaming');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      let buffer = '';
      let ragChunks: RagChunk[] | undefined = undefined;

      // SSE parser: processes Server-Sent Events from the stream
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE events (events end with \n\n)
        const events = buffer.split('\n\n');
        // Keep the last incomplete event in the buffer
        buffer = events.pop() || '';

        for (const eventText of events) {
          if (!eventText.trim()) continue;

          // Parse SSE event format
          const lines = eventText.split('\n');
          let eventType = 'message'; // default event type
          let eventData = '';

          for (const line of lines) {
            if (line.startsWith('event:')) {
              eventType = line.substring(6).trim();
            } else if (line.startsWith('data:')) {
              eventData = line.substring(5).trim();
            }
            // id: and comment lines are ignored for now
          }

          // Handle different event types
          if (eventType === 'heartbeat') {
            // Heartbeat received, connection is alive
            continue;
          } else if (eventType === 'metadata') {
            // Parse metadata (session, model, timestamp)
            try {
              const metadata = JSON.parse(eventData);
              // Metadata event can be used for UI indicators if needed
              // For now, just log it for debugging
              console.debug('Stream metadata:', metadata);
            } catch (e) {
              console.error('Failed to parse metadata:', e);
            }
          } else if (eventType === 'rag_context' || eventType === 'rag_metadata') {
            // Parse RAG context/metadata (support both old and new names)
            try {
              const ragData = JSON.parse(eventData);
              if (ragData.type === 'rag_metadata' && Array.isArray(ragData.chunks)) {
                ragChunks = ragData.chunks;
                // Update message with RAG chunks
                setMessages((prev) => {
                  if (prev.length === 0) return prev;
                  const next = [...prev];
                  const last = next[next.length - 1];
                  if (last?.role === 'assistant') {
                    next[next.length - 1] = { ...last, ragChunks: ragChunks };
                  }
                  return next;
                });
              }
            } catch (e) {
              console.error('Failed to parse RAG context:', e);
            }
          } else if (eventType === 'text' || eventType === 'message') {
            // Parse text content (support both new 'text' and legacy 'message')
            try {
              const messageData = JSON.parse(eventData);
              const content = messageData.content || '';
              // Note: messageData.tokens and messageData.elapsed are available but not displayed yet

              // Append content to the last assistant message
              setMessages((prev) => {
                if (prev.length === 0) return prev;
                const next = [...prev];
                const last = next[next.length - 1];
                if (last?.role === 'assistant') {
                  next[next.length - 1] = { ...last, content: (last.content ?? '') + content };
                }
                return next;
              });
            } catch (e) {
              console.error('Failed to parse text data:', e);
            }
          } else if (eventType === 'error') {
            // Handle error event
            try {
              const errorData = JSON.parse(eventData);
              console.error('Stream error:', errorData);
              toast.error(`Error: ${errorData.error}`);
            } catch (e) {
              console.error('Failed to parse error data:', e);
            }
            break;
          } else if (eventType === 'retry') {
            // Handle retry status
            try {
              const retryData = JSON.parse(eventData);
              console.debug('Connection retry:', retryData);
              // Could show a toast notification for retries if desired
            } catch (e) {
              console.error('Failed to parse retry data:', e);
            }
          } else if (eventType === 'done') {
            // Stream completed
            try {
              const doneData = JSON.parse(eventData);
              // doneData contains total_tokens and elapsed time
              console.debug('Stream completed:', doneData);
            } catch (e) {
              // Ignore parse errors for done event
            }
            break;
          }
        }
      }

      // Auto-title the session based on first user message
      if (isFirstMessage) {
        try {
          // Create a title from the first ~50 chars of the user's message
          const autoTitle = text.length > 50 ? text.substring(0, 47) + '...' : text;
          await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/title`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: autoTitle }),
          });
        } catch (titleErr) {
          // Non-critical error, just log it
          console.warn('Failed to auto-title session:', titleErr);
        }
      }

      // Notify parent that session was updated (model metadata may have changed)
      onSessionUpdated?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLastError(msg);
      setStatus('error');

      setMessages((prev) => {
        if (prev.length === 0) {
          return [{ role: 'assistant', content: `[error] ${msg}` }];
        }
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === 'assistant') {
          next[next.length - 1] = { ...last, content: (last.content ?? '') + `\n\n[error] ${msg}` };
          return next;
        }
        return [...next, { role: 'assistant', content: `\n\n[error] ${msg}` }];
      });
    } finally {
      setIsStreaming(false);
      isStreamingRef.current = false;
      if (status !== 'error') setStatus('idle');
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
    setStatus('idle');
    isStreamingRef.current = false;
  }

  function handleEditMessage(index: number) {
    const message = messages[index];
    setEditingIndex(index);
    setInput(message.content);
  }

  function cancelEdit() {
    setEditingIndex(null);
    setInput('');
  }

  async function handleRegenerate(assistantIndex: number) {
    if (isStreamingRef.current) return;

    // Find the preceding user message
    let userMessageIndex = -1;
    for (let i = assistantIndex - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        userMessageIndex = i;
        break;
      }
    }

    if (userMessageIndex === -1) {
      toast.error('No user message found to regenerate from');
      return;
    }

    const userMessage = messages[userMessageIndex];
    const prompt = userMessage.content;

    // Truncate messages: keep everything up to and including the user message, then add empty assistant
    setMessages((prev) => [
      ...prev.slice(0, userMessageIndex + 1),
      { role: 'assistant', content: '' },
    ]);

    setStatus('connecting');
    setIsStreaming(true);
    isStreamingRef.current = true;
    setLastError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      // Build request body with optional rag_filters
      const requestBody: any = {
        session: sessionName,
        prompt: prompt,
        model: selectedModel || undefined,
        rag_enabled: ragEnabled,
      };

      // Add rag_filters if any filters are set
      if (ragEnabled && (ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to)) {
        requestBody.rag_filters = ragFilters;
      }

      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
        signal: controller.signal,
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      if (!res.body) {
        throw new Error('No response body (streaming not supported)');
      }

      setStatus('streaming');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      let buffer = '';
      let ragChunks: RagChunk[] | undefined = undefined;

      // SSE parser: processes Server-Sent Events from the stream
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE events (events end with \n\n)
        const events = buffer.split('\n\n');
        // Keep the last incomplete event in the buffer
        buffer = events.pop() || '';

        for (const eventText of events) {
          if (!eventText.trim()) continue;

          // Parse SSE event format
          const lines = eventText.split('\n');
          let eventType = 'message'; // default event type
          let eventData = '';

          for (const line of lines) {
            if (line.startsWith('event:')) {
              eventType = line.substring(6).trim();
            } else if (line.startsWith('data:')) {
              eventData = line.substring(5).trim();
            }
            // id: and comment lines are ignored for now
          }

          // Handle different event types
          if (eventType === 'heartbeat') {
            // Heartbeat received, connection is alive
            continue;
          } else if (eventType === 'metadata') {
            // Parse metadata (session, model, timestamp)
            try {
              const metadata = JSON.parse(eventData);
              // Metadata event can be used for UI indicators if needed
              // For now, just log it for debugging
              console.debug('Stream metadata:', metadata);
            } catch (e) {
              console.error('Failed to parse metadata:', e);
            }
          } else if (eventType === 'rag_context' || eventType === 'rag_metadata') {
            // Parse RAG context/metadata (support both old and new names)
            try {
              const ragData = JSON.parse(eventData);
              if (ragData.type === 'rag_metadata' && Array.isArray(ragData.chunks)) {
                ragChunks = ragData.chunks;
                // Update message with RAG chunks
                setMessages((prev) => {
                  if (prev.length === 0) return prev;
                  const next = [...prev];
                  const last = next[next.length - 1];
                  if (last?.role === 'assistant') {
                    next[next.length - 1] = { ...last, ragChunks: ragChunks };
                  }
                  return next;
                });
              }
            } catch (e) {
              console.error('Failed to parse RAG context:', e);
            }
          } else if (eventType === 'text' || eventType === 'message') {
            // Parse text content (support both new 'text' and legacy 'message')
            try {
              const messageData = JSON.parse(eventData);
              const content = messageData.content || '';
              // Note: messageData.tokens and messageData.elapsed are available but not displayed yet

              // Append content to the last assistant message
              setMessages((prev) => {
                if (prev.length === 0) return prev;
                const next = [...prev];
                const last = next[next.length - 1];
                if (last?.role === 'assistant') {
                  next[next.length - 1] = { ...last, content: (last.content ?? '') + content };
                }
                return next;
              });
            } catch (e) {
              console.error('Failed to parse text data:', e);
            }
          } else if (eventType === 'error') {
            // Handle error event
            try {
              const errorData = JSON.parse(eventData);
              console.error('Stream error:', errorData);
              toast.error(`Error: ${errorData.error}`);
            } catch (e) {
              console.error('Failed to parse error data:', e);
            }
            break;
          } else if (eventType === 'retry') {
            // Handle retry status
            try {
              const retryData = JSON.parse(eventData);
              console.debug('Connection retry:', retryData);
              // Could show a toast notification for retries if desired
            } catch (e) {
              console.error('Failed to parse retry data:', e);
            }
          } else if (eventType === 'done') {
            // Stream completed
            try {
              const doneData = JSON.parse(eventData);
              // doneData contains total_tokens and elapsed time
              console.debug('Stream completed:', doneData);
            } catch (e) {
              // Ignore parse errors for done event
            }
            break;
          }
        }
      }

      onSessionUpdated?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLastError(msg);
      setStatus('error');

      setMessages((prev) => {
        if (prev.length === 0) {
          return [{ role: 'assistant', content: `[error] ${msg}` }];
        }
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === 'assistant') {
          next[next.length - 1] = { ...last, content: (last.content ?? '') + `\n\n[error] ${msg}` };
          return next;
        }
        return [...next, { role: 'assistant', content: `\n\n[error] ${msg}` }];
      });
    } finally {
      setIsStreaming(false);
      isStreamingRef.current = false;
      if (status !== 'error') setStatus('idle');
      abortRef.current = null;
    }
  }

  // Memoized message renderer for error boundary fallback
  const renderMessageItem = useCallback(
    (idx: number, m: ChatMessage) => {
      const isUser = m.role === 'user';
      const isEditing = editingIndex === idx;

      return (
        <div
          data-message-index={idx}
          className="message-bubble"
          style={{
            padding: '8px 12px',
            marginBottom: 16,
            display: 'flex',
            flexDirection: 'column',
            alignItems: isUser ? 'flex-end' : 'flex-start',
            transition: 'all 0.3s ease',
          }}
        >
          <div
            style={{
              maxWidth: isUser ? '80%' : '100%',
              padding: isUser ? '12px 16px' : '8px 0',
              borderRadius: isUser ? 18 : 0,
              background: isUser
                ? (highlightedMessageIndex === idx ? 'var(--highlight)' : '#FDDCC8')
                : (highlightedMessageIndex === idx ? 'var(--highlight)' : 'transparent'),
              color: isUser ? '#1a1a1a' : 'var(--foreground)',
            }}
          >
            {/* Show RAG citations if available (streaming) or persisted (loaded from session) */}
            {((m.ragChunks && m.ragChunks.length > 0) || (m.rag_chunks && m.rag_chunks.length > 0)) && (
              <RagCitationsCollapsible
                sessionName={sessionName}
                messageIndex={idx}
                initialChunks={m.ragChunks || m.rag_chunks || ragChunksCache.get(idx)}
                onChunksLoaded={(chunks) => {
                  setRagChunksCache((prev) => {
                    const next = new Map(prev);
                    next.set(idx, chunks);
                    return next;
                  });
                }}
              />
            )}

            <div style={{ whiteSpace: 'pre-wrap' }}>
              {m.role === 'assistant' && !m.content && status === 'connecting' ? (
                <span style={{ opacity: 0.5 }}>⋯</span>
              ) : (
                m.content
              )}
            </div>
          </div>

          {/* Action buttons below user message */}
          {isUser && !isStreaming && !isEditing && (
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
              {/* Copy button */}
              <button
                className="edit-icon"
                onClick={() => {
                  navigator.clipboard.writeText(m.content);
                  toast.success('Copied to clipboard');
                }}
                title="Copy message"
                style={{
                  padding: 4,
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  opacity: 0,
                  transition: 'opacity 0.2s',
                  color: '#666',
                }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
              </button>
              {/* Edit button */}
              <button
                className="edit-icon"
                onClick={() => handleEditMessage(idx)}
                title="Edit message"
                style={{
                  padding: 4,
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  opacity: 0,
                  transition: 'opacity 0.2s',
                  color: '#666',
                }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                </svg>
              </button>
            </div>
          )}

          {/* Action buttons below assistant message */}
          {!isUser && !isStreaming && m.content && (
            <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
              {/* Copy button */}
              <button
                onClick={() => {
                  navigator.clipboard.writeText(m.content);
                  toast.success('Copied to clipboard');
                }}
                title="Copy response"
                style={{
                  padding: 4,
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  opacity: 0.5,
                  transition: 'opacity 0.2s',
                  color: '#666',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; }}
                onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.5'; }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
              </button>
              {/* Regenerate button */}
              <button
                onClick={() => handleRegenerate(idx)}
                title="Regenerate response"
                style={{
                  padding: 4,
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  opacity: 0.5,
                  transition: 'opacity 0.2s',
                  color: '#666',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; }}
                onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.5'; }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 2v6h-6" />
                  <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
                  <path d="M3 22v-6h6" />
                  <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
                </svg>
              </button>
            </div>
          )}
        </div>
      );
    },
    [
      highlightedMessageIndex,
      sessionName,
      ragChunksCache,
      status,
      editingIndex,
      isStreaming,
      toast,
    ]
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Model selector - ChatGPT style */}
      <div style={{ position: 'relative', marginBottom: 8 }}>
        <button
          onClick={() => !isStreaming && setShowModelDropdown(!showModelDropdown)}
          disabled={isStreaming}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: '8px 12px',
            background: 'var(--background)',
            border: '1px solid var(--border)',
            borderRadius: 20,
            fontSize: 14,
            fontWeight: 500,
            cursor: isStreaming ? 'not-allowed' : 'pointer',
            color: 'var(--foreground)',
            transition: 'background 0.2s ease',
          }}
          onMouseEnter={(e) => {
            if (!isStreaming) e.currentTarget.style.background = 'var(--button-hover)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'var(--background)';
          }}
        >
          <span style={{ fontWeight: 600 }}>nyxGPT</span>
          <span style={{ opacity: 0.6 }}>{releaseVersion || ''}</span>
          <span style={{ opacity: 0.5, fontSize: 12, marginLeft: 2 }}>›</span>
        </button>

        {showModelDropdown && (
          <>
            {/* Backdrop to close dropdown */}
            <div
              style={{
                position: 'fixed',
                top: 0,
                left: 0,
                right: 0,
                bottom: 0,
                zIndex: 999,
              }}
              onClick={() => setShowModelDropdown(false)}
            />
            {/* Dropdown menu */}
            <div
              style={{
                position: 'absolute',
                top: '100%',
                left: 0,
                marginTop: 4,
                background: 'var(--sidebar-bg)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
                zIndex: 1000,
                minWidth: 200,
                maxHeight: 300,
                overflowY: 'auto',
              }}
            >
              {availableModels.length === 0 ? (
                <div style={{ padding: '12px 16px', fontSize: 14, opacity: 0.6 }}>
                  Loading models...
                </div>
              ) : (
                availableModels.map((model) => (
                  <button
                    key={model}
                    onClick={() => {
                      setSelectedModel(model);
                      setShowModelDropdown(false);
                    }}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      width: '100%',
                      padding: '10px 16px',
                      background: model === selectedModel ? 'var(--button-hover)' : 'transparent',
                      border: 'none',
                      fontSize: 14,
                      cursor: 'pointer',
                      color: 'var(--foreground)',
                      textAlign: 'left',
                    }}
                    onMouseEnter={(e) => {
                      if (model !== selectedModel) e.currentTarget.style.background = 'var(--button-hover)';
                    }}
                    onMouseLeave={(e) => {
                      if (model !== selectedModel) e.currentTarget.style.background = 'transparent';
                    }}
                  >
                    {model}
                    {model === selectedModel && (
                      <span style={{ marginLeft: 'auto', opacity: 0.6 }}>✓</span>
                    )}
                  </button>
                ))
              )}
            </div>
          </>
        )}
      </div>

      <div
        style={{
          flex: 1,
          marginTop: 12,
          border: '1px solid var(--border-light)',
          borderRadius: 10,
          background: 'var(--chat-bg)',
          overflow: 'hidden',
        }}
      >
        {messages.length === 0 ? (
          <div style={{ padding: 12, opacity: 0.7 }}>Send a message to start…</div>
        ) : (
          <VirtuosoErrorBoundary
            sessionName={sessionName}
            messages={messages}
            itemContent={renderMessageItem}
          >
            <Virtuoso
              ref={virtuosoRef}
              data={messages}
              style={{ height: '100%' }}
              firstItemIndex={firstItemIndex}
              initialTopMostItemIndex={messages.length - 1}
              defaultItemHeight={100}
              followOutput={() => (isAtBottomRef.current ? 'smooth' : false)}
              atBottomStateChange={(atBottom) => {
                isAtBottomRef.current = atBottom;
              }}
              startReached={loadOlderMessages}
              itemContent={(idx, m) => renderMessageItem(idx, m)}
              components={{
                Header: isLoadingMore
                  ? () => (
                      <div
                        style={{
                          padding: '8px',
                          textAlign: 'center',
                          opacity: 0.7,
                          fontSize: 12,
                        }}
                      >
                        Loading older messages...
                      </div>
                    )
                  : undefined,
              }}
            />
          </VirtuosoErrorBoundary>
        )}
      </div>

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".txt,.md,.json,.pdf,.docx,.pptx,.epub,.html,.htm"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void uploadFile(file);
          e.target.value = ''; // Reset so same file can be selected again
        }}
        style={{ display: 'none' }}
      />

      {/* RAG Filters Panel */}
      {showRagFilters && ragEnabled && (
        <div
          style={{
            marginTop: 12,
            padding: 12,
            border: '1px solid var(--border)',
            borderRadius: 10,
            background: 'var(--input-bg)',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>RAG Document Filters</h3>
            <button
              onClick={() => setRagFilters({})}
              style={{
                padding: '4px 8px',
                fontSize: 12,
                border: '1px solid var(--border)',
                borderRadius: 4,
                background: 'transparent',
                color: 'var(--foreground)',
                cursor: 'pointer',
              }}
            >
              Clear All
            </button>
          </div>

          {/* Document selection */}
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
              Select Documents
            </label>
            <div style={{ maxHeight: 150, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 6, padding: 8 }}>
              {availableDocuments.length === 0 ? (
                <div style={{ fontSize: 12, opacity: 0.6, textAlign: 'center', padding: 8 }}>
                  No documents available
                </div>
              ) : (
                availableDocuments.map((doc) => (
                  <label
                    key={doc.doc_id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      padding: '6px 8px',
                      cursor: 'pointer',
                      borderRadius: 4,
                      fontSize: 12,
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--button-hover)'; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                  >
                    <input
                      type="checkbox"
                      checked={ragFilters.doc_ids?.includes(doc.doc_id) || false}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        setRagFilters((prev) => {
                          const currentDocIds = prev.doc_ids || [];
                          if (checked) {
                            return { ...prev, doc_ids: [...currentDocIds, doc.doc_id] };
                          } else {
                            return { ...prev, doc_ids: currentDocIds.filter((id) => id !== doc.doc_id) };
                          }
                        });
                      }}
                      style={{ marginRight: 8 }}
                    />
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 500 }}>{doc.filename || doc.doc_id}</div>
                      <div style={{ fontSize: 11, opacity: 0.7 }}>
                        {doc.chunks} chunks
                        {doc.tags && doc.tags.length > 0 && ` • ${doc.tags.join(', ')}`}
                      </div>
                    </div>
                  </label>
                ))
              )}
            </div>
          </div>

          {/* Filename filter */}
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
              Filename Search
            </label>
            <input
              type="text"
              value={ragFilters.filename || ''}
              onChange={(e) => setRagFilters((prev) => ({ ...prev, filename: e.target.value || undefined }))}
              placeholder="Filter by filename..."
              style={{
                width: '100%',
                padding: '6px 10px',
                border: '1px solid var(--border)',
                borderRadius: 6,
                background: 'var(--background)',
                color: 'var(--foreground)',
                fontSize: 12,
              }}
            />
          </div>

          {/* Date range filter */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                From Date
              </label>
              <input
                type="date"
                value={ragFilters.date_from || ''}
                onChange={(e) => setRagFilters((prev) => ({ ...prev, date_from: e.target.value || undefined }))}
                style={{
                  width: '100%',
                  padding: '6px 10px',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: 12,
                }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                To Date
              </label>
              <input
                type="date"
                value={ragFilters.date_to || ''}
                onChange={(e) => setRagFilters((prev) => ({ ...prev, date_to: e.target.value || undefined }))}
                style={{
                  width: '100%',
                  padding: '6px 10px',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: 12,
                }}
              />
            </div>
          </div>
        </div>
      )}

      {/* Attached Documents Panel */}
      {showAttachedDocs && (
        <div
          style={{
            marginTop: 12,
            padding: 12,
            border: '1px solid var(--border)',
            borderRadius: 10,
            background: 'var(--input-bg)',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Force-Included Documents</h3>
            <span style={{ fontSize: 12, opacity: 0.6 }}>{attachedDocIds.length} attached</span>
          </div>

          {/* Attached doc list */}
          <div style={{ maxHeight: 120, overflowY: 'auto', marginBottom: 10 }}>
            {attachedDocIds.length === 0 ? (
              <div style={{ fontSize: 12, opacity: 0.6, textAlign: 'center', padding: 8 }}>
                No documents attached
              </div>
            ) : (
              attachedDocIds.map((docId) => (
                <div
                  key={docId}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '4px 8px',
                    borderRadius: 4,
                    fontSize: 12,
                  }}
                >
                  <span style={{ fontFamily: 'monospace' }}>{docId}</span>
                  <button
                    onClick={() => void detachDocument(docId)}
                    style={{
                      padding: '2px 6px',
                      fontSize: 11,
                      border: '1px solid var(--border)',
                      borderRadius: 4,
                      background: 'transparent',
                      color: 'var(--foreground)',
                      cursor: 'pointer',
                    }}
                    title="Detach document"
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>

          {/* Attach input */}
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              value={attachDocInput}
              onChange={(e) => setAttachDocInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && attachDocInput.trim()) {
                  void attachDocument(attachDocInput.trim());
                  setAttachDocInput('');
                }
              }}
              placeholder="Enter doc_id to attach..."
              style={{
                flex: 1,
                padding: '6px 10px',
                border: '1px solid var(--border)',
                borderRadius: 6,
                background: 'var(--background)',
                color: 'var(--foreground)',
                fontSize: 12,
              }}
            />
            <button
              onClick={() => {
                if (attachDocInput.trim()) {
                  void attachDocument(attachDocInput.trim());
                  setAttachDocInput('');
                }
              }}
              style={{
                padding: '6px 12px',
                border: '1px solid var(--border)',
                borderRadius: 6,
                background: 'var(--button-hover)',
                color: 'var(--foreground)',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              Attach
            </button>
          </div>
        </div>
      )}

      {/* Message input box - two lines */}
      <div
        style={{
          marginTop: 12,
          border: '1px solid var(--border)',
          borderRadius: 10,
          background: 'var(--input-bg)',
          overflow: 'hidden',
        }}
      >
        {/* Edit header - shown when editing a message */}
        {editingIndex !== null && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '8px 12px',
              borderBottom: '1px solid var(--border)',
              background: 'var(--button-hover)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--foreground)' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
              </svg>
              <span style={{ fontSize: 13, fontWeight: 500 }}>Edit</span>
            </div>
            <button
              onClick={cancelEdit}
              style={{
                background: 'transparent',
                border: 'none',
                cursor: 'pointer',
                padding: 4,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--foreground)',
                opacity: 0.6,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.6'; }}
              title="Cancel edit"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        )}

        {/* Line 1: Text input */}
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Type your message…"
          disabled={isStreaming}
          style={{
            width: '100%',
            padding: '12px',
            border: 'none',
            outline: 'none',
            background: 'transparent',
            fontSize: 14,
            color: 'var(--foreground)',
          }}
        />

        {/* Line 2: Controls row */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '8px 12px',
            borderTop: '1px solid var(--border)',
          }}
        >
          {/* Left side: Upload button and RAG toggle */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {/* Upload menu button */}
            <div style={{ position: 'relative' }}>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setShowUploadMenu(!showUploadMenu);
                }}
                disabled={isStreaming}
                style={{
                  width: 32,
                  height: 32,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 8,
                  border: 'none',
                  background: 'transparent',
                  cursor: isStreaming ? 'not-allowed' : 'pointer',
                  color: 'var(--foreground)',
                  opacity: isStreaming ? 0.4 : 0.6,
                  transition: 'opacity 0.15s, background 0.15s',
                }}
                onMouseEnter={(e) => {
                  if (!isStreaming) {
                    e.currentTarget.style.opacity = '1';
                    e.currentTarget.style.background = 'var(--button-hover)';
                  }
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.opacity = isStreaming ? '0.4' : '0.6';
                  e.currentTarget.style.background = 'transparent';
                }}
                title="Upload file"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="5" x2="12" y2="19" />
                  <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
              </button>

              {/* Upload dropdown menu */}
              {showUploadMenu && (
                <div
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    position: 'absolute',
                    bottom: '100%',
                    left: 0,
                    marginBottom: 8,
                    background: 'var(--sidebar-bg)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
                    zIndex: 1000,
                    minWidth: 160,
                  }}
                >
                  <button
                    onClick={() => {
                      fileInputRef.current?.click();
                      setShowUploadMenu(false);
                    }}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      width: '100%',
                      padding: '10px 14px',
                      background: 'transparent',
                      border: 'none',
                      fontSize: 14,
                      cursor: 'pointer',
                      color: 'var(--foreground)',
                      textAlign: 'left',
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = 'var(--button-hover)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = 'transparent';
                    }}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                    </svg>
                    Upload file
                  </button>
                </div>
              )}
            </div>

            {/* RAG toggle */}
            <button
              onClick={() => void toggleRag()}
              disabled={isStreaming}
              style={{
                padding: '6px 10px',
                borderRadius: 6,
                border: '1px solid var(--border)',
                background: ragEnabled ? '#E45801' : 'var(--button-hover)',
                color: ragEnabled ? 'white' : 'var(--foreground)',
                cursor: isStreaming ? 'not-allowed' : 'pointer',
                fontSize: 12,
                fontWeight: 500,
              }}
            >
              RAG: {ragEnabled ? 'ON' : 'OFF'}
            </button>

            {/* RAG Filters toggle button */}
            {ragEnabled && (
              <button
                onClick={() => setShowRagFilters(!showRagFilters)}
                disabled={isStreaming}
                style={{
                  padding: '6px 10px',
                  borderRadius: 6,
                  border: '1px solid var(--border)',
                  background: showRagFilters ? 'var(--button-hover)' : 'transparent',
                  color: 'var(--foreground)',
                  cursor: isStreaming ? 'not-allowed' : 'pointer',
                  fontSize: 12,
                  fontWeight: 500,
                }}
                title="Filter RAG documents"
              >
                Filters {(ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to) ? '(active)' : ''}
              </button>
            )}

            {/* Attached Docs toggle button — always visible so docs can be managed even when RAG is off */}
            {(ragEnabled || attachedDocIds.length > 0) && (
              <button
                onClick={() => setShowAttachedDocs(!showAttachedDocs)}
                disabled={isStreaming}
                style={{
                  padding: '6px 10px',
                  borderRadius: 6,
                  border: '1px solid var(--border)',
                  background: showAttachedDocs ? 'var(--button-hover)' : 'transparent',
                  color: 'var(--foreground)',
                  cursor: isStreaming ? 'not-allowed' : 'pointer',
                  fontSize: 12,
                  fontWeight: 500,
                }}
                title="Manage force-included documents"
              >
                Docs {attachedDocIds.length > 0 ? `(${attachedDocIds.length})` : ''}
              </button>
            )}

            {ragStatus === 'uploading' && <span style={{ fontSize: 12, color: 'var(--foreground)', opacity: 0.6 }}>Uploading...</span>}
            {ragError && <span style={{ fontSize: 12, color: 'red' }}>{ragError}</span>}
          </div>

          {/* Right side: Send/Stop button */}
          {isStreaming ? (
            <button
              onClick={stop}
              title="Stop generating"
              style={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                border: 'none',
                background: '#dc2626',
                color: 'white',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          ) : (
            <button
              onClick={() => void send()}
              disabled={!input.trim()}
              title="Send message"
              style={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                border: 'none',
                background: input.trim() ? '#E45801' : 'var(--button-hover)',
                color: 'white',
                cursor: input.trim() ? 'pointer' : 'not-allowed',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
                opacity: input.trim() ? 1 : 0.5,
                transition: 'background 0.2s, opacity 0.2s',
              }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {(status === 'connecting' || status === 'streaming') && (
        <div style={{ marginTop: 8, fontSize: 12, opacity: 0.7, display: 'flex', alignItems: 'center', gap: 8 }}>
          <LoadingSpinner size="small" />
          <span>{status === 'connecting' ? 'Connecting...' : 'Streaming...'}</span>
        </div>
      )}
      {lastError && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--error)' }}>
          Error: {lastError}
        </div>
      )}
    </div>
  );
}
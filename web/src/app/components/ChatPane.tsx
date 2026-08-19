'use client';

import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Virtuoso, VirtuosoHandle } from 'react-virtuoso';
import LoadingSpinner from '../../components/LoadingSpinner';
import { extractSseEvents, safeJsonParse } from '../lib/sse';
import { isQueuedForBackgroundSync } from '../lib/backgroundSync';
import { formatVersion } from '../lib/version';
import { useToast } from '../../contexts/ToastContext';
import { useIsMobile } from '../../hooks/useIsMobile';
import { apiErrorText } from '../../lib/apiError';

// The API's error envelope (see `http_exception_handler` in src/nyxgpt/app.py)
// nests the human-readable message under `error.message`; a plain string
// `error` (from the Next.js proxy route's own catch block, e.g. on a network
// failure) or a top-level `detail` are also handled as fallbacks.
// Error Boundary for Virtuoso rendering
export class VirtuosoErrorBoundary extends Component<
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
    const telemetry = (
      globalThis as typeof globalThis & {
        telemetry?: { captureException: (e: Error, ctx: Record<string, unknown>) => void };
      }
    ).telemetry;
    if (typeof window !== 'undefined' && telemetry) {
      telemetry.captureException(error, {
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

type AttachmentFile = {
  file: File;
  preview: string; // data URL for images, empty for documents
  type: 'image' | 'document';
  media_type: string;
  data: string; // base64-encoded content
  filename: string;
};

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  ragChunks?: RagChunk[];
  rag_chunks?: RagChunk[]; // Backend uses snake_case
  attachments?: AttachmentFile[]; // Inline attachments shown in message bubble
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
  chunk_id?: number | null;  // Internal zero-based clustering key; not for display
  collection?: string | null;
  chunk_number?: number | null;  // 1-based, human-readable chunk position
  total_chunks?: number | null;
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

// Human-readable citation ref, e.g. "chunk 2 of 5". Prefers the 1-based
// chunk_number/total_chunks fields; falls back to chunk_id + 1 for
// citations persisted before those fields were tracked (the raw
// zero-based chunk_id alone reads as a chunk *count* of 0, not an index).
function formatChunkRef(chunk: RagChunk): string {
  if (chunk.chunk_number != null) {
    return chunk.total_chunks
      ? `chunk ${chunk.chunk_number} of ${chunk.total_chunks}`
      : `chunk ${chunk.chunk_number}`;
  }
  if (chunk.chunk_id !== null && chunk.chunk_id !== undefined) {
    return `chunk ${chunk.chunk_id + 1}`;
  }
  return '';
}

// The parent only mounts this component when the message already carries a
// non-empty ragChunks/rag_chunks array (which seeds `chunks`), so no lazy
// chunk fetching is needed here — only the display config is fetched.
function RagCitationsCollapsible({ initialChunks }: { initialChunks: RagChunk[] }) {
  const [expanded, setExpanded] = useState(false);
  const [chunks] = useState<RagChunk[]>(initialChunks);
  const [ragConfig, setRagConfig] = useState<RagConfig | null>(null);
  const [expandedChunks, setExpandedChunks] = useState<Set<number>>(new Set());

  const handleToggle = async () => {
    const newExpanded = !expanded;
    setExpanded(newExpanded);

    // Fetch config if not yet loaded (retry on each expand if missing)
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

  const chunkCount = chunks.length;

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
          {`${chunkCount} RAG ${chunkCount === 1 ? 'source' : 'sources'} retrieved`}
        </span>
      </button>

      {expanded && (
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
                    {formatChunkRef(chunk) && ` (${formatChunkRef(chunk)})`}
                    {chunk.collection && chunk.collection !== 'default' && ` · ${chunk.collection}`}
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

    </div>
  );
}

// Body of POST /api/chat/stream. Both send paths build the same request;
// naming the shape is what lets `rag_filters` and `attachments` be attached
// conditionally without the object being `any`.
type RagFilters = {
  doc_ids?: string[];
  filename?: string;
  tags?: string[];
  date_from?: string;
  date_to?: string;
  collection?: string;
};

type ChatAttachment = {
  type: string;
  media_type: string;
  data: string;
  filename: string;
};

type ChatStreamRequest = {
  session: string;
  prompt: string;
  model?: string;
  rag_enabled: boolean;
  rag_filters?: RagFilters;
  attachments?: ChatAttachment[];
};

export default function ChatPane({ sessionName, onSessionUpdated, scrollToMessageIndex, releaseVersion }: Props) {
  const toast = useToast();
  const isMobile = useIsMobile();
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
  const [ragError, setRagError] = useState<string | null>(null);

  // RAG filters state
  const [showRagFilters, setShowRagFilters] = useState<boolean>(false);
  const [ragFilters, setRagFilters] = useState<RagFilters>({});
  const [availableDocuments, setAvailableDocuments] = useState<Array<{
    doc_id: string;
    filename: string | null;
    chunks: number;
    tags: string[] | null;
    ingested_at: string | null;
  }>>([]);
  // Collections available for scoping RAG queries and chat-uploaded documents.
  // The same `ragFilters.collection` selection is used for both search
  // scoping and as the ingest target for files uploaded via chat, so there
  // is one clearly-labeled "current collection" for the whole chat surface.
  const [availableCollections, setAvailableCollections] = useState<Array<{
    name: string;
    doc_count: number;
    chunk_count: number;
  }>>([]);
  const [uploadingToCollection, setUploadingToCollection] = useState<boolean>(false);
  const collectionUploadInputRef = useRef<HTMLInputElement>(null);

  // Attached documents state (force-include for RAG)
  const [attachedDocIds, setAttachedDocIds] = useState<string[]>([]);
  const [showAttachedDocs, setShowAttachedDocs] = useState<boolean>(false);
  const [attachDocInput, setAttachDocInput] = useState<string>('');

  // Inline attachment state
  const [pendingAttachments, setPendingAttachments] = useState<AttachmentFile[]>([]);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState<boolean>(false);

  // Rename state
  const [sessionTitle, setSessionTitle] = useState<string>('');

  // Edit state
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Pagination state
  const [totalMessages, setTotalMessages] = useState<number>(0);
  const [loadedOffset, setLoadedOffset] = useState<number>(0);
  const [isLoadingMore, setIsLoadingMore] = useState<boolean>(false);
  const [hasMore, setHasMore] = useState<boolean>(true);
  const [firstItemIndex, setFirstItemIndex] = useState<number>(0);
  const PAGE_SIZE = 50;

  // Fetch available models on mount, whenever the model dropdown is opened, and
  // whenever the tab regains focus, so a model pulled from the Manage Models
  // page shows up here without requiring a manual reload.
  const fetchModels = useCallback(async () => {
    try {
      const res = await fetch('/api/models');
      if (res.ok) {
        const data = await res.json();
        const models = data.models || [];
        setAvailableModels(models);
        // Set first model as default if none selected
        setSelectedModel((prev) => (prev ? prev : models[0] || ''));
      } else {
        console.error('Failed to fetch models:', res.status, res.statusText);
      }
    } catch (err) {
      console.error('Failed to fetch models:', err);
    }
  }, []);

  useEffect(() => {
    void fetchModels();
  }, [fetchModels]);

  useEffect(() => {
    if (showModelDropdown) {
      void fetchModels();
    }
  }, [showModelDropdown, fetchModels]);

  useEffect(() => {
    function handleFocus() {
      void fetchModels();
    }
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  }, [fetchModels]);

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

          // Fetch available documents and collections if RAG is enabled
          if (data.rag_enabled) {
            fetchAvailableDocuments();
            fetchAvailableCollections();
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
      let clearHighlightTimeout: ReturnType<typeof setTimeout> | undefined;
      // Wait a bit for messages to render
      const scrollTimeout = setTimeout(() => {
        virtuosoRef.current?.scrollToIndex({
          index: scrollToMessageIndex,
          align: 'center',
          behavior: 'smooth',
        });
        // Highlight the message
        setHighlightedMessageIndex(scrollToMessageIndex);
        // Remove highlight after 2 seconds
        clearHighlightTimeout = setTimeout(() => {
          setHighlightedMessageIndex(null);
        }, 2000);
      }, 100);
      return () => {
        clearTimeout(scrollTimeout);
        clearTimeout(clearHighlightTimeout);
      };
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

  // Load older messages when scrolling to top (Critical Issue 2: Use Virtuoso's startReached)
  const loadOlderMessages = useCallback(async () => {
    if (isLoadingMore || !hasMore) return;

    // hasMore is only true while loadedOffset > 0 (both the initial load and
    // every older-page load set hasMore = offset > 0), so newOffset always
    // differs from loadedOffset here.
    const newOffset = Math.max(0, loadedOffset - PAGE_SIZE);

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

  async function fetchAvailableDocuments(collection?: string) {
    try {
      const targetCollection = collection ?? ragFilters.collection ?? 'default';
      const res = await fetch(
        `/api/v1/rag/documents?collection=${encodeURIComponent(targetCollection)}`
      );
      if (res.ok) {
        const data = await res.json();
        setAvailableDocuments(data.documents || []);
      }
    } catch (err) {
      console.error('Failed to fetch available documents:', err);
    }
  }

  // Re-scope the "Select Documents" list to whichever collection is chosen in
  // the RAG filter dropdown — otherwise it keeps showing the collection that
  // happened to be selected (or 'default') when it was first fetched.
  useEffect(() => {
    if (ragEnabled) {
      void fetchAvailableDocuments(ragFilters.collection || 'default');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ragFilters.collection, ragEnabled]);

  // Filename Search filters the "Select Documents" list client-side against
  // whatever the selected collection returned -- live-as-you-type, no Enter
  // or submit button required. Matches on filename (falling back to doc_id
  // for documents with no filename metadata), case-insensitively.
  const filenameQuery = ragFilters.filename?.trim().toLowerCase() ?? '';
  const visibleDocuments = useMemo(() => {
    if (!filenameQuery) return availableDocuments;
    return availableDocuments.filter((doc) =>
      (doc.filename || doc.doc_id).toLowerCase().includes(filenameQuery)
    );
  }, [availableDocuments, filenameQuery]);

  async function fetchAvailableCollections() {
    try {
      const res = await fetch('/api/v1/rag/collections');
      if (res.ok) {
        const data = await res.json();
        setAvailableCollections(data.collections || []);
      }
    } catch (err) {
      console.error('Failed to fetch available collections:', err);
    }
  }

  // Uploads a document straight into the currently-selected RAG collection
  // (`ragFilters.collection`, defaulting to "default"), persisting it for
  // future retrieval — distinct from the paperclip attachment flow above,
  // which only inlines a file into a single outgoing message.
  async function uploadDocumentToCollection(file: File): Promise<void> {
    const targetCollection = ragFilters.collection;
    setUploadingToCollection(true);
    setRagError(null);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const query = targetCollection ? `?collection=${encodeURIComponent(targetCollection)}` : '';
      const res = await fetch(`/api/rag/upload${query}`, { method: 'POST', body: formData });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(apiErrorText(errorData, `HTTP ${res.status}`));
      }

      const data = await res.json();
      await Promise.all([fetchAvailableCollections(), fetchAvailableDocuments()]);
      toast.success(
        `Uploaded '${data.doc_id}' into '${data.collection || targetCollection || 'default'}': ${data.chunks_ingested} chunk(s) ingested.`
      );
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to upload document to collection: ${msg}`);
    } finally {
      setUploadingToCollection(false);
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
      if (isQueuedForBackgroundSync(err)) {
        // Request was queued by the service worker for replay once back
        // online; the document will actually be detached once it fires.
        setAttachedDocIds((prev) => prev.filter((id) => id !== docId));
        toast.info('You are offline — document removal will complete when you reconnect');
        return;
      }
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

      // Fetch available documents and collections when enabling RAG
      if (newRagEnabled) {
        fetchAvailableDocuments();
        fetchAvailableCollections();
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setRagError(msg);
    }
  }

  async function processFilesAsAttachments(files: FileList | File[]): Promise<void> {
    const fileArray = Array.from(files);
    const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
    const DOC_TYPES = [
      'application/pdf',
      'text/plain',
      'text/html',
      'text/markdown',
      'text/x-markdown',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      'application/epub+zip',
    ];
    const allowed = [...IMAGE_TYPES, ...DOC_TYPES];

    const newAttachments: AttachmentFile[] = [];

    for (const file of fileArray) {
      if (!allowed.includes(file.type)) {
        toast.error(`Unsupported file type: ${file.name} (${file.type})`);
        continue;
      }

      try {
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const result = reader.result as string;
            // Strip data URL prefix to get raw base64
            const base64Data = result.split(',')[1] ?? '';
            resolve(base64Data);
          };
          reader.onerror = () => reject(new Error('Failed to read file'));
          reader.readAsDataURL(file);
        });

        const isImage = IMAGE_TYPES.includes(file.type);
        const preview = isImage
          ? await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result as string);
              reader.readAsDataURL(file);
            })
          : '';

        newAttachments.push({
          file,
          preview,
          type: isImage ? 'image' : 'document',
          media_type: file.type,
          data: base64,
          filename: file.name,
        });
      } catch {
        toast.error(`Failed to process file: ${file.name}`);
      }
    }

    if (newAttachments.length > 0) {
      setPendingAttachments((prev) => [...prev, ...newAttachments]);
    }
  }

  async function send() {
    const text = input.trim();
    if ((!text && pendingAttachments.length === 0) || isStreamingRef.current) return;

    // Clear edit state if we were editing
    if (editingIndex !== null) {
      setEditingIndex(null);
    }

    // Check if this is the first message (for auto-titling)
    const isFirstMessage = messages.length === 0;

    // Snapshot attachments before clearing
    const attachmentsSnapshot = [...pendingAttachments];

    // Optimistic append user message (with attachments)
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text, attachments: attachmentsSnapshot.length > 0 ? attachmentsSnapshot : undefined },
      { role: 'assistant', content: '' },
    ]);
    setStatus('connecting');
    setInput('');
    setPendingAttachments([]);
    setIsStreaming(true);
    isStreamingRef.current = true;
    setLastError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      // Build request body with optional rag_filters and attachments
      const requestBody: ChatStreamRequest = {
        session: sessionName,
        prompt: text || ' ',
        model: selectedModel || undefined,
        rag_enabled: ragEnabled,
      };

      // Add rag_filters if any filters are set
      if (ragEnabled && (ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to || ragFilters.collection)) {
        requestBody.rag_filters = ragFilters;
      }

      // Add attachments if any
      if (attachmentsSnapshot.length > 0) {
        requestBody.attachments = attachmentsSnapshot.map((a) => ({
          type: a.type,
          media_type: a.media_type,
          data: a.data,
          filename: a.filename,
        }));
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

        // Process complete SSE events; framing, comment/keep-alive frames,
        // multi-line data fields, and CRLF handled by the shared parser (#3112)
        const { events, rest } = extractSseEvents(buffer);
        buffer = rest;

        for (const { event: eventType, data: eventData } of events) {

          // Handle different event types
          if (eventType === 'heartbeat') {
            // Heartbeat received, connection is alive
            continue;
          } else if (eventType === 'metadata') {
            // Parse metadata (session, model, timestamp)
            const metadata = safeJsonParse(eventData);
            if (metadata) {
              // Metadata event can be used for UI indicators if needed
              console.debug('Stream metadata:', metadata);
            } else if (eventData) {
              console.error('Failed to parse metadata:', eventData);
            }
          } else if (eventType === 'rag_context' || eventType === 'rag_metadata') {
            // Parse RAG context/metadata (support both old and new names)
            const ragData = safeJsonParse<{ type?: string; chunks?: RagChunk[] }>(eventData);
            if (!ragData) {
              if (eventData) console.error('Failed to parse RAG context:', eventData);
            } else {
              if (ragData.type === 'rag_metadata' && Array.isArray(ragData.chunks)) {
                ragChunks = ragData.chunks;
                // Update message with RAG chunks
                setMessages((prev) => {
                  const next = [...prev];
                  // The pre-appended assistant bubble is always last.
                  next[next.length - 1] = { ...next[next.length - 1], ragChunks: ragChunks };
                  return next;
                });
              }
            }
          } else if (eventType === 'text' || eventType === 'message') {
            // Parse text content (support both new 'text' and legacy 'message')
            const messageData = safeJsonParse<{ content?: string }>(eventData);
            if (!messageData) {
              // Empty or fragmented payloads are skipped instead of crashing (#3112)
              if (eventData) console.error('Failed to parse text data:', eventData);
            } else {
              const content = messageData.content || '';
              // Note: messageData.tokens and messageData.elapsed are available but not displayed yet

              // Append content to the last assistant message
              setMessages((prev) => {
                const next = [...prev];
                const last = next[next.length - 1];
                // The pre-appended assistant bubble (content: '') is always last.
                next[next.length - 1] = { ...last, content: last.content + content };
                return next;
              });
            }
          } else if (eventType === 'error') {
            // Handle error event
            const errorData = safeJsonParse<{ error?: string }>(eventData);
            if (errorData) {
              console.error('Stream error:', errorData);
              toast.error(`Error: ${errorData.error}`);
            } else {
              console.error('Stream error event with unparseable payload:', eventData);
              toast.error('Streaming error');
            }
            break;
          } else if (eventType === 'retry') {
            // Handle retry status
            const retryData = safeJsonParse(eventData);
            if (retryData) console.debug('Connection retry:', retryData);
          } else if (eventType === 'done') {
            // Stream completed; parse errors on the done payload are ignored
            const doneData = safeJsonParse(eventData);
            if (doneData) console.debug('Stream completed:', doneData);
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
        // The empty assistant bubble is always pre-appended before streaming.
        const next = [...prev];
        const last = next[next.length - 1];
        next[next.length - 1] = { ...last, content: last.content + `\n\n[error] ${msg}` };
        return next;
      });
    } finally {
      setIsStreaming(false);
      isStreamingRef.current = false;
      setStatus((current) => (current !== 'error' ? 'idle' : current));
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
    // (No isStreaming guard needed: the regenerate buttons are not rendered
    // while a stream is in flight.)
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
      const requestBody: ChatStreamRequest = {
        session: sessionName,
        prompt: prompt,
        model: selectedModel || undefined,
        rag_enabled: ragEnabled,
      };

      // Add rag_filters if any filters are set
      if (ragEnabled && (ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to || ragFilters.collection)) {
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

        // Process complete SSE events; framing, comment/keep-alive frames,
        // multi-line data fields, and CRLF handled by the shared parser (#3112)
        const { events, rest } = extractSseEvents(buffer);
        buffer = rest;

        for (const { event: eventType, data: eventData } of events) {

          // Handle different event types
          if (eventType === 'heartbeat') {
            // Heartbeat received, connection is alive
            continue;
          } else if (eventType === 'metadata') {
            // Parse metadata (session, model, timestamp)
            const metadata = safeJsonParse(eventData);
            if (metadata) {
              // Metadata event can be used for UI indicators if needed
              console.debug('Stream metadata:', metadata);
            } else if (eventData) {
              console.error('Failed to parse metadata:', eventData);
            }
          } else if (eventType === 'rag_context' || eventType === 'rag_metadata') {
            // Parse RAG context/metadata (support both old and new names)
            const ragData = safeJsonParse<{ type?: string; chunks?: RagChunk[] }>(eventData);
            if (!ragData) {
              if (eventData) console.error('Failed to parse RAG context:', eventData);
            } else {
              if (ragData.type === 'rag_metadata' && Array.isArray(ragData.chunks)) {
                ragChunks = ragData.chunks;
                // Update message with RAG chunks
                setMessages((prev) => {
                  const next = [...prev];
                  // The pre-appended assistant bubble is always last.
                  next[next.length - 1] = { ...next[next.length - 1], ragChunks: ragChunks };
                  return next;
                });
              }
            }
          } else if (eventType === 'text' || eventType === 'message') {
            // Parse text content (support both new 'text' and legacy 'message')
            const messageData = safeJsonParse<{ content?: string }>(eventData);
            if (!messageData) {
              // Empty or fragmented payloads are skipped instead of crashing (#3112)
              if (eventData) console.error('Failed to parse text data:', eventData);
            } else {
              const content = messageData.content || '';
              // Note: messageData.tokens and messageData.elapsed are available but not displayed yet

              // Append content to the last assistant message
              setMessages((prev) => {
                const next = [...prev];
                const last = next[next.length - 1];
                // The pre-appended assistant bubble (content: '') is always last.
                next[next.length - 1] = { ...last, content: last.content + content };
                return next;
              });
            }
          } else if (eventType === 'error') {
            // Handle error event
            const errorData = safeJsonParse<{ error?: string }>(eventData);
            if (errorData) {
              console.error('Stream error:', errorData);
              toast.error(`Error: ${errorData.error}`);
            } else {
              console.error('Stream error event with unparseable payload:', eventData);
              toast.error('Streaming error');
            }
            break;
          } else if (eventType === 'retry') {
            // Handle retry status
            const retryData = safeJsonParse(eventData);
            if (retryData) console.debug('Connection retry:', retryData);
          } else if (eventType === 'done') {
            // Stream completed; parse errors on the done payload are ignored
            const doneData = safeJsonParse(eventData);
            if (doneData) console.debug('Stream completed:', doneData);
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
        // The empty assistant bubble is always pre-appended before streaming.
        const next = [...prev];
        const last = next[next.length - 1];
        next[next.length - 1] = { ...last, content: last.content + `\n\n[error] ${msg}` };
        return next;
      });
    } finally {
      setIsStreaming(false);
      isStreamingRef.current = false;
      setStatus((current) => (current !== 'error' ? 'idle' : current));
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
            {/* Show RAG citations if available (streaming) or persisted (loaded from session).
                An explicitly-empty array (as opposed to undefined) means RAG was
                on for this turn but retrieval returned zero rows -- show an
                honest "no sources" indicator so the answer is never ambiguously
                uncited (issue #3585). */}
            {(() => {
              const ragChunksForMsg = m.ragChunks ?? m.rag_chunks;
              if (!ragChunksForMsg) return null;
              if (ragChunksForMsg.length > 0) {
                return <RagCitationsCollapsible initialChunks={ragChunksForMsg} />;
              }
              return (
                <div
                  style={{
                    marginBottom: 8,
                    padding: 8,
                    background: 'var(--rag-bg)',
                    border: '1px solid var(--rag-border)',
                    borderRadius: 6,
                    fontSize: 12,
                    color: 'var(--rag-text)',
                    opacity: 0.8,
                  }}
                >
                  No RAG sources retrieved for this reply
                </div>
              );
            })()}

            {/* Inline attachment thumbnails in message bubble */}
            {isUser && m.attachments && m.attachments.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                {m.attachments.map((att, attIdx) => (
                  <div
                    key={attIdx}
                    style={{
                      width: 72,
                      height: 72,
                      borderRadius: 8,
                      overflow: 'hidden',
                      border: '1px solid rgba(0,0,0,0.15)',
                      background: 'rgba(0,0,0,0.05)',
                      flexShrink: 0,
                      cursor: att.type === 'image' ? 'pointer' : 'default',
                    }}
                    onClick={() => {
                      if (att.type === 'image' && att.preview) {
                        window.open(att.preview, '_blank');
                      }
                    }}
                    title={att.filename}
                  >
                    {att.type === 'image' && att.preview ? (
                      <img
                        src={att.preview}
                        alt={att.filename}
                        loading="lazy"
                        decoding="async"
                        style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                      />
                    ) : (
                      <div style={{
                        width: '100%',
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        padding: 4,
                      }}>
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5 }}>
                          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                          <polyline points="14 2 14 8 20 8" />
                        </svg>
                        <span style={{ fontSize: 9, textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', width: '100%', padding: '0 2px', opacity: 0.6 }}>
                          {att.filename}
                        </span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
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
                aria-label="Copy message"
                style={{
                  padding: 4,
                  width: isMobile ? 44 : undefined,
                  height: isMobile ? 44 : undefined,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  // Hover-revealed on desktop (see .message-bubble:hover .edit-icon in
                  // globals.css); touch devices have no hover, so keep it visible on mobile.
                  opacity: isMobile ? 1 : 0,
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
                aria-label="Edit message"
                style={{
                  padding: 4,
                  width: isMobile ? 44 : undefined,
                  height: isMobile ? 44 : undefined,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  opacity: isMobile ? 1 : 0,
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
                aria-label="Copy response"
                style={{
                  padding: 4,
                  width: isMobile ? 44 : undefined,
                  height: isMobile ? 44 : undefined,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
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
                aria-label="Regenerate response"
                style={{
                  padding: 4,
                  width: isMobile ? 44 : undefined,
                  height: isMobile ? 44 : undefined,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
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
      messages,
      highlightedMessageIndex,
      sessionName,
      status,
      editingIndex,
      isStreaming,
      isMobile,
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
          <span style={{ opacity: 0.6 }}>{formatVersion(releaseVersion)}</span>
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

          {/* Collection selection */}
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
              Collection
            </label>
            <select
              value={ragFilters.collection || ''}
              onChange={(e) =>
                // Clear doc_ids on collection switch: a doc checked while a
                // different collection was selected doesn't belong to the
                // new collection, so keeping it selected pins the query to
                // `collection=X AND doc_ids=[doc-not-in-X]` -> zero rows,
                // every subsequent turn, silently (issue #3585).
                setRagFilters((prev) => ({ ...prev, collection: e.target.value || undefined, doc_ids: undefined }))
              }
              style={{
                width: '100%',
                padding: '6px 10px',
                border: '1px solid var(--border)',
                borderRadius: 6,
                background: 'var(--background)',
                color: 'var(--foreground)',
                fontSize: 12,
              }}
            >
              <option value="">Default</option>
              {availableCollections
                .filter((coll) => coll.name !== 'default')
                .map((coll) => (
                  <option key={coll.name} value={coll.name}>
                    {coll.name} ({coll.doc_count} docs)
                  </option>
                ))}
            </select>
            <p style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
              Scopes RAG search to this collection.
            </p>
            <button
              onClick={() => collectionUploadInputRef.current?.click()}
              disabled={uploadingToCollection || isStreaming}
              style={{
                marginTop: 6,
                padding: '6px 10px',
                borderRadius: 6,
                border: '1px solid var(--border)',
                background: 'var(--button-hover)',
                color: 'var(--foreground)',
                cursor: uploadingToCollection || isStreaming ? 'not-allowed' : 'pointer',
                fontSize: 12,
                fontWeight: 500,
              }}
              title="Upload a document into this collection"
            >
              {uploadingToCollection
                ? 'Uploading…'
                : `Upload document to '${ragFilters.collection || 'default'}'`}
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
              ) : visibleDocuments.length === 0 ? (
                <div style={{ fontSize: 12, opacity: 0.6, textAlign: 'center', padding: 8 }}>
                  No documents match &quot;{ragFilters.filename?.trim()}&quot;
                </div>
              ) : (
                visibleDocuments.map((doc) => (
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
            <p style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
              Filters Select Documents above as you type; also scopes RAG search.
            </p>
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
            <datalist id="available-docs-list">
              {availableDocuments.map((doc) => (
                <option key={doc.doc_id} value={doc.doc_id} label={doc.filename || doc.doc_id} />
              ))}
            </datalist>
            <input
              type="text"
              list="available-docs-list"
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

      {/* Hidden inline attachment file input */}
      <input
        ref={attachmentInputRef}
        type="file"
        accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,text/plain,text/html,text/markdown,.md,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.docx,application/vnd.openxmlformats-officedocument.presentationml.presentation,.pptx,application/epub+zip,.epub"
        multiple
        onChange={(e) => {
          if (e.target.files && e.target.files.length > 0) {
            void processFilesAsAttachments(e.target.files);
          }
          e.target.value = '';
        }}
        style={{ display: 'none' }}
      />

      {/* Hidden file input for uploading a document into the selected RAG collection */}
      <input
        ref={collectionUploadInputRef}
        type="file"
        accept=".txt,.md,.json,.pdf,.docx,.pptx,.epub,.html,.htm"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            void uploadDocumentToCollection(file);
          }
          e.target.value = '';
        }}
        style={{ display: 'none' }}
      />

      {/* Message input box - two lines */}
      <div
        style={{
          marginTop: 12,
          border: isDragOver ? '2px dashed #E45801' : '1px solid var(--border)',
          borderRadius: 10,
          background: isDragOver ? 'rgba(228,88,1,0.05)' : 'var(--input-bg)',
          overflow: 'hidden',
          transition: 'border-color 0.15s, background 0.15s',
        }}
        onDragOver={(e) => { e.preventDefault(); setIsDragOver(true); }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false); }}
        onDrop={(e) => {
          e.preventDefault();
          setIsDragOver(false);
          if (e.dataTransfer.files.length > 0) {
            void processFilesAsAttachments(e.dataTransfer.files);
          }
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

        {/* Attachment thumbnail preview strip */}
        {pendingAttachments.length > 0 && (
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 8,
              padding: '8px 12px',
              borderBottom: '1px solid var(--border)',
            }}
          >
            {pendingAttachments.map((att, idx) => (
              <div
                key={idx}
                style={{
                  position: 'relative',
                  width: 64,
                  height: 64,
                  borderRadius: 8,
                  overflow: 'hidden',
                  border: '1px solid var(--border)',
                  background: 'var(--button-hover)',
                  flexShrink: 0,
                }}
              >
                {att.type === 'image' && att.preview ? (
                  <img
                    src={att.preview}
                    alt={att.filename}
                    loading="lazy"
                    decoding="async"
                    style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                  />
                ) : (
                  <div
                    style={{
                      width: '100%',
                      height: '100%',
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      justifyContent: 'center',
                      padding: 4,
                    }}
                  >
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}>
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                    </svg>
                    <span style={{ fontSize: 9, textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', width: '100%', padding: '0 2px', opacity: 0.7 }}>
                      {att.filename}
                    </span>
                  </div>
                )}
                {/* Remove button */}
                <button
                  onClick={() => setPendingAttachments((prev) => prev.filter((_, i) => i !== idx))}
                  style={{
                    position: 'absolute',
                    top: 2,
                    right: 2,
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    border: 'none',
                    background: 'rgba(0,0,0,0.6)',
                    color: 'white',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    padding: 0,
                    fontSize: 10,
                    lineHeight: 1,
                  }}
                  title={`Remove ${att.filename}`}
                >
                  ×
                </button>
              </div>
            ))}
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
          placeholder={pendingAttachments.length > 0 ? 'Add a message (optional)…' : 'Type your message…'}
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
          {/* Left side: Attach button and RAG toggle */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {/* Inline attachment (paperclip) button */}
            <button
              onClick={() => attachmentInputRef.current?.click()}
              disabled={isStreaming}
              style={{
                width: isMobile ? 44 : 32,
                height: isMobile ? 44 : 32,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderRadius: 8,
                border: pendingAttachments.length > 0 ? '1px solid #E45801' : 'none',
                background: pendingAttachments.length > 0 ? 'rgba(228,88,1,0.1)' : 'transparent',
                cursor: isStreaming ? 'not-allowed' : 'pointer',
                color: pendingAttachments.length > 0 ? '#E45801' : 'var(--foreground)',
                opacity: isStreaming ? 0.4 : 0.7,
                transition: 'opacity 0.15s, background 0.15s',
                position: 'relative',
              }}
              onMouseEnter={(e) => { if (!isStreaming) e.currentTarget.style.opacity = '1'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = isStreaming ? '0.4' : '0.7'; }}
              title="Attach image or document"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
              {pendingAttachments.length > 0 && (
                <span style={{
                  position: 'absolute',
                  top: -4,
                  right: -4,
                  background: '#E45801',
                  color: 'white',
                  borderRadius: '50%',
                  width: 14,
                  height: 14,
                  fontSize: 9,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontWeight: 700,
                }}>
                  {pendingAttachments.length}
                </span>
              )}
            </button>

            {/* RAG toggle */}
            <button
              onClick={() => void toggleRag()}
              disabled={isStreaming}
              style={{
                padding: isMobile ? '10px 14px' : '6px 10px',
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
                  padding: isMobile ? '10px 14px' : '6px 10px',
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
                Filters {(ragFilters.doc_ids?.length || ragFilters.filename || ragFilters.tags?.length || ragFilters.date_from || ragFilters.date_to) ? '(active)' : ''} · {ragFilters.collection || 'default'}
              </button>
            )}

            {/* Attached Docs toggle button — always visible so docs can be managed even when RAG is off */}
            {(ragEnabled || attachedDocIds.length > 0) && (
              <button
                onClick={() => {
                  if (!showAttachedDocs && availableDocuments.length === 0) {
                    void fetchAvailableDocuments();
                  }
                  setShowAttachedDocs(!showAttachedDocs);
                }}
                disabled={isStreaming}
                style={{
                  padding: isMobile ? '10px 14px' : '6px 10px',
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

            {ragError && <span style={{ fontSize: 12, color: 'red' }}>{ragError}</span>}
          </div>

          {/* Right side: Send/Stop button */}
          {isStreaming ? (
            <button
              onClick={stop}
              title="Stop generating"
              style={{
                width: isMobile ? 44 : 32,
                height: isMobile ? 44 : 32,
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
              disabled={!input.trim() && pendingAttachments.length === 0}
              title="Send message"
              style={{
                width: isMobile ? 44 : 32,
                height: isMobile ? 44 : 32,
                borderRadius: '50%',
                border: 'none',
                background: (input.trim() || pendingAttachments.length > 0) ? '#E45801' : 'var(--button-hover)',
                color: 'white',
                cursor: (input.trim() || pendingAttachments.length > 0) ? 'pointer' : 'not-allowed',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
                opacity: (input.trim() || pendingAttachments.length > 0) ? 1 : 0.5,
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

'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';
import { useToast } from '../../contexts/ToastContext';

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
  doc_id?: string | null;
  chunk_id?: number | null;
};

type Props = {
  sessionName: string;
  onSessionUpdated?: () => void;
  scrollToMessageIndex?: number | null;
};

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

  const handleToggle = async () => {
    const newExpanded = !expanded;
    setExpanded(newExpanded);

    // Lazy load chunks on first expand if not already loaded
    if (newExpanded && !chunks && !loading) {
      setLoading(true);
      setError(null);

      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionName)}/messages/${messageIndex}/rag`
        );

        if (!res.ok) {
          throw new Error(`Failed to load RAG chunks: ${res.status}`);
        }

        const data = await res.json();
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
          {chunks.map((chunk, idx) => (
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
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontWeight: 500, color: 'var(--rag-text)' }}>
                  Source {idx + 1}
                </span>
                <span style={{ color: 'var(--foreground)', opacity: 0.6 }}>
                  Score: {chunk.score.toFixed(3)}
                </span>
              </div>
              {chunk.doc_id && (
                <div style={{ fontSize: 11, color: 'var(--foreground)', opacity: 0.6, marginBottom: 4 }}>
                  Doc: {chunk.doc_id}
                  {chunk.chunk_id !== null && chunk.chunk_id !== undefined && ` (chunk ${chunk.chunk_id})`}
                </div>
              )}
              <div style={{ color: 'var(--foreground)', whiteSpace: 'pre-wrap' }}>
                {chunk.text.length > 200 ? chunk.text.substring(0, 200) + '...' : chunk.text}
              </div>
            </div>
          ))}
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

export default function ChatPane({ sessionName, onSessionUpdated, scrollToMessageIndex }: Props) {
  const toast = useToast();
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<'idle' | 'connecting' | 'streaming' | 'error'>('idle');
  const [lastError, setLastError] = useState<string | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const isStreamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const messageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [highlightedMessageIndex, setHighlightedMessageIndex] = useState<number | null>(null);

  // RAG state
  const [ragEnabled, setRagEnabled] = useState<boolean>(false);
  const [ragStatus, setRagStatus] = useState<'idle' | 'uploading' | 'error'>('idle');
  const [ragError, setRagError] = useState<string | null>(null);
  const [ragChunksCache, setRagChunksCache] = useState<Map<number, RagChunk[]>>(new Map());

  // Rename state
  const [sessionTitle, setSessionTitle] = useState<string>('');
  const [renaming, setRenaming] = useState<boolean>(false);

  // Edit/Regenerate state
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editContent, setEditContent] = useState<string>('');

  const title = useMemo(() => sessionTitle || `Session: ${sessionName}`, [sessionTitle, sessionName]);

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
    isStreamingRef.current = false;
    abortRef.current?.abort();
    abortRef.current = null;

    // Fetch session metadata (RAG status, title, and model)
    fetch(`/api/sessions/${encodeURIComponent(sessionName)}/metadata`)
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          setRagEnabled(data.rag_enabled || false);
          setSessionTitle(data.title || '');
          setSelectedModel(data.model || '');
          setRagError(null);
        }
      })
      .catch((err) => {
        console.error('Failed to fetch session metadata:', err);
        setRagEnabled(false); // Default to disabled if fetch fails
        setSessionTitle(''); // Default to empty title
        setSelectedModel(''); // Default to empty (will use first available model)
      });

    // Load historical messages for the session
    fetch(`/api/v1/sessions/${encodeURIComponent(sessionName)}`)
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          if (data.messages && Array.isArray(data.messages)) {
            setMessages(data.messages);
          }
        }
      })
      .catch((err) => {
        console.error('Failed to load session messages:', err);
      });
  }, [sessionName]);

  // Scroll to message when scrollToMessageIndex changes
  useEffect(() => {
    if (scrollToMessageIndex !== null && scrollToMessageIndex !== undefined) {
      // Wait a bit for messages to render
      setTimeout(() => {
        const targetRef = messageRefs.current[scrollToMessageIndex];
        if (targetRef) {
          targetRef.scrollIntoView({ behavior: 'smooth', block: 'center' });
          // Highlight the message
          setHighlightedMessageIndex(scrollToMessageIndex);
          // Remove highlight after 2 seconds
          setTimeout(() => {
            setHighlightedMessageIndex(null);
          }, 2000);
        }
      }, 100);
    }
  }, [scrollToMessageIndex]);

  async function toggleRag() {
    try {
      const endpoint = ragEnabled ? 'disable' : 'enable';
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/rag/${endpoint}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error(`Failed to ${endpoint} RAG`);
      setRagEnabled(!ragEnabled);
      setRagError(null);
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
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session: sessionName,
          prompt: text,
          model: selectedModel || undefined,
          rag_enabled: ragEnabled,
        }),
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

      let accumulatedText = '';
      let ragChunks: RagChunk[] | undefined = undefined;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        accumulatedText += chunk;

        // Check for RAG metadata in accumulated text
        if (!ragChunks && accumulatedText.includes('__RAG_START__')) {
          const ragEndIndex = accumulatedText.indexOf('__RAG_END__');
          if (ragEndIndex !== -1) {
            const ragStartIndex = accumulatedText.indexOf('__RAG_START__') + '__RAG_START__'.length;
            const ragJson = accumulatedText.substring(ragStartIndex, ragEndIndex);
            try {
              const ragData = JSON.parse(ragJson);
              if (ragData.type === 'rag_metadata' && Array.isArray(ragData.chunks)) {
                ragChunks = ragData.chunks;
              }
            } catch (e) {
              console.error('Failed to parse RAG metadata:', e);
            }
            // Remove RAG metadata from accumulated text
            accumulatedText = accumulatedText.substring(ragEndIndex + '__RAG_END__'.length);
            // Update message with RAG chunks
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === 'assistant') {
                next[next.length - 1] = { ...last, ragChunks: ragChunks, content: accumulatedText.trimStart() };
              }
              return next;
            });
            continue;
          }
        }

        // Append chunk to last assistant message (if not waiting for RAG end marker)
        if (!accumulatedText.includes('__RAG_START__') || ragChunks !== undefined) {
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === 'assistant') {
              next[next.length - 1] = { ...last, content: (last.content ?? '') + chunk };
            }
            return next;
          });
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

  async function handleEditMessage(index: number) {
    const message = messages[index];
    setEditingIndex(index);
    setEditContent(message.content);
  }

  async function saveEdit(index: number) {
    if (!editContent.trim()) {
      toast.error('Edit content cannot be empty');
      return;
    }

    try {
      const res = await fetch(`/api/v1/sessions/${sessionName}/messages/${index}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editContent, fork: true }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({ detail: 'Edit failed' }));
        throw new Error(errData.detail || 'Edit failed');
      }

      // Reload session to get updated messages
      setEditingIndex(null);
      setEditContent('');
      toast.success('Message edited');
      onSessionUpdated?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to edit message: ${msg}`);
    }
  }

  function cancelEdit() {
    setEditingIndex(null);
    setEditContent('');
  }

  async function handleRegenerateResponse(index: number) {
    const message = messages[index];
    if (message.role !== 'user') {
      toast.error('Can only regenerate from user messages');
      return;
    }

    try {
      setIsStreaming(true);
      setStatus('connecting');

      const res = await fetch(`/api/v1/sessions/${sessionName}/messages/${index}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: selectedModel }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({ detail: 'Regenerate failed' }));
        throw new Error(errData.detail || 'Regenerate failed');
      }

      const data = await res.json();

      // Reload session to get the new response
      toast.success('Response regenerated');
      onSessionUpdated?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to regenerate: ${msg}`);
      setStatus('error');
    } finally {
      setIsStreaming(false);
      setStatus('idle');
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <div style={{ marginTop: 4, fontSize: 12, opacity: 0.7, display: 'flex', alignItems: 'center', gap: 8 }}>
            {(status === 'connecting' || status === 'streaming') && <LoadingSpinner size="small" />}
            <span>Status: <strong>{status}</strong></span>
            {lastError ? <span style={{ marginLeft: 8, color: 'red' }}>({lastError})</span> : null}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {isStreaming && (
            <button onClick={stop} style={{ padding: '6px 10px', cursor: 'pointer' }}>
              Stop
            </button>
          )}
        </div>
      </div>

      {/* Model selector */}
      <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label htmlFor="model-select" style={{ fontSize: 14, fontWeight: 500 }}>
          Model:
        </label>
        <select
          id="model-select"
          value={selectedModel}
          onChange={(e) => setSelectedModel(e.target.value)}
          disabled={isStreaming}
          style={{
            padding: '6px 10px',
            borderRadius: 6,
            border: '1px solid var(--border)',
            fontSize: 14,
            cursor: isStreaming ? 'not-allowed' : 'pointer',
            background: isStreaming ? 'var(--button-hover)' : 'var(--input-bg)',
          }}
        >
          {availableModels.length === 0 ? (
            <option value="">Loading models...</option>
          ) : (
            availableModels.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))
          )}
        </select>
      </div>

      <div
        style={{
          flex: 1,
          marginTop: 12,
          padding: 12,
          border: '1px solid var(--border-light)',
          borderRadius: 10,
          overflowY: 'auto',
          whiteSpace: 'pre-wrap',
          background: 'var(--chat-bg)',
        }}
      >
        {messages.length === 0 ? (
          <div style={{ opacity: 0.7 }}>Send a message to start…</div>
        ) : (
          messages.map((m, idx) => (
            <div
              key={idx}
              ref={(el) => { messageRefs.current[idx] = el; }}
              data-message-index={idx}
              style={{
                marginBottom: 12,
                padding: highlightedMessageIndex === idx ? 12 : 0,
                background: highlightedMessageIndex === idx ? 'var(--highlight)' : 'transparent',
                borderRadius: highlightedMessageIndex === idx ? 8 : 0,
                transition: 'all 0.3s ease',
              }}
            >
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
                <strong>{m.role}</strong>
                {m.edited_at && <span style={{ fontSize: 10, opacity: 0.6 }}>(edited)</span>}
              </div>
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
              {editingIndex === idx ? (
                <div>
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    style={{
                      width: '100%',
                      minHeight: 80,
                      padding: 8,
                      borderRadius: 6,
                      border: '1px solid var(--border)',
                      background: 'var(--input-bg)',
                      color: 'var(--foreground)',
                      fontFamily: 'inherit',
                      fontSize: 14,
                      resize: 'vertical',
                    }}
                  />
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button
                      onClick={() => saveEdit(idx)}
                      style={{
                        padding: '4px 8px',
                        fontSize: 12,
                        borderRadius: 4,
                        border: '1px solid var(--border)',
                        background: 'var(--success)',
                        color: 'white',
                        cursor: 'pointer',
                      }}
                    >
                      Save
                    </button>
                    <button
                      onClick={cancelEdit}
                      style={{
                        padding: '4px 8px',
                        fontSize: 12,
                        borderRadius: 4,
                        border: '1px solid var(--border)',
                        background: 'var(--button-hover)',
                        color: 'var(--foreground)',
                        cursor: 'pointer',
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div>
                  <div>
                    {m.role === 'assistant' && !m.content && status === 'connecting' ? (
                      <span style={{ opacity: 0.5 }}>⋯</span>
                    ) : (
                      m.content
                    )}
                  </div>
                  {!isStreaming && (
                    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                      <button
                        onClick={() => handleEditMessage(idx)}
                        style={{
                          padding: '4px 8px',
                          fontSize: 11,
                          borderRadius: 4,
                          border: '1px solid var(--border)',
                          background: 'transparent',
                          color: 'var(--foreground)',
                          cursor: 'pointer',
                          opacity: 0.7,
                        }}
                        title="Edit message"
                      >
                        ✏️ Edit
                      </button>
                      {m.role === 'user' && (
                        <button
                          onClick={() => handleRegenerateResponse(idx)}
                          style={{
                            padding: '4px 8px',
                            fontSize: 11,
                            borderRadius: 4,
                            border: '1px solid var(--border)',
                            background: 'transparent',
                            color: 'var(--foreground)',
                            cursor: 'pointer',
                            opacity: 0.7,
                          }}
                          title="Regenerate response from this message"
                        >
                          🔄 Regenerate
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {/* RAG Controls */}
      <div style={{ display: 'flex', gap: 8, marginTop: 12, alignItems: 'center' }}>
        <button
          onClick={() => void toggleRag()}
          disabled={isStreaming}
          style={{
            padding: '6px 10px',
            borderRadius: 6,
            border: '1px solid var(--border)',
            background: ragEnabled ? 'var(--success)' : 'var(--button-hover)',
            color: ragEnabled ? 'white' : 'var(--foreground)',
            cursor: isStreaming ? 'not-allowed' : 'pointer',
            fontSize: 12,
            fontWeight: 500,
          }}
        >
          RAG: {ragEnabled ? 'ON' : 'OFF'}
        </button>

        <label style={{ fontSize: 12, cursor: isStreaming || ragStatus === 'uploading' ? 'not-allowed' : 'pointer' }}>
          <input
            type="file"
            accept=".txt,.md,.json,.pdf"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void uploadFile(file);
            }}
            disabled={isStreaming || ragStatus === 'uploading'}
            style={{ fontSize: 12 }}
          />
        </label>

        {ragStatus === 'uploading' && <span style={{ fontSize: 12, color: 'var(--foreground)', opacity: 0.6 }}>Uploading...</span>}
        {ragError && <span style={{ fontSize: 12, color: 'red' }}>{ragError}</span>}
      </div>

      <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
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
            flex: 1,
            padding: '10px 12px',
            borderRadius: 10,
            border: '1px solid var(--border)',
          }}
        />
        <button
          onClick={() => void send()}
          disabled={isStreaming || !input.trim()}
          style={{
            padding: '10px 14px',
            borderRadius: 10,
            border: '1px solid var(--border)',
            background: isStreaming ? 'var(--button-hover)' : 'var(--input-bg)',
            cursor: isStreaming ? 'not-allowed' : 'pointer',
          }}
        >
          Send
        </button>
      </div>

      <div style={{ marginTop: 8, fontSize: 12, opacity: 0.65 }}>
        Streaming via <code>/api/chat/stream</code>
      </div>
    </div>
  );
}
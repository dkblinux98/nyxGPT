'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
};

type Props = {
  sessionName: string;
};

export default function ChatPane({ sessionName }: Props) {
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<'idle' | 'connecting' | 'streaming' | 'error'>('idle');
  const [lastError, setLastError] = useState<string | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const isStreamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  // RAG state
  const [ragEnabled, setRagEnabled] = useState<boolean>(false);
  const [ragStatus, setRagStatus] = useState<'idle' | 'uploading' | 'error'>('idle');
  const [ragError, setRagError] = useState<string | null>(null);

  // Rename state
  const [sessionTitle, setSessionTitle] = useState<string>('');
  const [renaming, setRenaming] = useState<boolean>(false);

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
    // New session selected: clear local transcript UI.
    // (Server-side transcript will be surfaced later via a session load endpoint.)
    setMessages([]);
    setStatus('idle');
    setLastError(null);
    setInput('');
    isStreamingRef.current = false;
    abortRef.current?.abort();
    abortRef.current = null;

    // Fetch session metadata (RAG status and title)
    fetch(`/api/sessions/${encodeURIComponent(sessionName)}/metadata`)
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          setRagEnabled(data.rag_enabled || false);
          setSessionTitle(data.title || '');
          setRagError(null);
        }
      })
      .catch((err) => {
        console.error('Failed to fetch session metadata:', err);
        setRagEnabled(false); // Default to disabled if fetch fails
        setSessionTitle(''); // Default to empty title
      });
  }, [sessionName]);

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
      alert(`Failed to rename session: ${msg}`);
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

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });

        // Append chunk to last assistant message
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <div style={{ marginTop: 4, fontSize: 12, opacity: 0.7 }}>
            Status: <strong>{status}</strong>
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
            border: '1px solid #ddd',
            fontSize: 14,
            cursor: isStreaming ? 'not-allowed' : 'pointer',
            background: isStreaming ? '#f5f5f5' : 'white',
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
          border: '1px solid #e5e5e5',
          borderRadius: 10,
          overflowY: 'auto',
          whiteSpace: 'pre-wrap',
          background: 'white',
        }}
      >
        {messages.length === 0 ? (
          <div style={{ opacity: 0.7 }}>Send a message to start…</div>
        ) : (
          messages.map((m, idx) => (
            <div key={idx} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>
                <strong>{m.role}</strong>
              </div>
              <div>{m.content}</div>
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
            border: '1px solid #ddd',
            background: ragEnabled ? '#4caf50' : '#f5f5f5',
            color: ragEnabled ? 'white' : 'black',
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

        {ragStatus === 'uploading' && <span style={{ fontSize: 12, color: '#666' }}>Uploading...</span>}
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
            border: '1px solid #ddd',
          }}
        />
        <button
          onClick={() => void send()}
          disabled={isStreaming || !input.trim()}
          style={{
            padding: '10px 14px',
            borderRadius: 10,
            border: '1px solid #ddd',
            background: isStreaming ? '#f5f5f5' : 'white',
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
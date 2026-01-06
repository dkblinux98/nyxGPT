'use client';

import { useEffect, useState } from 'react';
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

  // Search and filter state
  const [searchText, setSearchText] = useState<string>('');
  const [filterModel, setFilterModel] = useState<string>('');
  const [filterPinned, setFilterPinned] = useState<string>('all'); // 'all', 'pinned', 'unpinned'
  const [filterTags, setFilterTags] = useState<string>('');

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
      }}
    >
      <aside
        style={{
          width: 320,
          borderRight: '1px solid #ddd',
          padding: '1rem',
          overflowY: 'auto',
        }}
      >
        <h1 style={{ margin: 0 }}>myGPT</h1>
        <p style={{ marginTop: 6, marginBottom: 16, opacity: 0.8 }}>
          Local web UI (early)
        </p>

        {/* Search input */}
        <input
          type="text"
          placeholder="Search sessions..."
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          style={{
            width: '100%',
            padding: '8px 10px',
            marginBottom: 8,
            border: '1px solid #ddd',
            borderRadius: 6,
            fontSize: 14,
            boxSizing: 'border-box',
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
      </aside>

      <section style={{ flex: 1, padding: '1.5rem', overflowY: 'auto' }}>
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
          <ChatPane sessionName={selectedSession} />
        </div>
      </section>
    </main>
  );
}

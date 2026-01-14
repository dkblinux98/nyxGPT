'use client';

import { memo, useCallback } from 'react';
import { Virtuoso } from 'react-virtuoso';

type Session = {
  name: string;
  modified?: string;
  messages?: number;
  pinned?: boolean;
  tags?: string[];
  title?: string;
  summary?: string;
  token_estimate?: number;
  model?: string;
};

type VirtualizedSessionListProps = {
  sessions: Session[];
  selectedSession: string;
  onSelectSession: (name: string) => void;
  onContextMenu: (e: React.MouseEvent, sessionName: string) => void;
  searchText?: string;
  pendingSessions: Set<string>;
  highlightText: (text: string, search: string) => React.ReactNode;
};

// Memoized individual session item to prevent unnecessary re-renders
const SessionItem = memo(function SessionItem({
  session,
  isActive,
  onSelect,
  onContextMenu,
  searchText = '',
  isPending,
  highlightText,
}: {
  session: Session;
  isActive: boolean;
  onSelect: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
  searchText?: string;
  isPending: boolean;
  highlightText: (text: string, search: string) => React.ReactNode;
}) {
  const displayText = session.title?.trim() ? session.title : session.name;

  return (
    <button
      onClick={onSelect}
      onContextMenu={onContextMenu}
      style={{
        textAlign: 'left',
        padding: '10px 10px',
        borderRadius: 8,
        border: '1px solid ' + (isActive ? 'var(--border)' : 'var(--border-light)'),
        background: isActive ? 'var(--active-bg)' : 'var(--sidebar-bg)',
        cursor: 'pointer',
        color: 'var(--foreground)',
        opacity: isPending ? 0.6 : 1,
        transition: 'opacity 0.2s ease',
        position: 'relative',
        width: '100%',
        marginBottom: '6px',
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 14, display: 'flex', alignItems: 'center', gap: 6 }}>
        {session.pinned && <span>📌 </span>}
        {searchText ? highlightText(displayText, searchText) : displayText}
        {isPending && (
          <span
            style={{
              fontSize: 10,
              opacity: 0.5,
              animation: 'pulse 1.5s ease-in-out infinite',
              willChange: 'transform, opacity'
            }}
            title="Syncing..."
          >
            ⟳
          </span>
        )}
      </div>
      <div style={{ fontSize: 12, opacity: 0.75, marginTop: 2 }}>
        {session.messages ?? 0} msg · {session.model ?? ''}
      </div>
      {!!(session.tags && session.tags.length) && (
        <div style={{ fontSize: 12, opacity: 0.7, marginTop: 4 }}>
          {session.tags.slice(0, 5).join(', ')}
        </div>
      )}
    </button>
  );
});

export const VirtualizedSessionList = memo(function VirtualizedSessionList({
  sessions,
  selectedSession,
  onSelectSession,
  onContextMenu,
  searchText = '',
  pendingSessions,
  highlightText,
}: VirtualizedSessionListProps) {
  // Row renderer for virtualized list
  const renderItem = useCallback((index: number) => {
    const session = sessions[index];
    const isActive = session.name === selectedSession;
    const isPending = pendingSessions.has(session.name);

    const handleSelect = () => {
      onSelectSession(session.name);
    };

    const handleContextMenu = (e: React.MouseEvent) => {
      onContextMenu(e, session.name);
    };

    return (
      <SessionItem
        session={session}
        isActive={isActive}
        onSelect={handleSelect}
        onContextMenu={handleContextMenu}
        searchText={searchText}
        isPending={isPending}
        highlightText={highlightText}
      />
    );
  }, [sessions, selectedSession, onSelectSession, onContextMenu, searchText, pendingSessions, highlightText]);

  if (sessions.length === 0) {
    return (
      <div style={{ fontSize: 12, opacity: 0.75 }}>
        No sessions found yet.
      </div>
    );
  }

  return (
    <Virtuoso
      style={{ height: '600px' }}
      totalCount={sessions.length}
      itemContent={renderItem}
      overscan={3}
    />
  );
});

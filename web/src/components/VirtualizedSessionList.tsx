'use client';

import { memo, useCallback, useMemo, useRef, useEffect, KeyboardEvent } from 'react';
import { Virtuoso, VirtuosoHandle } from 'react-virtuoso';

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
  itemRef,
}: {
  session: Session;
  isActive: boolean;
  onSelect: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
  searchText?: string;
  isPending: boolean;
  highlightText: (text: string, search: string) => React.ReactNode;
  itemRef?: (el: HTMLButtonElement | null) => void;
}) {
  const displayText = session.title?.trim() ? session.title : session.name;

  return (
    <button
      ref={itemRef}
      onClick={onSelect}
      onContextMenu={onContextMenu}
      tabIndex={isActive ? 0 : -1}
      style={{
        textAlign: 'left',
        padding: '10px 10px',
        minHeight: 44,
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
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const itemRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

  // Stable callbacks to prevent renderItem from being recreated on every render
  const handleSelect = useCallback((sessionName: string) => {
    onSelectSession(sessionName);
  }, [onSelectSession]);

  const handleContextMenuClick = useCallback((e: React.MouseEvent, sessionName: string) => {
    onContextMenu(e, sessionName);
  }, [onContextMenu]);

  // Convert pendingSessions Set to callback to avoid passing entire Set in dependencies
  const isPendingCallback = useCallback((sessionName: string) => {
    return pendingSessions.has(sessionName);
  }, [pendingSessions]);

  // Memoize handler factories to avoid creating new functions on every renderItem call
  const createSelectHandler = useCallback((sessionName: string) => {
    return () => handleSelect(sessionName);
  }, [handleSelect]);

  const createContextMenuHandler = useCallback((sessionName: string) => {
    return (e: React.MouseEvent) => handleContextMenuClick(e, sessionName);
  }, [handleContextMenuClick]);

  // Pre-create all handlers for current session list
  // This trades memory (one handler per session) for performance (stable references)
  const sessionHandlers = useMemo(() => {
    const handlers = new Map<string, {
      onSelect: () => void;
      onContextMenu: (e: React.MouseEvent) => void;
    }>();

    sessions.forEach(session => {
      handlers.set(session.name, {
        onSelect: createSelectHandler(session.name),
        onContextMenu: createContextMenuHandler(session.name),
      });
    });

    return handlers;
  }, [sessions, createSelectHandler, createContextMenuHandler]);

  // Keyboard navigation support
  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    const currentIndex = sessions.findIndex(s => s.name === selectedSession);
    if (currentIndex === -1) return;

    let nextIndex = currentIndex;

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        nextIndex = Math.min(currentIndex + 1, sessions.length - 1);
        break;
      case 'ArrowUp':
        e.preventDefault();
        nextIndex = Math.max(currentIndex - 1, 0);
        break;
      case 'Home':
        e.preventDefault();
        nextIndex = 0;
        break;
      case 'End':
        e.preventDefault();
        nextIndex = sessions.length - 1;
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        // Already selected, no action needed
        return;
      default:
        return;
    }

    if (nextIndex !== currentIndex) {
      const nextSession = sessions[nextIndex];
      onSelectSession(nextSession.name);

      // Scroll to the newly selected item
      virtuosoRef.current?.scrollToIndex({
        index: nextIndex,
        align: 'center',
        behavior: 'smooth',
      });
    }
  }, [sessions, selectedSession, onSelectSession]);

  // Focus the selected item when selection changes
  useEffect(() => {
    const selectedButton = itemRefs.current.get(selectedSession);
    if (selectedButton) {
      selectedButton.focus();
    }
  }, [selectedSession]);

  // Row renderer for virtualized list
  // Now uses pre-created stable handlers, only recreated when sessions change
  const renderItem = useCallback((index: number) => {
    const session = sessions[index];
    const isActive = session.name === selectedSession;
    const isPending = isPendingCallback(session.name);
    const handlers = sessionHandlers.get(session.name)!;

    return (
      <SessionItem
        session={session}
        isActive={isActive}
        onSelect={handlers.onSelect}
        onContextMenu={handlers.onContextMenu}
        searchText={searchText}
        isPending={isPending}
        highlightText={highlightText}
        itemRef={(el) => {
          if (el) {
            itemRefs.current.set(session.name, el);
          } else {
            itemRefs.current.delete(session.name);
          }
        }}
      />
    );
  }, [sessions, selectedSession, isPendingCallback, sessionHandlers, searchText, highlightText]);

  if (sessions.length === 0) {
    return (
      <div style={{ fontSize: 12, opacity: 0.75 }}>
        No sessions found yet.
      </div>
    );
  }

  return (
    <div
      onKeyDown={handleKeyDown}
      tabIndex={-1}
      style={{
        flex: 1,
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <Virtuoso
        ref={virtuosoRef}
        style={{ height: '100%' }}
        totalCount={sessions.length}
        itemContent={renderItem}
        overscan={5}
        aria-label="Session list (use arrow keys to navigate)"
        role="list"
        data-total-sessions={sessions.length}
      />
    </div>
  );
});

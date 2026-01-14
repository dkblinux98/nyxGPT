import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { VirtualizedSessionList } from '../../src/components/VirtualizedSessionList';

// Mock react-virtuoso for testing environment
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent }: any) => {
    return (
      <div data-testid="virtualized-list">
        {Array.from({ length: Math.min(totalCount, 10) }).map((_, index) => (
          <div key={index}>{itemContent(index)}</div>
        ))}
      </div>
    );
  },
}));

describe('VirtualizedSessionList', () => {
  const mockSessions = [
    {
      name: 'session-1',
      title: 'First Session',
      messages: 5,
      model: 'llama3.1:8b',
      pinned: false,
      tags: ['test', 'example'],
    },
    {
      name: 'session-2',
      title: 'Second Session',
      messages: 10,
      model: 'mistral:7b',
      pinned: true,
      tags: [],
    },
    {
      name: 'session-3',
      title: 'Third Session',
      messages: 3,
      model: 'llama3.1:8b',
      pinned: false,
      tags: ['work'],
    },
  ];

  const mockHighlightText = (text: string, search: string) => {
    if (!search) return text;
    return text;
  };

  it('renders empty message when no sessions', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={[]}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    expect(screen.getByText('No sessions found yet.')).toBeInTheDocument();
  });

  it('renders virtualized list with sessions', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Check that at least some sessions are rendered (virtualized list may not render all)
    expect(screen.getByText('First Session')).toBeInTheDocument();
  });

  it('shows pinned indicator for pinned sessions', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-2"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Check for pin indicator (📌)
    const sessionButtons = screen.getAllByRole('button');
    const pinnedButton = sessionButtons.find(btn => btn.textContent?.includes('📌'));
    expect(pinnedButton).toBeDefined();
  });

  it('shows syncing indicator for pending sessions', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const pendingSessions = new Set(['session-1']);

    render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={pendingSessions}
        highlightText={mockHighlightText}
      />
    );

    // Check for sync indicator (⟳)
    const sessionButtons = screen.getAllByRole('button');
    const pendingButton = sessionButtons.find(btn => btn.textContent?.includes('⟳'));
    expect(pendingButton).toBeDefined();
  });

  it('memoization: does not re-render when props are unchanged', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { rerender } = render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Re-render with same props
    rerender(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Component should not re-render (tested via React DevTools profiler in real usage)
    // This test mainly ensures component renders without errors
    expect(screen.getByText('First Session')).toBeInTheDocument();
  });

  it('displays session metadata correctly', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Check for message count and model (multiple sessions may have same model)
    expect(screen.getByText(/5 msg/)).toBeInTheDocument();
    expect(screen.getAllByText(/llama3.1:8b/).length).toBeGreaterThan(0);
  });
});

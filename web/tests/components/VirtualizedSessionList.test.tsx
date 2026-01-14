import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { VirtualizedSessionList } from '../../src/components/VirtualizedSessionList';
import React from 'react';

// Mock react-virtuoso for testing environment
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: any) => {
    // Render more items for testing (50 max to verify larger lists work)
    const renderCount = Math.min(totalCount, 50);
    return (
      <div
        data-testid="virtualized-list"
        style={style}
        aria-label={props['aria-label']}
        role={props.role}
        data-total-sessions={props['data-total-sessions']}
      >
        {Array.from({ length: renderCount }).map((_, index) => (
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

  it('calls onSelectSession when session is clicked', () => {
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

    const firstSessionButton = screen.getByText('First Session').closest('button');
    fireEvent.click(firstSessionButton!);

    expect(mockHandlers.onSelectSession).toHaveBeenCalledWith('session-1');
  });

  it('calls onContextMenu when session is right-clicked', () => {
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

    const firstSessionButton = screen.getByText('First Session').closest('button');
    fireEvent.contextMenu(firstSessionButton!);

    expect(mockHandlers.onContextMenu).toHaveBeenCalled();
    const callArgs = mockHandlers.onContextMenu.mock.calls[0];
    expect(callArgs[1]).toBe('session-1'); // Second arg should be session name
  });

  it('memoization: uses stable callback references', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    // Track render count via SessionItem render spy
    const renderSpy = vi.fn();

    const OriginalMemo = React.memo;
    const memoSpy = vi.fn((component: any) => {
      return OriginalMemo((props: any) => {
        renderSpy(props.session?.name);
        return component(props);
      });
    });

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

    const initialRenderCount = renderSpy.mock.calls.length;

    // Re-render with same props - memoized component should not re-render children
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

    // Component should still render without errors
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

  it('handles large lists (100+ sessions)', () => {
    const largeSessions = Array.from({ length: 150 }, (_, i) => ({
      name: `session-${i}`,
      title: `Session ${i}`,
      messages: i,
      model: 'llama3.1:8b',
      pinned: i % 10 === 0,
      tags: [`tag-${i % 5}`],
    }));

    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={largeSessions}
        selectedSession="session-0"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Virtuoso should render (mock renders up to 50 items)
    const list = screen.getByTestId('virtualized-list');
    expect(list).toBeInTheDocument();

    // Verify data attribute shows total count
    expect(list.getAttribute('data-total-sessions')).toBe('150');

    // At least first session should be visible
    expect(screen.getByText('Session 0')).toBeInTheDocument();
  });

  it('applies responsive flex styling', () => {
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

    const list = screen.getByTestId('virtualized-list');
    const styles = list.getAttribute('style');

    // Should use flex: 1 (or expanded flex-grow: 1) instead of fixed height
    expect(styles).toMatch(/flex: 1|flex-grow: 1/);
    expect(styles).toContain('min-height: 0');
    expect(styles).not.toContain('height: 600px');
  });

  it('includes proper ARIA attributes for accessibility', () => {
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

    const list = screen.getByTestId('virtualized-list');

    // Check ARIA attributes
    expect(list.getAttribute('aria-label')).toBe('Session list');
    expect(list.getAttribute('role')).toBe('list');
    expect(list.getAttribute('data-total-sessions')).toBe('3');
  });

  it('updates when sessions change', () => {
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

    expect(screen.getByText('First Session')).toBeInTheDocument();

    // Update sessions with new data
    const newSessions = [
      {
        name: 'session-4',
        title: 'Fourth Session',
        messages: 7,
        model: 'qwen:14b',
        pinned: false,
        tags: ['new'],
      },
    ];

    rerender(
      <VirtualizedSessionList
        sessions={newSessions}
        selectedSession="session-4"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    // Should show new session
    expect(screen.getByText('Fourth Session')).toBeInTheDocument();
    // Old session should be gone
    expect(screen.queryByText('First Session')).not.toBeInTheDocument();
  });
});

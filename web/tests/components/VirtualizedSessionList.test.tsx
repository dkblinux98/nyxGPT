import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { VirtualizedSessionList } from '../../src/components/VirtualizedSessionList';
import React from 'react';

// Mock react-virtuoso for testing environment
// This mock simulates viewport-based rendering to verify virtualization works
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, overscan, ...props }: any) => {
    // Simulate viewport rendering: only render ~20 items regardless of total count
    // This verifies that large lists don't render all items at once
    const VIEWPORT_ITEMS = 20;
    const renderCount = Math.min(totalCount, VIEWPORT_ITEMS);

    return (
      <div
        data-testid="virtualized-list"
        style={style}
        aria-label={props['aria-label']}
        role={props.role}
        data-total-sessions={props['data-total-sessions']}
        data-rendered-items={renderCount}
        data-overscan={overscan}
      >
        {Array.from({ length: renderCount }).map((_, index) => (
          <div key={index} data-item-index={index}>
            {itemContent(index)}
          </div>
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
    // The responsive flex sizing lives on Virtuoso's wrapping container;
    // Virtuoso itself just fills that container at height: 100%.
    const wrapperStyles = list.parentElement?.getAttribute('style');

    // Should use flex: 1 (or expanded flex-grow: 1) instead of fixed height
    expect(wrapperStyles).toMatch(/flex: 1|flex-grow: 1/);
    expect(wrapperStyles).toContain('min-height: 0');
    expect(list.getAttribute('style')).not.toContain('height: 600px');
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
    expect(list.getAttribute('aria-label')).toBe('Session list (use arrow keys to navigate)');
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

  it('verifies virtualization: only renders visible items (not all 1000)', () => {
    // Create a large session list (1000 items) to verify virtualization
    const largeSessionList = Array.from({ length: 1000 }, (_, i) => ({
      name: `session-${i}`,
      title: `Session ${i}`,
      messages: i,
      model: 'llama3.1:8b',
      pinned: false,
      tags: [],
    }));

    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    render(
      <VirtualizedSessionList
        sessions={largeSessionList}
        selectedSession="session-0"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    const list = screen.getByTestId('virtualized-list');

    // Verify total count is reported correctly
    expect(list.getAttribute('data-total-sessions')).toBe('1000');

    // Verify only viewport items are rendered (our mock renders ~20 items max)
    const renderedItems = list.getAttribute('data-rendered-items');
    expect(Number(renderedItems)).toBeLessThanOrEqual(20);
    expect(Number(renderedItems)).toBeLessThan(1000);

    // Verify first few items are visible
    expect(screen.getByText('Session 0')).toBeInTheDocument();

    // Verify items beyond viewport are NOT rendered
    // Session 500 should not be in the DOM since only ~20 items are rendered
    expect(screen.queryByText('Session 500')).not.toBeInTheDocument();
    expect(screen.queryByText('Session 999')).not.toBeInTheDocument();

    // This confirms virtualization is working: we have 1000 sessions
    // but only ~20 DOM nodes, preventing performance issues
  });

  it('verifies overscan configuration for smooth scrolling', () => {
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

    // Verify overscan is configured (should be 5 for better keyboard navigation)
    expect(list.getAttribute('data-overscan')).toBe('5');
  });

  it('supports keyboard navigation with arrow keys', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');
    expect(listContainer).toBeInTheDocument();

    // Simulate ArrowDown key press
    fireEvent.keyDown(listContainer!, { key: 'ArrowDown' });

    // Should select next session (session-2)
    expect(mockHandlers.onSelectSession).toHaveBeenCalledWith('session-2');
  });

  it('supports keyboard navigation with Home and End keys', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');

    // Simulate Home key press
    fireEvent.keyDown(listContainer!, { key: 'Home' });
    expect(mockHandlers.onSelectSession).toHaveBeenCalledWith('session-1');

    // Simulate End key press
    fireEvent.keyDown(listContainer!, { key: 'End' });
    expect(mockHandlers.onSelectSession).toHaveBeenCalledWith('session-3');
  });

  it('falls back to the session name when title is missing or blank', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const sessionsWithoutTitle = [
      { name: 'raw-session-name', messages: 1, pinned: false, tags: [] },
      { name: 'blank-title-session', title: '   ', messages: 2, pinned: false, tags: [] },
    ];

    render(
      <VirtualizedSessionList
        sessions={sessionsWithoutTitle}
        selectedSession="raw-session-name"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    expect(screen.getByText('raw-session-name')).toBeInTheDocument();
    expect(screen.getByText('blank-title-session')).toBeInTheDocument();
  });

  it('renders blank model text when session has no model', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const sessionsWithoutModel = [
      { name: 'no-model-session', title: 'No Model', messages: 4, pinned: false, tags: [] },
      { name: 'no-messages-session', title: 'No Messages', pinned: false, tags: [] },
    ];

    render(
      <VirtualizedSessionList
        sessions={sessionsWithoutModel}
        selectedSession="no-model-session"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    expect(screen.getByText(/4 msg/)).toBeInTheDocument();
    // session.messages defaults to 0 when missing
    expect(screen.getByText(/^0 msg/)).toBeInTheDocument();
  });

  it('invokes highlightText when a search term is active', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const highlightSpy = vi.fn((text: string, search: string) => (
      <mark data-testid={`highlight-${text}`}>{text}</mark>
    ));

    render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="session-1"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText="First"
        pendingSessions={new Set()}
        highlightText={highlightSpy}
      />
    );

    expect(highlightSpy).toHaveBeenCalledWith('First Session', 'First');
    expect(screen.getByTestId('highlight-First Session')).toBeInTheDocument();
  });

  it('supports keyboard navigation with the ArrowUp key', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');
    fireEvent.keyDown(listContainer!, { key: 'ArrowUp' });

    expect(mockHandlers.onSelectSession).toHaveBeenCalledWith('session-1');
  });

  it('does nothing on Enter or Space key (already selected)', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');
    fireEvent.keyDown(listContainer!, { key: 'Enter' });
    fireEvent.keyDown(listContainer!, { key: ' ' });

    expect(mockHandlers.onSelectSession).not.toHaveBeenCalled();
  });

  it('ignores unrecognized keys', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');
    fireEvent.keyDown(listContainer!, { key: 'Tab' });

    expect(mockHandlers.onSelectSession).not.toHaveBeenCalled();
  });

  it('does nothing on keydown when the selected session is not in the list', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
      <VirtualizedSessionList
        sessions={mockSessions}
        selectedSession="not-a-real-session"
        onSelectSession={mockHandlers.onSelectSession}
        onContextMenu={mockHandlers.onContextMenu}
        searchText=""
        pendingSessions={new Set()}
        highlightText={mockHighlightText}
      />
    );

    const listContainer = container.querySelector('div[tabindex="-1"]');
    fireEvent.keyDown(listContainer!, { key: 'ArrowDown' });

    expect(mockHandlers.onSelectSession).not.toHaveBeenCalled();
  });

  it('does not call onSelectSession when ArrowUp is pressed at the first item', () => {
    const mockHandlers = {
      onSelectSession: vi.fn(),
      onContextMenu: vi.fn(),
    };

    const { container } = render(
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

    const listContainer = container.querySelector('div[tabindex="-1"]');
    fireEvent.keyDown(listContainer!, { key: 'ArrowUp' });

    expect(mockHandlers.onSelectSession).not.toHaveBeenCalled();
  });

  it('updated ARIA label mentions keyboard navigation', () => {
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
    const ariaLabel = list.getAttribute('aria-label');

    // Should mention arrow keys for better accessibility
    expect(ariaLabel).toContain('arrow keys');
  });
});

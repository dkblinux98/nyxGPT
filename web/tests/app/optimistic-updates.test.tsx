import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Home from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';
import type { VirtuosoMockProps } from '../mocks/virtuoso';

// Home renders the session sidebar via next/dynamic(VirtualizedSessionList),
// which wraps react-virtuoso. Virtuoso relies on real layout/ResizeObserver
// measurements it can't get in happy-dom, so without this mock it renders
// zero items and every session-lookup query below times out.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: VirtuosoMockProps) => (
    <div style={style} aria-label={props['aria-label']} role={props.role}>
      {Array.from({ length: totalCount }).map((_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

// Home uses useTheme(), which requires a ThemeProvider ancestor.
function renderHome() {
  return render(
    <ThemeProvider>
      <Home />
    </ThemeProvider>
  );
}

/**
 * Optimistic Updates Tests
 *
 * Tests for optimistic UI update logic in session operations.
 * These tests verify that UI updates occur immediately and rollback on failure.
 */

// Mock window.confirm and window.prompt
global.confirm = vi.fn();
global.prompt = vi.fn();

// Mock ToastContext
const mockToastContext = {
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  showToast: vi.fn(),
};

vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => mockToastContext,
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// Shape of the canned responses `mockFetchResponses` is seeded with, keyed
// by "METHOD /path". Only `sessions` is read back (the live-state tracking
// below replays it); everything else is handed to the component as-is.
type SessionRecord = Record<string, unknown>;
type MockResponse = {
  sessions?: SessionRecord[];
  [key: string]: unknown;
};

describe('Optimistic Updates Logic', () => {
  beforeEach(() => {
    // Clear any state before each test
  });

  describe('Session State Management', () => {
    it('should immediately update local state for pin toggle', () => {
      // Simulate initial state
      const sessions = [
        { name: 'session1', pinned: false },
        { name: 'session2', pinned: true },
      ];

      // Simulate pin toggle for session1
      const sessionName = 'session1';
      const session = sessions.find((s) => s.name === sessionName);
      const isPinned = session?.pinned || false;

      // Optimistic update
      const optimisticSessions = sessions.map((s) =>
        s.name === sessionName ? { ...s, pinned: !isPinned } : s
      );

      expect(optimisticSessions[0].pinned).toBe(true);
      expect(optimisticSessions[1].pinned).toBe(true);
    });

    it('should immediately remove session from list on delete', () => {
      const sessions = [
        { name: 'session1', pinned: false },
        { name: 'session2', pinned: true },
        { name: 'session3', pinned: false },
      ];

      const sessionToDelete = 'session2';
      const remainingSessions = sessions.filter((s) => s.name !== sessionToDelete);

      expect(remainingSessions.length).toBe(2);
      expect(remainingSessions.find((s) => s.name === sessionToDelete)).toBeUndefined();
    });

    it('should immediately add new session to list on create', () => {
      const sessions = [
        { name: 'session1', pinned: false },
      ];

      const newSession = {
        name: 'session2',
        messages: 0,
        pinned: false,
        tags: [],
        title: 'session2',
        modified: new Date().toISOString(),
      };

      const updatedSessions = [newSession, ...sessions];

      expect(updatedSessions.length).toBe(2);
      expect(updatedSessions[0].name).toBe('session2');
    });

    it('should immediately update session title on rename', () => {
      const sessions = [
        { name: 'session1', title: 'Old Title', pinned: false },
      ];

      const sessionName = 'session1';
      const newTitle = 'New Title';

      const optimisticSessions = sessions.map((s) =>
        s.name === sessionName ? { ...s, title: newTitle } : s
      );

      expect(optimisticSessions[0].title).toBe('New Title');
    });
  });

  describe('Rollback Logic', () => {
    it('should restore previous state on pin toggle failure', () => {
      const sessions = [
        { name: 'session1', pinned: false },
      ];

      // Save previous state (as done in the actual implementation)
      const previousSessions = [...sessions];

      // Optimistic update
      const optimisticSessions = sessions.map((s) =>
        s.name === 'session1' ? { ...s, pinned: true } : s
      );

      expect(optimisticSessions[0].pinned).toBe(true);

      // Simulate failure - rollback to previous state
      const rolledBackSessions = previousSessions;

      expect(rolledBackSessions[0].pinned).toBe(false);
    });

    it('should restore previous state on delete failure', () => {
      const sessions = [
        { name: 'session1', pinned: false },
        { name: 'session2', pinned: true },
      ];

      const previousSessions = [...sessions];
      const previousSelection = 'session2';

      // Optimistic update - remove session
      const optimisticSessions = sessions.filter((s) => s.name !== 'session2');
      expect(optimisticSessions.length).toBe(1);

      // Simulate failure - rollback
      const rolledBackSessions = previousSessions;
      const rolledBackSelection = previousSelection;

      expect(rolledBackSessions.length).toBe(2);
      expect(rolledBackSelection).toBe('session2');
    });

    it('should restore previous state on create failure', () => {
      const sessions = [
        { name: 'session1', pinned: false },
      ];

      const previousSessions = [...sessions];
      const previousSelection = 'session1';

      // Optimistic update - add new session
      const newSession = {
        name: 'session2',
        messages: 0,
        pinned: false,
        tags: [],
        title: 'session2',
        modified: new Date().toISOString(),
      };
      const optimisticSessions = [newSession, ...sessions];
      const optimisticSelection = 'session2';

      expect(optimisticSessions.length).toBe(2);
      expect(optimisticSelection).toBe('session2');

      // Simulate failure - rollback
      const rolledBackSessions = previousSessions;
      const rolledBackSelection = previousSelection;

      expect(rolledBackSessions.length).toBe(1);
      expect(rolledBackSelection).toBe('session1');
    });

    it('should restore previous state on rename failure', () => {
      const sessions = [
        { name: 'session1', title: 'Old Title', pinned: false },
      ];

      const previousSessions = [...sessions];

      // Optimistic update
      const optimisticSessions = sessions.map((s) =>
        s.name === 'session1' ? { ...s, title: 'New Title' } : s
      );

      expect(optimisticSessions[0].title).toBe('New Title');

      // Simulate failure - rollback
      const rolledBackSessions = previousSessions;

      expect(rolledBackSessions[0].title).toBe('Old Title');
    });
  });

  describe('Pending State Management', () => {
    it('should add session to pending set when operation starts', () => {
      const pendingSessions = new Set<string>();
      const sessionName = 'session1';

      // Mark as pending
      const updatedPendingSessions = new Set(pendingSessions).add(sessionName);

      expect(updatedPendingSessions.has(sessionName)).toBe(true);
      expect(updatedPendingSessions.size).toBe(1);
    });

    it('should remove session from pending set when operation completes', () => {
      const pendingSessions = new Set<string>(['session1', 'session2']);
      const sessionName = 'session1';

      // Remove from pending
      const updatedPendingSessions = new Set(pendingSessions);
      updatedPendingSessions.delete(sessionName);

      expect(updatedPendingSessions.has(sessionName)).toBe(false);
      expect(updatedPendingSessions.size).toBe(1);
      expect(updatedPendingSessions.has('session2')).toBe(true);
    });

    it('should handle multiple concurrent pending operations', () => {
      const pendingSessions = new Set<string>();

      // Start multiple operations
      pendingSessions.add('session1');
      pendingSessions.add('session2');
      pendingSessions.add('session3');

      expect(pendingSessions.size).toBe(3);
      expect(pendingSessions.has('session1')).toBe(true);
      expect(pendingSessions.has('session2')).toBe(true);
      expect(pendingSessions.has('session3')).toBe(true);

      // Complete one operation
      pendingSessions.delete('session2');

      expect(pendingSessions.size).toBe(2);
      expect(pendingSessions.has('session2')).toBe(false);
      expect(pendingSessions.has('session1')).toBe(true);
      expect(pendingSessions.has('session3')).toBe(true);
    });
  });

  describe('Session Selection Logic', () => {
    it('should switch to another session when deleting selected session', () => {
      const sessions = [
        { name: 'session1', pinned: false },
        { name: 'session2', pinned: true },
        { name: 'session3', pinned: false },
      ];

      const selectedSession = 'session2';
      const sessionToDelete = 'session2';

      // Check if we need to switch selection
      const needsSwitching = sessionToDelete === selectedSession;
      expect(needsSwitching).toBe(true);

      // Get remaining sessions
      const remainingSessions = sessions.filter((s) => s.name !== sessionToDelete);
      const newSelection = remainingSessions[0]?.name || 'default';

      expect(newSelection).toBe('session1');
    });

    it('should not change selection when deleting non-selected session', () => {
      const sessions = [
        { name: 'session1', pinned: false },
        { name: 'session2', pinned: true },
      ];

      const selectedSession = 'session1';
      const sessionToDelete = 'session2';

      const needsSwitching = sessionToDelete === selectedSession;
      expect(needsSwitching).toBe(false);
    });

    it('should update selection when filename changes during rename', () => {
      const sessionName = 'session1';
      const selectedSession = 'session1';
      const responseData = { new_name: 'new-session1' };

      const shouldUpdateSelection =
        responseData.new_name !== sessionName &&
        sessionName === selectedSession;

      expect(shouldUpdateSelection).toBe(true);

      if (shouldUpdateSelection) {
        const newSelection = responseData.new_name;
        expect(newSelection).toBe('new-session1');
      }
    });
  });

  describe('Edge Cases', () => {
    it('should handle empty session list', () => {
      const sessions: unknown[] = [];
      const optimisticSessions = sessions.filter((s) => s.name !== 'nonexistent');

      expect(optimisticSessions.length).toBe(0);
    });

    it('should handle non-existent session in pin toggle', () => {
      const sessions = [{ name: 'session1', pinned: false }];
      const sessionName = 'nonexistent';

      const session = sessions.find((s) => s.name === sessionName);
      expect(session).toBeUndefined();

      const isPinned = session?.pinned || false;
      expect(isPinned).toBe(false);
    });

    it('should preserve other session properties during updates', () => {
      const sessions = [
        {
          name: 'session1',
          pinned: false,
          messages: 5,
          model: 'llama3',
          tags: ['test']
        },
      ];

      const optimisticSessions = sessions.map((s) =>
        s.name === 'session1' ? { ...s, pinned: true } : s
      );

      expect(optimisticSessions[0].pinned).toBe(true);
      expect(optimisticSessions[0].messages).toBe(5);
      expect(optimisticSessions[0].model).toBe('llama3');
      expect(optimisticSessions[0].tags).toEqual(['test']);
    });
  });
});

describe('Optimistic Updates Integration Tests', () => {
  const mockSessions = [
    {
      name: 'session1',
      title: 'Test Session 1',
      messages: 5,
      pinned: false,
      tags: ['test'],
      modified: '2024-01-01T12:00:00Z',
    },
    {
      name: 'session2',
      title: 'Test Session 2',
      messages: 3,
      pinned: true,
      tags: [],
      modified: '2024-01-02T12:00:00Z',
    },
    {
      name: 'session3',
      title: 'Test Session 3',
      messages: 0,
      pinned: false,
      tags: [],
      modified: '2024-01-03T12:00:00Z',
    },
  ];

  const mockFetchResponses = (responses: Record<string, MockResponse>) => {
    // Track live session state so that a revalidate's GET /api/sessions
    // reflects prior mutations (pin/unpin/delete/rename) instead of always
    // replaying the static list the test seeded — otherwise revalidate
    // silently clobbers the optimistic update it's meant to confirm.
    const seed = responses['GET /api/sessions'];
    let liveSessions: SessionRecord[] | null =
      seed && Array.isArray(seed.sessions) ? seed.sessions.map((s) => ({ ...s })) : null;

    vi.mocked(global.fetch).mockImplementation((url: string, options?: RequestInit) => {
      const method = options?.method || 'GET';
      const key = `${method} ${url}`;

      if (liveSessions && key === 'GET /api/sessions') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: liveSessions }),
          text: () => Promise.resolve(JSON.stringify({ sessions: liveSessions })),
        });
      }

      // Match patterns for dynamic URLs
      for (const [pattern, response] of Object.entries(responses)) {
        if (pattern === 'GET /api/sessions') continue;
        if (url.includes(pattern) || key.includes(pattern)) {
          if (response.error) {
            return Promise.resolve({
              ok: false,
              status: response.status || 500,
              text: () => Promise.resolve(response.error),
              json: () => Promise.resolve({ detail: response.error }),
            });
          }

          if (liveSessions) {
            if (url.includes('/api/sessions/init') && options?.body) {
              const body = JSON.parse(options.body);
              liveSessions = [
                { name: body.name, title: 'New Chat', messages: 0, pinned: false, tags: [], modified: new Date(0).toISOString() },
                ...liveSessions,
              ];
            }
            const match = url.match(/\/api\/sessions\/([^/]+)\/(pin|unpin|delete|rename)/);
            if (match) {
              const [, encodedName, action] = match;
              const sessionName = decodeURIComponent(encodedName);
              if (action === 'pin' || action === 'unpin') {
                liveSessions = liveSessions.map((s) =>
                  s.name === sessionName ? { ...s, pinned: action === 'pin' } : s
                );
              } else if (action === 'delete') {
                liveSessions = liveSessions.filter((s) => s.name !== sessionName);
              } else if (action === 'rename' && options?.body) {
                const body = JSON.parse(options.body);
                liveSessions = liveSessions.map((s) =>
                  s.name === sessionName ? { ...s, title: body.new_name } : s
                );
              }
            }
          }

          const isBlob = response instanceof Blob;
          return Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: () => Promise.resolve(response),
            text: () => Promise.resolve(isBlob ? '' : JSON.stringify(response)),
            blob: () => Promise.resolve(isBlob ? response : new Blob([JSON.stringify(response)])),
          });
        }
      }

      // Default response
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
    });
  };

  beforeEach(() => {
    // Re-assign after tests/setup.ts's MSW server.listen() patches global.fetch,
    // so this mock isn't clobbered by MSW's real fetch interceptor.
    global.fetch = vi.fn();
    vi.clearAllMocks();
    vi.mocked(global.confirm).mockReturnValue(true);
    vi.mocked(global.prompt).mockReturnValue('New Title');
  });

  describe('Pin Toggle Integration', () => {
    it('should show optimistic update immediately when pinning session', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/pin': { success: true },
      });

      renderHome();

      // Wait for sessions to load
      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      // Find and click pin button for session1
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      expect(session1Card).toBeInTheDocument();

      // Simulate right-click to open context menu
      const user = userEvent.setup();
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      // Find pin option in context menu
      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      // Verify optimistic update appears immediately (before API completes)
      await waitFor(() => {
        // Session should show pinned state immediately
        expect(session1Card).toHaveAttribute('data-pinned', 'true');
      }, { timeout: 100 }); // Very short timeout to verify it's optimistic
    });

    it('should rollback pin state when API fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/pin': { error: 'Failed to pin session', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      const user = userEvent.setup();

      // Trigger pin
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      // Wait for error alert and rollback
      await waitFor(() => {
        // Should rollback to original unpinned state
        expect(session1Card).toHaveAttribute('data-pinned', 'false');
      });
    });

    it('should prevent concurrent pin operations on same session', async () => {
      let pinCallCount = 0;
      vi.mocked(global.fetch).mockImplementation((url: string) => {
        if (url.includes('/pin')) {
          pinCallCount++;
          return new Promise((resolve) => {
            setTimeout(() => {
              resolve({
                ok: true,
                status: 200,
                json: () => Promise.resolve({ success: true }),
              });
            }, 100);
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: mockSessions }),
        });
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      const user = userEvent.setup();

      // Attempt rapid clicks
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByRole('button', { name: /pin/i });

      // Click multiple times rapidly
      await user.click(pinButton);
      await user.click(pinButton);
      await user.click(pinButton);

      // Wait for the pin operation to settle
      await waitFor(() => expect(pinCallCount).toBeGreaterThan(0));

      // Should only have one pin operation (second and third blocked by pending check)
      expect(pinCallCount).toBeLessThan(3);
    });
  });

  describe('Delete Session Integration', () => {
    it('should show optimistic update immediately when deleting session', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 3')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session3Card = screen.getByText('Test Session 3').closest('[data-session]');

      // Open context menu and delete
      await user.pointer({ target: session3Card!, keys: '[MouseRight]' });
      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      // Session should disappear immediately (optimistic update)
      await waitFor(() => {
        expect(screen.queryByText('Test Session 3')).not.toBeInTheDocument();
      }, { timeout: 100 });
    });

    it('should rollback delete when API fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { error: 'Failed to delete session', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 3')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session3Card = screen.getByText('Test Session 3').closest('[data-session]');

      await user.pointer({ target: session3Card!, keys: '[MouseRight]' });
      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      // Session should reappear after rollback
      await waitFor(() => {
        expect(screen.getByText('Test Session 3')).toBeInTheDocument();
      });
    });

    it('should switch selection when deleting selected session', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      // Assume session1 is selected
      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      // Delete selected session
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      // Should switch to next available session immediately. Unlike the
      // plain-delete case, deleting the *selected* session also triggers a
      // selection change (extra effects/fetches for the newly selected
      // session), so this needs more real-clock headroom than the 100ms
      // used for a same-session optimistic-update check.
      await waitFor(() => {
        // First remaining session should become selected
        expect(screen.queryByText('Test Session 1')).not.toBeInTheDocument();
        // Sessions 2 and 3 should still be visible
        const remainingSessions = screen.getAllByText(/Test Session [23]/);
        expect(remainingSessions.length).toBe(2);
      }, { timeout: 1000 });
    });
  });

  describe('Rename Session Integration', () => {
    it('should show optimistic update immediately when renaming session', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/rename': { success: true, new_name: 'session1' },
      });

      vi.mocked(global.prompt).mockReturnValue('Updated Title');

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      // Open context menu and rename
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const renameButton = await screen.findByText(/rename/i);
      await user.click(renameButton);

      // Title should update immediately
      await waitFor(() => {
        expect(screen.getByText('Updated Title')).toBeInTheDocument();
      }, { timeout: 100 });
    });

    it('should rollback rename when API fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/rename': { error: 'Failed to rename session', status: 500 },
      });

      vi.mocked(global.prompt).mockReturnValue('Failed Title');

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const renameButton = await screen.findByText(/rename/i);
      await user.click(renameButton);

      // Title should rollback to original
      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
        expect(screen.queryByText('Failed Title')).not.toBeInTheDocument();
      });
    });

    it('should prevent concurrent rename operations on same session', async () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

      // The rename endpoint responds slowly, keeping the first rename
      // "pending" long enough for the second, near-simultaneous rename to
      // land while it's still in flight and get blocked.
      vi.mocked(global.fetch).mockImplementation((url: string) => {
        if (url.includes('/rename')) {
          return new Promise((resolve) => setTimeout(() => resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ success: true, new_name: 'session1' }),
          }), 100));
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: mockSessions }),
        });
      });

      // Each rename must produce a distinct title, otherwise the second
      // prompt's return value matches the first rename's already-applied
      // optimistic title and renameSession() no-ops before ever reaching
      // the "operation already in progress" check.
      vi.mocked(global.prompt)
        .mockReturnValueOnce('New Title 1')
        .mockReturnValueOnce('New Title 2');

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      // Trigger first rename
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const renameButton1 = await screen.findByText(/rename/i);
      await user.click(renameButton1);

      // Immediately try another rename (should be blocked)
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const renameButton2 = await screen.findByText(/rename/i);
      await user.click(renameButton2);

      // Should log warning about operation in progress
      await waitFor(() => {
        expect(consoleSpy).toHaveBeenCalledWith(
          expect.stringContaining('Operation already in progress'),
          'session1'
        );
      });

      consoleSpy.mockRestore();
    });
  });

  describe('State Consistency After Operations', () => {
    it('should refresh state from server after successful operation', async () => {
      const refreshedSessions = [
        ...mockSessions,
        { name: 'session4', title: 'Server Added', messages: 0, pinned: false, tags: [], modified: '2024-01-04T12:00:00Z' },
      ];

      let callCount = 0;
      vi.mocked(global.fetch).mockImplementation((url: string) => {
        callCount++;
        // First call: initial load
        // Second call: operation
        // Third call: refresh after operation
        if (callCount === 1 || callCount >= 3) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ sessions: callCount === 1 ? mockSessions : refreshedSessions }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ success: true }),
        });
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      // Perform pin operation
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      // After operation, should show server-added session
      await waitFor(() => {
        expect(screen.getByText('Server Added')).toBeInTheDocument();
      });
    });

    it('should restore prior state from cache after a failed operation', async () => {
      // deleteSession()'s failure path calls rollback(), not revalidate() —
      // a failed mutation restores the pre-mutation cache snapshot rather
      // than syncing against the server, so session3 must still be present
      // (and unpinned, its original state) once the rollback settles.
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { error: 'Delete failed', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 3')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session3Card = screen.getByText('Test Session 3').closest('[data-session]');

      // Try to delete session3
      await user.pointer({ target: session3Card!, keys: '[MouseRight]' });
      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      // After the failed delete rolls back, session3 should still be there.
      await waitFor(() => {
        expect(screen.getByText('Test Session 3')).toBeInTheDocument();
        expect(session3Card).toHaveAttribute('data-pinned', 'false');
      });
    });
  });

  describe('Multiple Concurrent Operations', () => {
    it('should handle operations on different sessions concurrently', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/pin': { success: true },
        '/delete': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();

      // Start pin on session1
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      // Immediately start delete on session3 (different session)
      const session3Card = screen.getByText('Test Session 3').closest('[data-session]');
      await user.pointer({ target: session3Card!, keys: '[MouseRight]' });
      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      // Both operations should succeed
      await waitFor(() => {
        expect(session1Card).toHaveAttribute('data-pinned', 'true');
        expect(screen.queryByText('Test Session 3')).not.toBeInTheDocument();
      });
    });
  });

  describe('Toast Notification Integration', () => {
    beforeEach(() => {
      // Clear mock calls before each test
      vi.clearAllMocks();
    });

    it('should optimistically add a new session when created', async () => {
      // createNewChat() posts to /api/sessions/init (not /api/v1/sessions) and,
      // per the current implementation, does not show a toast on success —
      // it only needs to reflect the optimistic session addition.
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        'POST /api/sessions/init': { name: 'new-session', messages: 0, pinned: false },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      // Create new session via the icon-only "Create new chat" button
      // (it has no visible text, only an aria-label).
      const newButton = screen.getByRole('button', { name: /create new chat/i });
      const user = userEvent.setup();
      await user.click(newButton);

      await waitFor(() => {
        expect(screen.getByText('New Chat')).toBeInTheDocument();
      });
    });

    it('should show success toast when session is deleted', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      await waitFor(() => {
        expect(mockToastContext.success).toHaveBeenCalledWith('Session deleted successfully');
      });
    });

    it('should show error toast when session deletion fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/delete': { error: 'Delete failed', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      await waitFor(() => {
        expect(mockToastContext.error).toHaveBeenCalledWith(expect.stringContaining('Failed to delete session'));
      });
    });

    it('should show success toast when session is renamed', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/rename': { success: true, new_name: 'new-title' },
      });

      vi.mocked(global.prompt).mockReturnValue('New Title');

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const renameButton = await screen.findByText(/rename/i);
      await user.click(renameButton);

      await waitFor(() => {
        expect(mockToastContext.success).toHaveBeenCalledWith('Session renamed successfully');
      });
    });

    it('should show error toast when session rename fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/rename': { error: 'Rename failed', status: 500 },
      });

      vi.mocked(global.prompt).mockReturnValue('New Title');

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const renameButton = await screen.findByText(/rename/i);
      await user.click(renameButton);

      await waitFor(() => {
        expect(mockToastContext.error).toHaveBeenCalledWith(expect.stringContaining('Failed to rename session'));
      });
    });

    it('should show success toast when session is pinned', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/pin': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      await waitFor(() => {
        expect(mockToastContext.success).toHaveBeenCalledWith('Session pinned successfully');
      });
    });

    it('should show success toast when session is unpinned', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/unpin': { success: true },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 2')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session2Card = screen.getByText('Test Session 2').closest('[data-session]');
      await user.pointer({ target: session2Card!, keys: '[MouseRight]' });

      const unpinButton = await screen.findByRole('button', { name: /unpin/i });
      await user.click(unpinButton);

      await waitFor(() => {
        expect(mockToastContext.success).toHaveBeenCalledWith('Session unpinned successfully');
      });
    });

    it('should show error toast when pin toggle fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/pin': { error: 'Pin failed', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const pinButton = await screen.findByRole('button', { name: /pin/i });
      await user.click(pinButton);

      await waitFor(() => {
        expect(mockToastContext.error).toHaveBeenCalledWith(expect.stringContaining('Failed to pin session'));
      });
    });

    it('should show success toast when session is exported', async () => {
      // Mock URL.createObjectURL
      global.URL.createObjectURL = vi.fn(() => 'blob:mock-url');
      global.URL.revokeObjectURL = vi.fn();
      // exportSession() appends a real <a href={downloadUrl}> and calls
      // .click() to trigger the download. happy-dom actually navigates on
      // that click, which corrupts window.location for every test that
      // runs afterward (breaking next/image's URL resolution). Stub it out.
      const anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/export': new Blob(['export data'], { type: 'application/json' }),
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      // "Export" only reveals a format submenu on hover; the actual export
      // is triggered by one of the format buttons inside it.
      const exportLabel = await screen.findByText(/export/i);
      await user.hover(exportLabel);
      const markdownButton = await screen.findByText('Markdown');
      await user.click(markdownButton);

      await waitFor(() => {
        expect(mockToastContext.success).toHaveBeenCalledWith('Session exported successfully');
      });

      anchorClickSpy.mockRestore();
    });

    it('should show error toast when session export fails', async () => {
      mockFetchResponses({
        'GET /api/sessions': { sessions: mockSessions },
        '/export': { error: 'Export failed', status: 500 },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const exportLabel = await screen.findByText(/export/i);
      await user.hover(exportLabel);
      const markdownButton = await screen.findByText('Markdown');
      await user.click(markdownButton);

      await waitFor(() => {
        expect(mockToastContext.error).toHaveBeenCalledWith(expect.stringContaining('Failed to export session'));
      });
    });

    it('should show warning toast when trying to delete the last session', async () => {
      const singleSessionList = [mockSessions[0]];
      mockFetchResponses({
        'GET /api/sessions': { sessions: singleSessionList },
      });

      renderHome();

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });

      const deleteButton = await screen.findByText(/delete/i);
      await user.click(deleteButton);

      await waitFor(() => {
        expect(mockToastContext.warning).toHaveBeenCalledWith('Cannot delete the last session');
      });
    });
  });
});

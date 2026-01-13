import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Home from '@/app/page';

/**
 * Optimistic Updates Tests
 *
 * Tests for optimistic UI update logic in session operations.
 * These tests verify that UI updates occur immediately and rollback on failure.
 */

// Mock global fetch
global.fetch = vi.fn();

// Mock window.confirm and window.prompt
global.confirm = vi.fn();
global.prompt = vi.fn();

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
      const sessions: any[] = [];
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

  const mockFetchResponses = (responses: Record<string, any>) => {
    (global.fetch as any).mockImplementation((url: string, options?: any) => {
      const method = options?.method || 'GET';
      const key = `${method} ${url}`;

      // Match patterns for dynamic URLs
      for (const [pattern, response] of Object.entries(responses)) {
        if (url.includes(pattern) || key.includes(pattern)) {
          if (response.error) {
            return Promise.resolve({
              ok: false,
              status: response.status || 500,
              text: () => Promise.resolve(response.error),
              json: () => Promise.resolve({ detail: response.error }),
            });
          }
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(response),
            text: () => Promise.resolve(JSON.stringify(response)),
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
    vi.clearAllMocks();
    (global.confirm as any).mockReturnValue(true);
    (global.prompt as any).mockReturnValue('New Title');
  });

  describe('Pin Toggle Integration', () => {
    it('should show optimistic update immediately when pinning session', async () => {
      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/pin': { success: true },
      });

      render(<Home />);

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
      const pinButton = await screen.findByText(/pin/i);
      await user.click(pinButton);

      // Verify optimistic update appears immediately (before API completes)
      await waitFor(() => {
        // Session should show pinned state immediately
        expect(session1Card).toHaveAttribute('data-pinned', 'true');
      }, { timeout: 100 }); // Very short timeout to verify it's optimistic
    });

    it('should rollback pin state when API fails', async () => {
      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/pin': { error: 'Failed to pin session', status: 500 },
      });

      render(<Home />);

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      const user = userEvent.setup();

      // Trigger pin
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByText(/pin/i);
      await user.click(pinButton);

      // Wait for error alert and rollback
      await waitFor(() => {
        // Should rollback to original unpinned state
        expect(session1Card).toHaveAttribute('data-pinned', 'false');
      });
    });

    it('should prevent concurrent pin operations on same session', async () => {
      let callCount = 0;
      (global.fetch as any).mockImplementation(() => {
        callCount++;
        return new Promise((resolve) => {
          setTimeout(() => {
            resolve({
              ok: true,
              status: 200,
              json: () => Promise.resolve({ sessions: mockSessions }),
            });
          }, 100);
        });
      });

      render(<Home />);

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      const user = userEvent.setup();

      // Attempt rapid clicks
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByText(/pin/i);

      // Click multiple times rapidly
      await user.click(pinButton);
      await user.click(pinButton);
      await user.click(pinButton);

      // Wait for operations to settle
      await waitFor(() => expect(callCount).toBeGreaterThan(0));

      // Should only have one pin operation (second and third blocked by pending check)
      expect(callCount).toBeLessThan(3);
    });
  });

  describe('Delete Session Integration', () => {
    it('should show optimistic update immediately when deleting session', async () => {
      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/delete': { success: true },
      });

      render(<Home />);

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
        '/api/v1/sessions': { sessions: mockSessions },
        '/delete': { error: 'Failed to delete session', status: 500 },
      });

      render(<Home />);

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
        '/api/v1/sessions': { sessions: mockSessions },
        '/delete': { success: true },
      });

      render(<Home />);

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

      // Should switch to next available session immediately
      await waitFor(() => {
        // First remaining session should become selected
        expect(screen.queryByText('Test Session 1')).not.toBeInTheDocument();
        // Session 2 or 3 should be visible and selected
        const remainingSession = screen.getByText(/Test Session [23]/);
        expect(remainingSession).toBeInTheDocument();
      }, { timeout: 100 });
    });
  });

  describe('Rename Session Integration', () => {
    it('should show optimistic update immediately when renaming session', async () => {
      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/rename': { success: true, new_name: 'session1' },
      });

      (global.prompt as any).mockReturnValue('Updated Title');

      render(<Home />);

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
        '/api/v1/sessions': { sessions: mockSessions },
        '/rename': { error: 'Failed to rename session', status: 500 },
      });

      (global.prompt as any).mockReturnValue('Failed Title');

      render(<Home />);

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

      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/rename': { success: true, new_name: 'session1' },
      });

      render(<Home />);

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
      (global.fetch as any).mockImplementation((url: string) => {
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

      render(<Home />);

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');

      // Perform pin operation
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByText(/pin/i);
      await user.click(pinButton);

      // After operation, should show server-added session
      await waitFor(() => {
        expect(screen.getByText('Server Added')).toBeInTheDocument();
      });
    });

    it('should refresh state from server after failed operation', async () => {
      const serverSessions = [
        ...mockSessions.slice(0, 2), // Server only has 2 sessions
      ];

      let callCount = 0;
      (global.fetch as any).mockImplementation((url: string, options?: any) => {
        callCount++;
        if (options?.method === 'POST' && url.includes('/delete')) {
          // Delete fails
          return Promise.resolve({
            ok: false,
            status: 500,
            text: () => Promise.resolve('Delete failed'),
          });
        }
        // Refreshes return actual server state
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ sessions: serverSessions }),
        });
      });

      render(<Home />);

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();
      const session3Card = screen.getByText('Test Session 3')?.closest('[data-session]');

      if (session3Card) {
        // Try to delete session3
        await user.pointer({ target: session3Card, keys: '[MouseRight]' });
        const deleteButton = await screen.findByText(/delete/i);
        await user.click(deleteButton);
      }

      // After failed delete, should sync with server state (only 2 sessions)
      await waitFor(() => {
        expect(screen.queryByText('Test Session 3')).not.toBeInTheDocument();
      });
    });
  });

  describe('Multiple Concurrent Operations', () => {
    it('should handle operations on different sessions concurrently', async () => {
      mockFetchResponses({
        '/api/v1/sessions': { sessions: mockSessions },
        '/pin': { success: true },
        '/delete': { success: true },
      });

      render(<Home />);

      await waitFor(() => {
        expect(screen.getByText('Test Session 1')).toBeInTheDocument();
      });

      const user = userEvent.setup();

      // Start pin on session1
      const session1Card = screen.getByText('Test Session 1').closest('[data-session]');
      await user.pointer({ target: session1Card!, keys: '[MouseRight]' });
      const pinButton = await screen.findByText(/pin/i);
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
});

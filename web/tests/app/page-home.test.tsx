import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import Home from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';

/**
 * Behavioral coverage for the Home page (src/app/page.tsx): session CRUD with
 * optimistic updates, export, filters, context menu, settings menu, keyboard
 * shortcuts, and offline (background-sync) fallbacks.
 */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: any) => (
    <div style={style} aria-label={props['aria-label']} role={props.role}>
      {Array.from({ length: totalCount }).map((_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

const toast = {
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
};
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => toast,
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// ChatPane is exercised by its own suites; a stub keeps these tests focused on
// page.tsx and lets us drive the onSessionUpdated callback deterministically.
vi.mock('@/app/components/ChatPane', () => ({
  default: ({ sessionName, onSessionUpdated, releaseVersion, scrollToMessageIndex }: any) => (
    <div data-testid="chatpane">
      <span data-testid="chatpane-session">{sessionName}</span>
      <span data-testid="chatpane-release">{releaseVersion ?? 'none'}</span>
      <span data-testid="chatpane-scroll">{String(scrollToMessageIndex)}</span>
      <button onClick={() => onSessionUpdated?.()}>chatpane-refresh</button>
    </div>
  ),
}));

type SessionFixture = {
  name: string;
  title?: string;
  messages?: number;
  pinned?: boolean;
  tags?: string[];
  model?: string;
  modified?: string;
};

const defaultSessions: SessionFixture[] = [
  { name: 'default', title: 'Default', messages: 2, pinned: false, tags: ['work'], model: 'llama3.1:8b' },
  { name: 'second', title: 'Second Chat', messages: 5, pinned: true, tags: ['home'], model: 'qwen2.5:7b' },
  { name: 'third', title: 'Third Chat', messages: 1, pinned: false, tags: [], model: 'llama3.1:8b' },
];

let sessionsPayload: SessionFixture[];
const fetchMock = vi.fn();

/** Route-style fetch mock; individual tests override specific routes. */
const routes: Record<string, (url: string, init?: RequestInit) => Promise<unknown> | unknown> = {};

function resetRoutes() {
  for (const k of Object.keys(routes)) delete routes[k];
}

function jsonRes(body: unknown, ok = true, status = 200, extra: Record<string, unknown> = {}) {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'ERR',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
    blob: () => Promise.resolve(new Blob(['x'])),
    headers: { get: () => null },
    ...extra,
  };
}

function installFetch() {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(routes)) {
      if (url.includes(prefix)) return Promise.resolve(handler(url, init));
    }
    if (url.includes('/api/sessions') && (!init || !init.method || init.method === 'GET')) {
      return Promise.resolve(jsonRes({ sessions: sessionsPayload }));
    }
    if (url.includes('/api/info')) {
      return Promise.resolve(jsonRes({ release_version: '2.0.0' }));
    }
    return Promise.resolve(jsonRes({}));
  });
  global.fetch = fetchMock as unknown as typeof fetch;
}

function renderHome() {
  return render(
    <ThemeProvider>
      <Home />
    </ThemeProvider>
  );
}

async function openContextMenu(sessionTitle: string) {
  const row = await screen.findByText(sessionTitle);
  fireEvent.contextMenu(row);
  return await screen.findByText('Rename');
}

beforeEach(() => {
  vi.clearAllMocks();
  resetRoutes();
  sessionsPayload = JSON.parse(JSON.stringify(defaultSessions));
  installFetch();
  vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
  vi.stubGlobal('prompt', vi.fn().mockReturnValue(null));
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Home page — sessions and layout', () => {
  it('renders sessions, selects one, and passes it to ChatPane', async () => {
    renderHome();
    expect(await screen.findByText('Second Chat')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Second Chat'));
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).toBe('second')
    );
    expect(screen.getByTestId('chatpane-release').textContent).toBe('2.0.0');
  });

  it('shows the empty state when there are no sessions', async () => {
    sessionsPayload = [];
    renderHome();
    expect(await screen.findByText('No sessions found yet.')).toBeInTheDocument();
  });

  it('surfaces an /api/info failure without crashing', async () => {
    routes['/api/info'] = () => jsonRes({}, false, 500);
    renderHome();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.getByTestId('chatpane-release').textContent).toBe('none');
  });

  it('toggles the sidebar via the toolbar button and the Menu button', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    fireEvent.click(screen.getByLabelText(/Toggle sidebar/));
    expect(screen.queryByText('Second Chat')).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Show sidebar/));
    expect(await screen.findByText('Second Chat')).toBeInTheDocument();
  });

  it('refreshes sessions when ChatPane reports an update', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    const before = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/sessions')).length;
    fireEvent.click(screen.getByText('chatpane-refresh'));
    await waitFor(() => {
      const after = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/sessions')).length;
      expect(after).toBeGreaterThan(before);
    });
  });
});

describe('Home page — filters', () => {
  it('filters by model, pinned state, and tag; clear-filters restores', async () => {
    renderHome();
    await screen.findByText('Second Chat');

    const selects = screen.getAllByRole('combobox');
    const modelSelect = selects[0];
    const pinnedSelect = selects[1];

    fireEvent.change(modelSelect, { target: { value: 'qwen2.5:7b' } });
    expect(await screen.findByText('(1 of 3)')).toBeInTheDocument();
    expect(screen.queryByText('Default')).not.toBeInTheDocument();

    fireEvent.change(modelSelect, { target: { value: '' } });
    fireEvent.change(pinnedSelect, { target: { value: 'pinned' } });
    expect(await screen.findByText('(1 of 3)')).toBeInTheDocument();
    expect(screen.getByText('Second Chat')).toBeInTheDocument();

    fireEvent.change(pinnedSelect, { target: { value: 'unpinned' } });
    expect(await screen.findByText('(2 of 3)')).toBeInTheDocument();

    const tagInput = screen.getByPlaceholderText('Filter by tag...');
    fireEvent.change(pinnedSelect, { target: { value: 'all' } });
    fireEvent.change(tagInput, { target: { value: 'WORK' } });
    expect(await screen.findByText('(1 of 3)')).toBeInTheDocument();

    fireEvent.change(tagInput, { target: { value: 'nomatch' } });
    expect(await screen.findByText('No sessions match your filters.')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Clear filters'));
    await waitFor(() =>
      expect(screen.queryByText('No sessions match your filters.')).not.toBeInTheDocument()
    );
    expect(screen.getByText('Default')).toBeInTheDocument();
  });
});

describe('Home page — create new chat', () => {
  it('creates a session optimistically and selects it', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    routes['/api/sessions/init'] = () => jsonRes({ ok: true });
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).toMatch(/^chat-/)
    );
  });

  it('rolls back and toasts on wrapped-error failure', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    routes['/api/sessions/init'] = () =>
      jsonRes({ error: { message: 'quota exceeded' } }, false, 400);
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create new chat: quota exceeded')
    );
    expect(screen.getByTestId('chatpane-session').textContent).toBe('default');
  });

  it('handles legacy string detail and array detail error formats', async () => {
    renderHome();
    await screen.findByText('Second Chat');

    routes['/api/sessions/init'] = () => jsonRes({ detail: 'legacy detail' }, false, 400);
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create new chat: legacy detail')
    );

    routes['/api/sessions/init'] = () =>
      jsonRes({ detail: [{ msg: 'bad name' }, { other: true }] }, false, 400);
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('bad name')
      )
    );
  });

  it('uses the Unknown-error fallback when the body is unparseable and HTTP status when empty', async () => {
    renderHome();
    await screen.findByText('Second Chat');

    routes['/api/sessions/init'] = () =>
      jsonRes({}, false, 500, { json: () => Promise.reject(new Error('bad json')) });
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create new chat: Unknown error')
    );

    routes['/api/sessions/init'] = () => jsonRes({}, false, 502);
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create new chat: HTTP 502')
    );
  });
});

describe('Home page — context menu operations', () => {
  it('opens on right-click, closes on outside click and Escape', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    fireEvent.click(document.body);
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());

    await openContextMenu('Third Chat');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());
  });

  it('deletes a session (confirm accepted) and revalidates', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/delete'] = () => {
      sessionsPayload = sessionsPayload.filter((s) => s.name !== 'third');
      return jsonRes({ ok: true });
    };
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Session deleted successfully'));
    await waitFor(() => expect(screen.queryByText('Third Chat')).not.toBeInTheDocument());
  });

  it('switches selection when deleting the selected session', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    fireEvent.click(screen.getByText('Third Chat'));
    await waitFor(() => expect(screen.getByTestId('chatpane-session').textContent).toBe('third'));
    await openContextMenu('Third Chat');
    routes['/delete'] = () => jsonRes({ ok: true });
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).not.toBe('third')
    );
  });

  it('does nothing when delete confirm is cancelled', async () => {
    (globalThis.confirm as ReturnType<typeof vi.fn>).mockReturnValue(false);
    renderHome();
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByText('Third Chat')).toBeInTheDocument();
  });

  it('blocks deleting the last remaining session', async () => {
    sessionsPayload = [defaultSessions[0]];
    renderHome();
    await openContextMenu('Default');
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('Cannot delete the last session')
    );
  });

  it('rolls back a failed delete', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/delete'] = () => jsonRes('boom', false, 500);
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('Failed to delete session'))
    );
    expect(await screen.findByText('Third Chat')).toBeInTheDocument();
  });

  it('renames a session via prompt and switches selection on filename change', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('Renamed Chat');
    renderHome();
    await screen.findByText('Second Chat');
    fireEvent.click(screen.getByText('Third Chat'));
    await openContextMenu('Third Chat');
    routes['/rename'] = () => jsonRes({ new_name: 'renamed-chat' });
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Session renamed successfully'));
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).toBe('renamed-chat')
    );
  });

  it('skips rename when prompt is cancelled or unchanged', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Rename')); // prompt returns null
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());
    expect(toast.success).not.toHaveBeenCalled();

    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('Third Chat'); // same title
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('rolls back a failed rename', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('New Title');
    renderHome();
    await openContextMenu('Third Chat');
    routes['/rename'] = () => jsonRes({ detail: 'name taken' }, false, 409);
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to rename session: name taken')
    );
    expect(await screen.findByText('Third Chat')).toBeInTheDocument();
  });

  it('pins and unpins with success toasts', async () => {
    renderHome();
    await openContextMenu('Third Chat'); // unpinned → shows Pin
    routes['/pin'] = () => jsonRes({ ok: true });
    fireEvent.click(screen.getByText('Pin'));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Session pinned successfully')
    );

    await openContextMenu('Second Chat'); // pinned → shows Unpin
    routes['/unpin'] = () => jsonRes({ ok: true });
    fireEvent.click(screen.getByText('Unpin'));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Session unpinned successfully')
    );
  });

  it('rolls back a failed pin', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/pin'] = () => jsonRes({}, false, 500);
    fireEvent.click(screen.getByText('Pin'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('Failed to pin session'))
    );
  });

  it('exports a session in each format and honors Content-Disposition', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/export'] = (url: string) =>
      jsonRes('content', true, 200, {
        headers: {
          get: (h: string) =>
            h === 'Content-Disposition' && url.includes('format=json')
              ? 'attachment; filename="custom.json"'
              : null,
        },
      });
    const exportTrigger = screen.getByText('Export');
    fireEvent.mouseEnter(exportTrigger.parentElement as HTMLElement);
    fireEvent.click(screen.getByText('Markdown'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Session exported successfully'));

    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Json'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(2));

    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Html'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(3));
  });

  it('surfaces an export failure', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/export'] = () => jsonRes('denied', false, 403);
    fireEvent.click(screen.getByText('Markdown'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('Failed to export session'))
    );
  });
});

describe('Home page — offline background-sync paths', () => {
  beforeEach(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { controller: {} },
    });
  });

  afterEach(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  });

  it('keeps the optimistic delete and informs when the request was queued', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/delete'] = () => Promise.reject(new TypeError('Failed to fetch'));
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        'You are offline — session deletion will complete when you reconnect'
      )
    );
  });

  it('keeps the optimistic rename when queued', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('Offline Rename');
    renderHome();
    await openContextMenu('Third Chat');
    routes['/rename'] = () => Promise.reject(new TypeError('Failed to fetch'));
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        'You are offline — rename will complete when you reconnect'
      )
    );
  });

  it('keeps the optimistic pin when queued', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/pin'] = () => Promise.reject(new TypeError('Failed to fetch'));
    fireEvent.click(screen.getByText('Pin'));
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        'You are offline — pin will complete when you reconnect'
      )
    );
  });
});

describe('Home page — keyboard shortcuts', () => {
  it('Cmd/Ctrl+K creates a new chat with announcement', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    routes['/api/sessions/init'] = () => jsonRes({ ok: true });
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
    expect(await screen.findByRole('alert')).toHaveTextContent('Creating new chat');
  });

  it('Cmd/Ctrl+F and bare / focus the search; / inside an input does not', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    const search = screen.getByPlaceholderText(/Search sessions & messages/);

    fireEvent.keyDown(document, { key: 'f', metaKey: true });
    expect(document.activeElement).toBe(search);
    expect(screen.getByRole('alert')).toHaveTextContent('Search focused');

    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document, { key: '/' });
    expect(document.activeElement).toBe(search);

    const tagInput = screen.getByPlaceholderText('Filter by tag...');
    tagInput.focus();
    fireEvent.keyDown(tagInput, { key: '/' });
    expect(document.activeElement).toBe(tagInput);
  });

  it('Cmd/Ctrl+/ toggles the sidebar both ways', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    fireEvent.keyDown(document, { key: '/', ctrlKey: true });
    await waitFor(() => expect(screen.queryByText('Second Chat')).not.toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent('Sidebar hidden');
    fireEvent.keyDown(document, { key: '/', ctrlKey: true });
    expect(await screen.findByText('Second Chat')).toBeInTheDocument();
  });

  it('Escape closes an open context menu via the shortcut handler', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByText('Rename')).not.toBeInTheDocument());
  });

  it('clears the shortcut feedback after the announce timeout', async () => {
    vi.useFakeTimers();
    try {
      renderHome();
      routes['/api/sessions/init'] = () => jsonRes({ ok: true });
      fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
      expect(screen.getByRole('alert')).toBeInTheDocument();
      // A second announce clears the pending timeouts first (covers the
      // clearTimeout branches), then the final timers blank both messages.
      fireEvent.keyDown(document, { key: 'f', ctrlKey: true });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Home page — search result navigation', () => {
  it('navigates to a message hit and clears the scroll target after a delay', async () => {
    vi.useFakeTimers();
    try {
      renderHome();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10);
      });
      const search = screen.getByPlaceholderText(/Search sessions & messages/);
      routes['/api/v1/sessions/search'] = () =>
        jsonRes({
          results: [
            {
              session_name: 'second',
              session_title: 'Second Chat',
              message_index: 4,
              role: 'user',
              content_preview: '找到 target message preview',
              timestamp: null,
              matches: 1,
            },
          ],
        });
      fireEvent.focus(search);
      fireEvent.change(search, { target: { value: 'target message' } });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(350);
      });
      fireEvent.click(screen.getByText(/找到/));
      expect(screen.getByTestId('chatpane-session').textContent).toBe('second');
      expect(screen.getByTestId('chatpane-scroll').textContent).toBe('4');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      expect(screen.getByTestId('chatpane-scroll').textContent).toBe('null');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Home page — settings menu and theme', () => {
  it('navigates every admin submenu link and closes the menu each time', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    const user = userEvent.setup();

    const labels = [
      'Dashboard',
      'Configuration Wizard',
      'Resource Usage',
      'Logs',
      'Usage Analytics',
      'Observability',
      'Manage Models',
      'RAG Collections',
      'RAG Playground',
      'Deployment',
      'Canary',
    ];
    for (const label of labels) {
      await user.click(screen.getByRole('button', { name: /settings/i }));
      const adminToggle = await screen.findByRole('button', { name: /admin/i });
      if (adminToggle.getAttribute('aria-expanded') === 'false') {
        await user.click(adminToggle);
      }
      const link = await screen.findByRole('link', { name: new RegExp(label, 'i') });
      await user.click(link);
      await waitFor(() => expect(screen.queryByText('Theme')).not.toBeInTheDocument());
    }
  });

  it('switches theme via the Light and Dark buttons', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /settings/i }));
    await user.click(await screen.findByRole('button', { name: /dark/i }));
    await waitFor(() =>
      expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    );
    await user.click(screen.getByRole('button', { name: /settings/i }));
    await user.click(await screen.findByRole('button', { name: /light/i }));
    await waitFor(() =>
      expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    );
  });

  it('closes the settings menu with Escape', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /settings/i }));
    expect(await screen.findByText('Theme')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByText('Theme')).not.toBeInTheDocument());
  });
});

describe('Home page — inline style handlers (hover/focus sweep)', () => {
  it('executes every element-level mouse/focus style handler', async () => {
    renderHome();
    await screen.findByText('Second Chat');
    // Open the popovers so their handler-carrying elements are mounted.
    fireEvent.contextMenu(screen.getByText('Third Chat'));
    await screen.findByText('Rename');

    const stub = () => ({
      currentTarget: {
        style: { background: '', outline: '' },
        nextElementSibling: {
          style: { display: '' },
          matches: () => false,
        },
      },
    });

    const sweep = () => {
      let invoked = 0;
      document.querySelectorAll('*').forEach((el) => {
        const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
        if (!key) return;
        const props = (el as unknown as Record<string, Record<string, unknown>>)[key];
        for (const name of ['onMouseEnter', 'onMouseLeave', 'onFocus', 'onBlur'] as const) {
          const fn = props?.[name];
          if (typeof fn === 'function') {
            (fn as (e: unknown) => void)(stub());
            invoked++;
          }
        }
      });
      return invoked;
    };

    vi.useFakeTimers();
    try {
      expect(sweep()).toBeGreaterThan(0);
      // The export-submenu leave handler defers its hide check.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(150);
      });
    } finally {
      vi.useRealTimers();
    }

    // Repeat with the settings menu open (its entries carry their own handlers).
    fireEvent.keyDown(document, { key: 'Escape' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /settings/i }));
    await user.click(await screen.findByRole('button', { name: /admin/i }));
    expect(sweep()).toBeGreaterThan(0);
  });
});

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import React from 'react';
import Home, { highlightText } from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { highlightMatches } from '@/components/UnifiedSearch';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: any) => (
    <div style={style} aria-label={props['aria-label']} role={props.role}>
      {Array.from({ length: totalCount }).map((_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => toast,
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('@/app/components/ChatPane', () => ({
  default: ({ sessionName, onSessionUpdated }: any) => (
    <div data-testid="chatpane">
      <span data-testid="chatpane-session">{sessionName}</span>
      <button onClick={() => onSessionUpdated?.()}>chatpane-refresh</button>
    </div>
  ),
}));

let sessionsPayload: Array<Record<string, unknown>>;
const routes: Record<string, (url: string, init?: RequestInit) => unknown> = {};
const fetchMock = vi.fn();

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

async function openContextMenu(title: string) {
  fireEvent.contextMenu(await screen.findByText(title));
  return await screen.findByText('Rename');
}

beforeEach(() => {
  vi.clearAllMocks();
  for (const k of Object.keys(routes)) delete routes[k];
  sessionsPayload = [
    { name: 'default', title: 'Default', pinned: false, tags: [], model: 'm1' },
    { name: 'untitled' }, // no title — exercises title fallbacks
    { name: 'third', title: 'Third Chat', pinned: false, tags: [], model: 'm1' },
  ];
  installFetch();
  vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
  vi.stubGlobal('prompt', vi.fn().mockReturnValue(null));
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('exported helpers', () => {
  it('highlightText returns input unchanged for empty search and marks matches', () => {
    expect(highlightText('abc', '')).toBe('abc');
    const parts = highlightText('aXa', 'x') as React.ReactNode[];
    expect(Array.isArray(parts)).toBe(true);
    render(<div data-testid="ht">{parts}</div>);
    expect(screen.getByTestId('ht').querySelector('mark')?.textContent).toBe('X');
  });

  it('highlightMatches guards the empty query and handles no-match text', () => {
    expect(highlightMatches('plain text', '')).toBe('plain text');
    const { container } = render(<div>{highlightMatches('no hits here', 'zzz')}</div>);
    expect(container.querySelector('mark')).toBeNull();
    expect(container.textContent).toBe('no hits here');
  });
});

describe('Home page — platform and init fallbacks', () => {
  it('shows Mac shortcut labels when the platform is Mac', async () => {
    const original = navigator.platform;
    Object.defineProperty(navigator, 'platform', { configurable: true, value: 'MacIntel' });
    try {
      renderHome();
      expect(await screen.findByLabelText('Create new chat (⌘+K)')).toBeInTheDocument();
    } finally {
      Object.defineProperty(navigator, 'platform', { configurable: true, value: original });
    }
  });

  it('handles a non-Error /api/info rejection', async () => {
    routes['/api/info'] = () => Promise.reject('info exploded');
    renderHome();
    await screen.findByText('Third Chat'); // still renders
  });

  it('falls back to the first session when the list has no default', async () => {
    sessionsPayload = [
      { name: 'alpha', title: 'Alpha' },
      { name: 'beta', title: 'Beta' },
    ];
    renderHome();
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).toBe('alpha')
    );
  });

  it('shows the sessions error state and recovers via retry', async () => {
    let fail = true;
    routes['/api/sessions'] = () =>
      fail ? jsonRes('nope', false, 500) : jsonRes({ sessions: sessionsPayload });
    renderHome();
    expect(await screen.findByText('Failed to load sessions')).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: /Try Again|Retry/i }));
    expect(await screen.findByText('Third Chat')).toBeInTheDocument();
  });
});

describe('Home page — CRUD edge branches', () => {
  it('createNewChat reports a non-Error rejection via String(e)', async () => {
    renderHome();
    await screen.findByText('Third Chat');
    routes['/api/sessions/init'] = () => Promise.reject('raw failure');
    fireEvent.click(screen.getByLabelText(/Create new chat/));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create new chat: raw failure')
    );
  });

  it('delete falls back to "Delete failed" for an empty error body', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/delete'] = () => jsonRes('', false, 500, { text: () => Promise.resolve('') });
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to delete session: Delete failed')
    );
  });

  it('delete reports a non-Error rejection via String(e)', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/delete'] = () => Promise.reject('raw delete');
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to delete session: raw delete')
    );
  });

  it('falls back to default selection when duplicate names all disappear', async () => {
    sessionsPayload = [
      { name: 'dup', title: 'Dup A' },
      { name: 'dup', title: 'Dup B' },
    ];
    renderHome();
    await screen.findByText('Dup A');
    fireEvent.click(screen.getByText('Dup A'));
    await waitFor(() => expect(screen.getByTestId('chatpane-session').textContent).toBe('dup'));
    fireEvent.contextMenu(screen.getByText('Dup A'));
    await screen.findByText('Rename');
    routes['/delete'] = () => jsonRes({ ok: true });
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(screen.getByTestId('chatpane-session').textContent).toBe('default')
    );
  });

  it('rename uses the session name when there is no title and default message when detail missing', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('Fresh Name');
    renderHome();
    await openContextMenu('untitled');
    routes['/rename'] = () => jsonRes({}, false, 500);
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to rename session: Rename failed')
    );
    expect(globalThis.prompt).toHaveBeenCalledWith(
      'Enter new session name or title:',
      'untitled'
    );
  });

  it('rename reports a non-Error rejection via String(e)', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>).mockReturnValue('Another Name');
    renderHome();
    await openContextMenu('Third Chat');
    routes['/rename'] = () => Promise.reject('raw rename');
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to rename session: raw rename')
    );
  });

  it('skips a second rename while one is pending', async () => {
    (globalThis.prompt as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce('Slow Name')
      .mockReturnValueOnce('Slow Name 2');
    renderHome();
    let resolveRename: (v: unknown) => void = () => {};
    routes['/rename'] = () => new Promise((r) => (resolveRename = r));
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Rename'));
    // The optimistic update already renamed the row.
    await openContextMenu('Slow Name');
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(warnSpy).toHaveBeenCalledWith(
        'Operation already in progress for session:',
        'third'
      )
    );
    resolveRename(jsonRes({ new_name: 'third' }));
  });

  it('skips a second pin while one is pending', async () => {
    renderHome();
    let resolvePin: (v: unknown) => void = () => {};
    routes['/pin'] = () => new Promise((r) => (resolvePin = r));
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Pin'));
    // The optimistic update flipped the row to pinned, so the menu now says Unpin.
    await openContextMenu('Third Chat');
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    fireEvent.click(screen.getByText('Unpin'));
    await waitFor(() =>
      expect(warnSpy).toHaveBeenCalledWith(
        'Operation already in progress for session:',
        'third'
      )
    );
    resolvePin(jsonRes({ ok: true }));
  });

  it('pin reports a non-Error rejection via String(e)', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/pin'] = () => Promise.reject('raw pin');
    fireEvent.click(screen.getByText('Pin'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to pin session: raw pin')
    );
  });

  it('routes unexpected synchronous failures to the outer catch (delete/rename/pin)', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderHome();

    (globalThis.confirm as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      throw new Error('confirm exploded');
    });
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'An unexpected error occurred while deleting the session. Please refresh the page.'
      )
    );

    (globalThis.prompt as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      throw new Error('prompt exploded');
    });
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Rename'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'An unexpected error occurred while renaming the session. Please refresh the page.'
      )
    );

    // For pin, the inner catch's toast.error throwing escalates to the outer catch.
    routes['/pin'] = () => jsonRes({}, false, 500);
    toast.error.mockImplementationOnce(() => {
      throw new Error('toast exploded');
    });
    await openContextMenu('Third Chat');
    fireEvent.click(screen.getByText('Pin'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'An unexpected error occurred while toggling pin status. Please refresh the page.'
      )
    );
    expect(errSpy).toHaveBeenCalled();
  });

  it('export succeeds without a filename in Content-Disposition and reports non-Error failures', async () => {
    renderHome();
    await openContextMenu('Third Chat');
    routes['/export'] = () =>
      jsonRes('data', true, 200, {
        headers: { get: () => 'attachment' }, // present but no filename= match
      });
    fireEvent.click(screen.getByText('Markdown'));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Session exported successfully')
    );

    await openContextMenu('Third Chat');
    routes['/export'] = () => Promise.reject('raw export');
    fireEvent.click(screen.getByText('Json'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to export session: raw export')
    );
  });

  it('logs refresh failures from the ChatPane update callback', async () => {
    renderHome();
    await screen.findByText('Third Chat');
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    routes['/api/sessions'] = () => jsonRes('down', false, 500);
    fireEvent.click(screen.getByText('chatpane-refresh'));
    await waitFor(() => expect(errSpy).toHaveBeenCalled());
  });
});

describe('Home page — mobile layout', () => {
  beforeEach(() => {
    (window.matchMedia as ReturnType<typeof vi.fn>).mockImplementation((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  });

  afterEach(() => {
    (window.matchMedia as ReturnType<typeof vi.fn>).mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  });

  it('dismisses the sidebar via the backdrop and when selecting a session', async () => {
    renderHome();
    // On mobile the sidebar starts hidden — open it first.
    fireEvent.click(await screen.findByLabelText(/Show sidebar/));
    await screen.findByText('Third Chat');

    // Backdrop click closes it.
    const backdrop = document.querySelector('div[aria-hidden="true"]') as HTMLElement;
    expect(backdrop).toBeTruthy();
    fireEvent.click(backdrop);
    await waitFor(() => expect(screen.queryByText('Third Chat')).not.toBeInTheDocument());

    // Selecting a session also dismisses the overlay.
    fireEvent.click(screen.getByLabelText(/Show sidebar/));
    fireEvent.click(await screen.findByText('Third Chat'));
    await waitFor(() => expect(screen.queryByText('Default')).not.toBeInTheDocument());
    expect(screen.getByTestId('chatpane-session').textContent).toBe('third');
  });
});

describe('Home page — hidden-sidebar handler sweep', () => {
  it('executes the Menu button focus/blur handlers and null-sibling submenu branch', async () => {
    renderHome();
    await screen.findByText('Third Chat');
    fireEvent.click(screen.getByLabelText(/Toggle sidebar/));
    const menuButton = await screen.findByLabelText(/Show sidebar/);

    const key = Object.keys(menuButton).find((k) => k.startsWith('__reactProps$'))!;
    const props = (menuButton as unknown as Record<string, Record<string, (e: unknown) => void>>)[
      key
    ];
    const stub = { currentTarget: { style: { outline: '', background: '' } } };
    props.onFocus(stub);
    expect(stub.currentTarget.style.outline).toContain('#fff');
    props.onBlur(stub);
    expect(stub.currentTarget.style.outline).toContain('transparent');

    // Export-submenu handlers with a null nextElementSibling (guard branch).
    fireEvent.click(screen.getByLabelText(/Show sidebar/));
    fireEvent.contextMenu(await screen.findByText('Third Chat'));
    await screen.findByText('Export');
    const exportRow = screen.getByText('Export').closest('div')?.parentElement as HTMLElement;
    const exportKey = Object.keys(exportRow).find((k) => k.startsWith('__reactProps$'));
    if (exportKey) {
      const exportProps = (
        exportRow as unknown as Record<string, Record<string, (e: unknown) => void>>
      )[exportKey];
      const nullSibStub = {
        currentTarget: { style: { background: '' }, nextElementSibling: null },
      };
      exportProps.onMouseEnter?.(nullSibStub);
      exportProps.onMouseLeave?.(nullSibStub);
    }
  });
});

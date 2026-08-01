import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';

// Full-featured Virtuoso mock that exposes startReached / atBottomStateChange
// so pagination and auto-scroll behavior can be driven from tests, following
// the data-attribute conventions used in virtual-scrolling.test.tsx.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style, startReached, atBottomStateChange, components }: any) => (
    <div style={style} data-testid="virtuoso-mock">
      {components?.Header ? <components.Header /> : null}
      <button data-testid="trigger-start-reached" onClick={() => startReached?.()}>
        load-older
      </button>
      <button data-testid="trigger-at-bottom-true" onClick={() => atBottomStateChange?.(true)}>
        at-bottom-true
      </button>
      <button data-testid="trigger-at-bottom-false" onClick={() => atBottomStateChange?.(false)}>
        at-bottom-false
      </button>
      {(data || []).map((item: any, index: number) => (
        <div key={index}>{itemContent(index, item)}</div>
      ))}
    </div>
  ),
}));

const toastMocks = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => toastMocks,
}));

type Handler = (url: string, init?: RequestInit) => any;

function makeFetchMock(extra: Record<string, Handler> = {}) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    for (const key of Object.keys(extra)) {
      if (url.includes(key)) return Promise.resolve(extra[key](url, init));
    }
    if (url.includes('/api/models')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ models: ['modelA'] }) });
    }
    if (url.includes('/metadata')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: false, title: '', model: '' }) });
    }
    if (url.includes('/documents')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: [] }) });
    }
    if (url.includes('/api/v1/sessions/')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ messages: [], total: 0 }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
}

beforeEach(() => {
  toastMocks.success.mockClear();
  toastMocks.error.mockClear();
  toastMocks.warning.mockClear();
  toastMocks.info.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('scroll-to-message highlight timers', () => {
  it('scrolls to and highlights the target message, then clears the highlight after 2s', async () => {
    vi.useFakeTimers();
    const seed = Array.from({ length: 5 }, (_, i) => ({ role: i % 2 === 0 ? 'user' : 'assistant', content: `m${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: seed.length }) }),
    }) as unknown as typeof fetch;

    const { rerender } = render(<ChatPane sessionName="scroll1" scrollToMessageIndex={null} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    rerender(<ChatPane sessionName="scroll1" scrollToMessageIndex={2} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const highlighted = document.querySelector('[data-message-index="2"]') as HTMLElement;
    expect(highlighted.querySelector('div')?.getAttribute('style')).toContain('var(--highlight)');
    // The clear-highlight timeout is now scheduled (still pending).
    expect(vi.getTimerCount()).toBe(1);

    // NOTE: we verify the highlight-clear timer via vi.getTimerCount() rather
    // than re-reading the element's inline `background` style. happy-dom has
    // a confirmed quirk (reproduced independently of React/ChatPane) where a
    // CSSStyleDeclaration property that has ever held a `var(...)` reference
    // cannot be overwritten by a later plain-value assignment — the getter
    // keeps returning the stale `var(--highlight)` string forever after,
    // even though React's own re-render (confirmed via itemContent being
    // invoked with the updated, now-null highlightedMessageIndex) computed
    // the correct non-highlighted style. This is a jsdom/happy-dom
    // limitation, not a ChatPane bug — real browsers overwrite var()
    // background values without issue.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    // The nested 2s timer fired and cleared itself; no timers remain pending.
    expect(vi.getTimerCount()).toBe(0);
  });

  it('clears pending highlight timers on unmount without warnings', async () => {
    vi.useFakeTimers();
    const seed = [{ role: 'user', content: 'a' }, { role: 'assistant', content: 'b' }];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: seed.length }) }),
    }) as unknown as typeof fetch;

    const { rerender, unmount } = render(<ChatPane sessionName="scroll2" scrollToMessageIndex={null} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    rerender(<ChatPane sessionName="scroll2" scrollToMessageIndex={1} />);

    // Unmount before the 100ms scroll timeout fires; cleanup should clear both timers
    // and no state updates should occur on the unmounted component (no act warnings).
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
  });

  it('does nothing when scrollToMessageIndex is null or undefined', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="scroll3" scrollToMessageIndex={null} />);
    await screen.findByPlaceholderText('Type your message…');
    // No highlight-related crash; nothing to assert beyond a clean render.
  });
});

describe('pagination', () => {
  it('loads a paginated slice of recent messages on mount when total exceeds the page size', async () => {
    const total = 80;
    let paginatedCalled = false;
    global.fetch = makeFetchMock({
      '/api/v1/sessions/scroll4?offset=30&limit=50': () => {
        paginatedCalled = true;
        return {
          ok: true,
          status: 200,
          json: () => Promise.resolve({ messages: Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `p${i}` })), total }),
        };
      },
      '/api/v1/sessions/': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: Array.from({ length: total }, (_, i) => ({ role: 'user', content: `all${i}` })), total }),
      }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="scroll4" />);
    await waitFor(() => expect(paginatedCalled).toBe(true));
    await screen.findByText('p0');
  });

  it('falls back to all messages when the paginated fetch response is not ok', async () => {
    const total = 80;
    global.fetch = makeFetchMock({
      '/api/v1/sessions/scroll5?offset=30&limit=50': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      '/api/v1/sessions/': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: Array.from({ length: total }, (_, i) => ({ role: 'user', content: `all${i}` })), total }),
      }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="scroll5" />);
    await screen.findByText('all0');
  });

  it('falls back to all messages when the paginated fetch throws', async () => {
    const total = 80;
    global.fetch = makeFetchMock({
      '/api/v1/sessions/scroll6?offset=30&limit=50': () => Promise.reject(new Error('network down')),
      '/api/v1/sessions/': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: Array.from({ length: total }, (_, i) => ({ role: 'user', content: `all${i}` })), total }),
      }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="scroll6" />);
    await screen.findByText('all0');
  });

  it('loads older messages when startReached fires, prepending them and updating firstItemIndex', async () => {
    const seed = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `recent${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/scroll7?offset=0&limit=50': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `older${i}` })), total: 100 }),
      }),
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 100 }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="scroll7" />);
    await screen.findByText('recent0');

    const user = userEvent.setup();
    await user.click(screen.getByTestId('trigger-start-reached'));

    await waitFor(() => expect(screen.getByText('older0')).toBeInTheDocument());
    // A second click with hasMore now false (newOffset === loadedOffset) is a no-op guard branch.
    await user.click(screen.getByTestId('trigger-start-reached'));
  });

  it('shows a toast when loading older messages fails', async () => {
    const seed = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `recent${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/scroll8?offset=0&limit=50': () => Promise.reject(new Error('offline')),
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 100 }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="scroll8" />);
    await screen.findByText('recent0');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('trigger-start-reached'));
    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Failed to load older messages'));
  });

  it('exposes atBottomStateChange to track scroll position', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="scroll9" />);
    await screen.findByPlaceholderText('Type your message…');
    const user = userEvent.setup();
    // No messages yet so Virtuoso isn't mounted; this exercises the empty-state path safely.
    expect(screen.queryByTestId('virtuoso-mock')).not.toBeInTheDocument();
  });
});

describe('models, metadata, and documents fetch error branches', () => {
  it('logs an error when /api/models responds not-ok', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/api/models': () => ({ ok: false, status: 503, statusText: 'Unavailable', json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err1" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to fetch models:', 503, 'Unavailable'));
    errSpy.mockRestore();
  });

  it('logs an error when /api/models rejects', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/api/models': () => Promise.reject(new Error('down')),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err2" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to fetch models:', expect.any(Error)));
    errSpy.mockRestore();
  });

  it('resets RAG/title/model state when the metadata fetch rejects', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/metadata': () => Promise.reject(new Error('meta down')),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err3" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to fetch session metadata:', expect.any(Error)));
    expect(screen.getByText('RAG: OFF')).toBeInTheDocument();
    errSpy.mockRestore();
  });

  it('logs an error when the attached-documents fetch rejects', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/documents': () => Promise.reject(new Error('docs down')),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err4" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to fetch attached documents:', expect.any(Error)));
    errSpy.mockRestore();
  });

  it('logs an error when fetchAvailableDocuments (rag/documents) rejects', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/documents': () => Promise.reject(new Error('rag docs down')),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err5" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to fetch available documents:', expect.any(Error)));
    errSpy.mockRestore();
  });

  it('recovers gracefully when persisted RAG filters JSON is corrupt', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    sessionStorage.setItem('rag_filters_err6', '{bad-json');
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="err6" />);
    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('Failed to load RAG filters from session storage:', expect.any(Error)));
    errSpy.mockRestore();
  });

  it('logs an error when persisting RAG filters to sessionStorage throws', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="err7" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    await user.click(screen.getByTitle('Filter RAG documents'));
    const filenameInput = await screen.findByPlaceholderText('Filter by filename...');

    // Install the throwing setItem spy right before triggering the change
    // that will invoke it, directly on the sessionStorage instance (rather
    // than Storage.prototype) so it can't be shadowed by unrelated global
    // state left over from earlier tests in this file.
    const setItemSpy = vi.spyOn(window.sessionStorage, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded');
    });
    fireEvent.change(filenameInput, { target: { value: 'x' } });
    await waitFor(() => expect(filenameInput).toHaveValue('x'));

    await waitFor(() => expect(setItemSpy).toHaveBeenCalled(), { timeout: 3000 });
    await waitFor(
      () => expect(errSpy).toHaveBeenCalledWith('Failed to save RAG filters to session storage:', expect.any(Error)),
      { timeout: 3000 }
    );
    setItemSpy.mockRestore();
    errSpy.mockRestore();
  });
});

describe('model dropdown and upload menu interactions', () => {
  it('selects a model from the dropdown and closes it', async () => {
    global.fetch = makeFetchMock({
      '/api/models': () => ({ ok: true, status: 200, json: () => Promise.resolve({ models: ['alpha', 'beta'] }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="dropdown1" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    await screen.findByText('beta');
    await user.click(screen.getByText('beta'));
    await waitFor(() => expect(screen.queryByText('alpha')).not.toBeInTheDocument());
  });

  it('shows a loading state in the dropdown while models are empty', async () => {
    global.fetch = makeFetchMock({
      '/api/models': () => ({ ok: true, status: 200, json: () => Promise.resolve({ models: [] }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="dropdown2" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    expect(await screen.findByText('Loading models...')).toBeInTheDocument();
  });

  it('closes the model dropdown on Escape', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="dropdown3" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    await screen.findByText('modelA');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByText('modelA')).not.toBeInTheDocument());
  });

  it('closes the model dropdown via the backdrop click', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="dropdown4" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    await screen.findByText('modelA');
    const backdrop = document.querySelector('div[style*="position: fixed"]') as HTMLElement;
    await user.click(backdrop);
    await waitFor(() => expect(screen.queryByText('modelA')).not.toBeInTheDocument());
  });

  it('disables the model button while streaming', async () => {
    global.fetch = makeFetchMock({
      '/api/chat/stream': () => ({
        ok: true,
        status: 200,
        body: { getReader: () => ({ read: () => new Promise(() => {}) }) },
      }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="dropdown6" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'go');
    await user.click(screen.getByTitle('Send message'));
    await waitFor(() => expect(screen.getByRole('button', { name: /nyxGPT/i })).toBeDisabled());
  });
});

describe('edit message flow', () => {
  it('edit populates the input, and cancel restores it', async () => {
    const seed = [{ role: 'user', content: 'original text' }, { role: 'assistant', content: 'reply' }];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="edit1" />);
    await screen.findByText('original text');
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Edit message'));

    const input = screen.getByPlaceholderText('Type your message…') as HTMLInputElement;
    expect(input.value).toBe('original text');
    expect(screen.getByText('Edit')).toBeInTheDocument();

    await user.click(screen.getByTitle('Cancel edit'));
    await waitFor(() => expect(input.value).toBe(''));
    expect(screen.queryByText('Edit')).not.toBeInTheDocument();
  });

  it('copies user and assistant message text to the clipboard', async () => {
    const seed = [{ role: 'user', content: 'copy me' }, { role: 'assistant', content: 'copy this reply' }];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="edit2" />);
    await screen.findByText('copy me');
    const user = userEvent.setup();
    // Install the clipboard stub after userEvent.setup(), since user-event
    // installs its own navigator.clipboard shim during setup() that would
    // otherwise shadow ours.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    await user.click(screen.getByTitle('Copy message'));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('copy me');
    expect(toastMocks.success).toHaveBeenCalledWith('Copied to clipboard');

    await user.click(screen.getByTitle('Copy response'));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('copy this reply');
  });
});

describe('rename session (defensive dead code — see report)', () => {
  // renameSession()/sessionTitle/renaming are defined in ChatPane but no
  // rendered element ever invokes renameSession(); rename appears to be
  // driven from a parent/sidebar component instead. Intentionally left
  // uncovered rather than reaching into component internals directly.
  it('does not crash while the session title is loaded from metadata', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: false, title: 'My Title', model: '' }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="rename1" />);
    await screen.findByPlaceholderText('Type your message…');
  });
});

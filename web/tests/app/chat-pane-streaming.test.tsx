import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';
import type { VirtuosoMockProps } from '../mocks/virtuoso';

// Render every item directly (no real virtualization) so message content is
// queryable in tests, following the pattern in chat-pane-models.test.tsx.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style }: VirtuosoMockProps) => (
    <div style={style}>
      {(data || []).map((item: unknown, index: number) => (
        <div key={index}>{itemContent(index, item)}</div>
      ))}
    </div>
  ),
}));

const toastMocks = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => toastMocks,
}));

/** Builds a fake fetch Response whose body streams the given raw SSE text as a single chunk. */
function sseResponse(raw: string) {
  let sent = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read: async () => {
            if (!sent) {
              sent = true;
              return { value: new TextEncoder().encode(raw), done: false };
            }
            return { value: undefined, done: true };
          },
        };
      },
    },
  };
}

function rejectingBodyResponse() {
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read: async () => {
            throw new Error('stream read failure');
          },
        };
      },
    },
  };
}

type FetchOverride = (url: string, init?: RequestInit) => unknown;

function makeFetchMock(streamHandler?: FetchOverride, extra?: Record<string, FetchOverride>) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url === '/api/chat/stream' && streamHandler) {
      return Promise.resolve(streamHandler(url, init));
    }
    if (extra) {
      for (const key of Object.keys(extra)) {
        if (url.includes(key)) return Promise.resolve(extra[key](url, init));
      }
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

describe('ChatPane send() SSE streaming', () => {
  it('parses every SSE event type on a fresh session (auto-title, heartbeat/metadata/rag/text/retry/done)', async () => {
    const raw = [
      'event: heartbeat',
      'data:',
      '',
      'event: metadata',
      'data: {"session":"s","model":"m"}',
      '',
      'event: metadata',
      'data: not-json',
      '',
      'event: rag_metadata',
      'data: {"type":"rag_metadata","chunks":[{"text":"chunk one","score":0.5,"similarity_score":0.8,"doc_id":"doc1","chunk_id":2}]}',
      '',
      'event: rag_context',
      'data: not-json',
      '',
      'event: rag_context',
      'data: {"type":"other"}',
      '',
      'event: text',
      'data: {"content":"Hello "}',
      '',
      'event: message',
      'data: {"content":"World"}',
      '',
      'event: text',
      'data: not-json',
      '',
      'event: unhandled_custom',
      'data: {"foo":"bar"}',
      '',
      'event: retry',
      'data: {"retry":100}',
      '',
      'event: retry',
      'data:',
      '',
      'event: done',
      'data: {"ok":true}',
      '',
      '',
    ].join('\n');

    const onSessionUpdated = vi.fn();
    global.fetch = makeFetchMock(() => sseResponse(raw), {
      '/title': () => ({ ok: true, status: 200, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="s1" onSessionUpdated={onSessionUpdated} />);

    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'Hi there');
    await user.click(screen.getByTitle('Send message'));

    // Optimistic user message appended immediately.
    expect(await screen.findByText('Hi there')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('Hello World')).toBeInTheDocument();
    });

    // RAG citations block rendered because rag_metadata chunks arrived.
    await waitFor(() => {
      expect(screen.getByText(/1 RAG source retrieved/)).toBeInTheDocument();
    });

    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
  });

  it('shows an explicit "no sources retrieved" indicator when a live rag_metadata event carries zero chunks (#3585)', async () => {
    const raw = [
      'event: rag_metadata',
      'data: {"type":"rag_metadata","chunks":[]}',
      '',
      'event: text',
      'data: {"content":"No matches found."}',
      '',
      'event: done',
      'data: {}',
      '',
      '',
    ].join('\n');

    global.fetch = makeFetchMock(() => sseResponse(raw)) as unknown as typeof fetch;

    render(<ChatPane sessionName="s-zero-rag" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'anything');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(screen.getByText('No matches found.')).toBeInTheDocument());
    expect(screen.getByText('No RAG sources retrieved for this reply')).toBeInTheDocument();
  });

  it('always sends the active session name in the /api/chat/stream request body (#3459)', async () => {
    // Regression coverage for #3459: the send path must forward the
    // component's active `sessionName` on every request (including
    // "default") so the server appends to the same session instead of the
    // client silently drifting to a different one.
    const raw = ['event: done', 'data: {"ok":true}', '', ''].join('\n');

    let capturedInit: RequestInit | undefined;
    global.fetch = makeFetchMock((_url, init) => {
      capturedInit = init;
      return sseResponse(raw);
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="default" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'session field check');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(capturedInit).toBeDefined());
    const body = JSON.parse(capturedInit!.body as string);
    expect(body.session).toBe('default');
  });

  it('handles the error SSE event with a parseable payload', async () => {
    const raw = ['event: error', 'data: {"error":"boom"}', '', ''].join('\n');
    global.fetch = makeFetchMock(() => sseResponse(raw)) as unknown as typeof fetch;

    render(<ChatPane sessionName="s2" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'trigger error');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Error: boom'));
  });

  it('handles the error SSE event with an unparseable payload', async () => {
    const raw = ['event: error', 'data: not-json', '', ''].join('\n');
    global.fetch = makeFetchMock(() => sseResponse(raw)) as unknown as typeof fetch;

    render(<ChatPane sessionName="s3" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'trigger error 2');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Streaming error'));
  });

  it('shows an HTTP error when the response is not ok', async () => {
    global.fetch = makeFetchMock(() => ({ ok: false, status: 500 })) as unknown as typeof fetch;

    render(<ChatPane sessionName="s4" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'will fail');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => {
      expect(screen.getByText(/Error: HTTP 500/)).toBeInTheDocument();
    });
    expect(screen.getByText(/\[error\] HTTP 500/)).toBeInTheDocument();
  });

  it('shows an error when the response has no body (streaming unsupported)', async () => {
    global.fetch = makeFetchMock(() => ({ ok: true, status: 200, body: null })) as unknown as typeof fetch;

    render(<ChatPane sessionName="s5" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'no body');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => {
      expect(screen.getAllByText(/No response body/).length).toBeGreaterThan(0);
    });
  });

  it('surfaces a reader read failure as an assistant error message', async () => {
    global.fetch = makeFetchMock(() => rejectingBodyResponse()) as unknown as typeof fetch;

    render(<ChatPane sessionName="s6" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'reader fails');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => {
      expect(screen.getAllByText(/stream read failure/).length).toBeGreaterThan(0);
    });
  });

  it('truncates a long first message into an auto-title and warns when title save fails', async () => {
    const raw = ['event: done', 'data: {}', '', ''].join('\n');
    let titleCalled = false;
    let capturedTitleBody: Record<string, unknown> | null = null;
    global.fetch = makeFetchMock(() => sseResponse(raw), {
      '/title': (_url, init) => {
        titleCalled = true;
        capturedTitleBody = init?.body ? JSON.parse(init.body as string) : null;
        return Promise.reject(new Error('title save failed'));
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="s7" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    const longText = 'x'.repeat(80);
    await user.type(input, longText);
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(titleCalled).toBe(true));
    expect(capturedTitleBody.title.length).toBe(50); // 47 chars + '...'
    expect(capturedTitleBody.title.endsWith('...')).toBe(true);
  });

  it('does not auto-title when it is not the first message', async () => {
    const raw = ['event: done', 'data: {}', '', ''].join('\n');
    let titleCalled = false;
    global.fetch = makeFetchMock(() => sseResponse(raw), {
      '/title': () => {
        titleCalled = true;
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      },
      '/api/v1/sessions/': () => Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: [{ role: 'user', content: 'earlier' }, { role: 'assistant', content: 'reply' }], total: 2 }),
      }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="s8" />);
    const user = userEvent.setup();
    await screen.findByText('earlier');
    const input = screen.getByPlaceholderText('Type your message…');
    await user.type(input, 'second message');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => screen.getByText('second message'));
    expect(titleCalled).toBe(false);
  });

  it('does not throw when onSessionUpdated is not provided', async () => {
    const raw = ['event: done', 'data: {}', '', ''].join('\n');
    global.fetch = makeFetchMock(() => sseResponse(raw)) as unknown as typeof fetch;

    render(<ChatPane sessionName="s9" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'no callback');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => screen.getByText('no callback'));
  });

  it('ignores empty submissions and does not call fetch for the stream endpoint', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="s10" />);
    const user = userEvent.setup();
    await screen.findByPlaceholderText('Type your message…');
    const sendBtn = screen.getByTitle('Send message');
    expect(sendBtn).toBeDisabled();
    await user.click(sendBtn);
    expect(vi.mocked(global.fetch).mock.calls.some((c: unknown[]) => c[0] === '/api/chat/stream')).toBe(false);
  });

  it('stop() aborts the in-flight request', async () => {
    let abortedSignal: AbortSignal | undefined;
    global.fetch = makeFetchMock((_url, init) => {
      abortedSignal = init?.signal as AbortSignal;
      return new Promise(() => {
        /* never resolves until aborted */
      });
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="s11" />);
    const user = userEvent.setup();
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'stop me');
    await user.click(screen.getByTitle('Send message'));

    const stopBtn = await screen.findByTitle('Stop generating');
    await user.click(stopBtn);

    await waitFor(() => expect(abortedSignal?.aborted).toBe(true));
  });
});

describe('ChatPane handleRegenerate() SSE streaming', () => {
  function seedMessages(messages: unknown[]) {
    return {
      '/api/v1/sessions/': () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ messages, total: messages.length }) }),
    };
  }

  it('regenerates a response and re-parses all SSE event types', async () => {
    const raw = [
      'event: heartbeat',
      'data:',
      '',
      'event: rag_metadata',
      'data: {"type":"rag_metadata","chunks":[{"text":"c","score":0.2}]}',
      '',
      'event: text',
      'data: {"content":"Regenerated answer"}',
      '',
      'event: retry',
      'data: {"n":1}',
      '',
      'event: done',
      'data: {}',
      '',
      '',
    ].join('\n');

    const onSessionUpdated = vi.fn();
    global.fetch = makeFetchMock(
      () => sseResponse(raw),
      seedMessages([
        { role: 'user', content: 'Original question' },
        { role: 'assistant', content: 'Old response' },
      ])
    ) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen1" onSessionUpdated={onSessionUpdated} />);
    await screen.findByText('Old response');

    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => {
      expect(screen.getByText('Regenerated answer')).toBeInTheDocument();
    });
    expect(screen.queryByText('Old response')).not.toBeInTheDocument();
    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
  });

  it('shows a toast when there is no preceding user message to regenerate from', async () => {
    global.fetch = makeFetchMock(
      undefined,
      seedMessages([{ role: 'assistant', content: 'Orphan assistant message' }])
    ) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen2" />);
    await screen.findByText('Orphan assistant message');

    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    expect(toastMocks.error).toHaveBeenCalledWith('No user message found to regenerate from');
  });

  it('surfaces an HTTP error from regenerate', async () => {
    global.fetch = makeFetchMock(
      () => ({ ok: false, status: 503 }),
      seedMessages([
        { role: 'user', content: 'Q' },
        { role: 'assistant', content: 'A' },
      ])
    ) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen3" />);
    await screen.findByText('A');
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => {
      expect(screen.getByText(/\[error\] HTTP 503/)).toBeInTheDocument();
    });
  });

  it('surfaces a missing-body error from regenerate', async () => {
    global.fetch = makeFetchMock(
      () => ({ ok: true, status: 200, body: null }),
      seedMessages([
        { role: 'user', content: 'Q2' },
        { role: 'assistant', content: 'A2' },
      ])
    ) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen4" />);
    await screen.findByText('A2');
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => {
      expect(screen.getAllByText(/No response body/).length).toBeGreaterThan(0);
    });
  });
});

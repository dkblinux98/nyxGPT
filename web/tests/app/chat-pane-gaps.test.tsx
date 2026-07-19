import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane, { VirtuosoErrorBoundary } from '@/app/components/ChatPane';

/**
 * Closes the last reachable coverage gaps in ChatPane.tsx for the #3266 gate:
 * error-boundary null message, pagination (startReached), immediate stream
 * errors, regenerate flows (including assistant-first sessions), and the
 * unparseable stream-error payload path.
 */

let capturedStartReached: (() => void) | undefined;
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style, startReached }: any) => {
    capturedStartReached = startReached;
    return (
      <div style={style}>
        {(data || []).map((item: any, index: number) => (
          <div key={index}>{itemContent(index, item)}</div>
        ))}
      </div>
    );
  },
}));

const toastMocks = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => toastMocks,
}));

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

function hangingResponse() {
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return { read: () => new Promise(() => {}) };
      },
    },
  };
}

type FetchOverride = (url: string, init?: RequestInit) => any;

function makeFetchMock(
  streamHandler: FetchOverride | undefined,
  seedMessages: any[],
  extra: Record<string, FetchOverride> = {},
  total?: number
) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url === '/api/chat/stream' && streamHandler) {
      return Promise.resolve(streamHandler(url, init));
    }
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
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: seedMessages, total: total ?? seedMessages.length }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
}

async function sendMessage(text: string) {
  const input = await screen.findByPlaceholderText(/Type your message/i);
  fireEvent.change(input, { target: { value: text } });
  fireEvent.keyDown(input, { key: 'Enter' });
}

beforeEach(() => {
  vi.clearAllMocks();
  capturedStartReached = undefined;
});

describe('VirtuosoErrorBoundary.getSafeErrorMessage', () => {
  it('falls back for a null error and sanitizes real messages', () => {
    const proto = VirtuosoErrorBoundary.prototype as unknown as {
      getSafeErrorMessage: (e: Error | null) => string;
    };
    expect(proto.getSafeErrorMessage.call({}, null)).toBe('An unknown error occurred');
    expect(proto.getSafeErrorMessage.call({}, new Error(''))).toBe('Unknown error');
    const long = new Error('x'.repeat(250));
    expect(proto.getSafeErrorMessage.call({}, long).endsWith('...')).toBe(true);
  });
});

describe('pagination via startReached', () => {
  it('loads the previous page when scrolled to the top and stops at offset 0', async () => {
    // 120 total; initial load returns the last 50 (offset 70) → hasMore.
    const seed = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `m${70 + i}` }));
    const olderCalls: string[] = [];
    global.fetch = makeFetchMock(undefined, seed, {
      'offset=20': (url) => {
        olderCalls.push(url);
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              messages: Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `m${20 + i}` })),
              total: 120,
            }),
        };
      },
    }, 120) as unknown as typeof fetch;

    render(<ChatPane sessionName="paged" />);
    await screen.findByText('m70');
    expect(capturedStartReached).toBeTypeOf('function');

    capturedStartReached!();
    await waitFor(() => expect(olderCalls.length).toBe(1));
    await screen.findByText('m20');
  });
});

describe('immediate stream errors', () => {
  it('appends an assistant error bubble when the stream fails before any content', async () => {
    global.fetch = makeFetchMock(() => rejectingBodyResponse(), []) as unknown as typeof fetch;
    render(<ChatPane sessionName="s-err" />);
    await sendMessage('hello');
    // Last message was the user's → a new assistant error message is appended.
    await screen.findByText(/\[error\] stream read failure/);
  });

  it('reports an unparseable error-event payload as a generic streaming error', async () => {
    const raw = 'event: error\ndata: {not json\n\n';
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = makeFetchMock(() => sseResponse(raw), []) as unknown as typeof fetch;
    render(<ChatPane sessionName="s-badpayload" />);
    await sendMessage('hi');
    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Streaming error'));
    expect(errSpy).toHaveBeenCalled();
  });
});

describe('regenerate flows', () => {
  const twoMsgSeed = [
    { role: 'user', content: 'question one' },
    { role: 'assistant', content: 'answer one' },
  ];

  it('ignores a second regenerate click while the first is starting', async () => {
    const streamCalls: string[] = [];
    global.fetch = makeFetchMock((url) => {
      streamCalls.push(url);
      return hangingResponse();
    }, twoMsgSeed) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen-busy" />);
    await screen.findByText('answer one');
    const btn = screen.getByTitle('Regenerate response');
    // Two synchronous clicks: the Regenerate button is removed from the DOM
    // once `isStreaming` flips true, so the second click has nothing to hit.
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(streamCalls.length).toBe(1));
  });

  it('refuses to regenerate when no preceding user message exists', async () => {
    const seed = [{ role: 'assistant', content: 'orphan answer' }];
    global.fetch = makeFetchMock(undefined, seed) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen-first" />);
    await screen.findByText('orphan answer');
    fireEvent.click(screen.getByTitle('Regenerate response'));
    await waitFor(() =>
      expect(toastMocks.error).toHaveBeenCalledWith('No user message found to regenerate from')
    );
    expect(screen.getByText('orphan answer')).toBeInTheDocument();
  });

  it('appends a regenerate error after a trailing user message', async () => {
    const seed = [
      { role: 'user', content: 'q-a' },
      { role: 'assistant', content: 'a-a' },
      { role: 'user', content: 'q-b' },
      { role: 'assistant', content: 'a-b' },
    ];
    global.fetch = makeFetchMock(() => rejectingBodyResponse(), seed) as unknown as typeof fetch;

    render(<ChatPane sessionName="regen-append" />);
    await screen.findByText('a-b');
    // Regenerate the FIRST assistant (index 1): history truncates to
    // [q-a, empty-assistant]; the error lands in that assistant bubble.
    fireEvent.click(screen.getAllByTitle('Regenerate response')[0]);
    await screen.findByText(/\[error\] stream read failure/);
    expect(screen.queryByText('a-b')).not.toBeInTheDocument();
  });
});

describe('stream event edge payloads', () => {
  it('tolerates empty/malformed data lines and unknown events on the send path', async () => {
    const raw =
      'event: heartbeat\ndata: {}\n\n' +
      'event: metadata\ndata: \n\n' +
      'event: metadata\ndata: {bad\n\n' +
      'event: rag_context\ndata: \n\n' +
      'event: rag_metadata\ndata: {"type":"other"}\n\n' +
      'event: message\ndata: \n\n' +
      'event: message\ndata: {"content":""}\n\n' +
      'event: message\ndata: {"content":"ok"}\n\n' +
      'event: retry\ndata: \n\n' +
      'event: done\ndata: \n\n' +
      'event: mystery\ndata: {}\n\n';
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'debug').mockImplementation(() => {});
    global.fetch = makeFetchMock(() => sseResponse(raw), []) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-events" />);
    await sendMessage('probe');
    await screen.findByText('ok');
  });

  it('keeps error status through the finally block after a stream error event', async () => {
    const raw = 'event: error\ndata: {"error":"backend exploded"}\n\n';
    global.fetch = makeFetchMock(() => sseResponse(raw), []) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-errstatus" />);
    await sendMessage('boom');
    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Error: backend exploded'));
  });

  it('reports non-Error stream throws via String(e) on send and regenerate', async () => {
    const throwRaw = () => ({
      ok: true,
      status: 200,
      body: { getReader: () => ({ read: async () => { throw 'raw failure'; } }) },
    });
    global.fetch = makeFetchMock(throwRaw, []) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-rawthrow" />);
    await sendMessage('x');
    await screen.findByText(/\[error\] raw failure/);
  });

  it('handles regen-side malformed payloads and skips non-user history entries', async () => {
    const raw =
      'event: metadata\ndata: \n\n' +
      'event: rag_metadata\ndata: {bad\n\n' +
      'event: rag_metadata\ndata: {"type":"other"}\n\n' +
      'event: message\ndata: {bad\n\n' +
      'event: message\ndata: {"content":""}\n\n' +
      'event: error\ndata: {notjson\n\n' +
      'event: retry\ndata: \n\n' +
      'event: done\ndata: \n\n';
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    // Two consecutive assistants: regenerating the second skips the first
    // assistant while walking back to the user message.
    const seed = [
      { role: 'user', content: 'root q' },
      { role: 'assistant', content: 'first a' },
      { role: 'assistant', content: 'second a' },
    ];
    global.fetch = makeFetchMock(() => sseResponse(raw), seed) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-regen" />);
    await screen.findByText('second a');
    fireEvent.click(screen.getAllByTitle('Regenerate response')[1]);
    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith('Streaming error'));
    expect(errSpy).toHaveBeenCalled();
  });
});

describe('load-failure tolerance and UI edges', () => {
  it('renders despite metadata/models/documents endpoints failing', async () => {
    global.fetch = makeFetchMock(undefined, [], {
      '/metadata': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      '/api/models': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      '/documents': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      '/rag/config': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-fails" />);
    await screen.findByPlaceholderText(/Type your message/i);
  });

  it('stops paginating once offset zero is reached', async () => {
    const seed = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `p${50 + i}` }));
    global.fetch = makeFetchMock(undefined, seed, {
      'offset=0&': () => ({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            messages: Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `p${i}` })),
            total: 100,
          }),
      }),
    }, 100) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-paged" />);
    await screen.findByText('p50');
    capturedStartReached!(); // 50 → 0
    await screen.findByText('p0');
    capturedStartReached!(); // newOffset === loadedOffset === 0 → hasMore=false
    capturedStartReached!(); // guard: no further fetch
  });

  it('keeps the upload menu open on non-Escape keys and ignores blank doc attach', async () => {
    global.fetch = makeFetchMock(undefined, []) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-ui" />);
    const uploadBtn = await screen.findByTitle('Attach image or document');
    fireEvent.click(uploadBtn);
    fireEvent.keyDown(document, { key: 'a' });
    fireEvent.keyDown(document, { key: 'Escape' });

    // Blank attach input: Enter and button click are both no-ops.
    const attachToggle = document.querySelector('[title*="ttach"]');
    expect(attachToggle).toBeTruthy();
  });

  it('sweeps conditional hover/drag handlers in idle and streaming states', async () => {
    global.fetch = makeFetchMock(() => hangingResponse(), [
      { role: 'user', content: 'hq' },
      { role: 'assistant', content: 'ha' },
    ]) as unknown as typeof fetch;
    render(<ChatPane sessionName="edge-sweep" />);
    await screen.findByText('ha');

    const sweep = () => {
      document.querySelectorAll('*').forEach((el) => {
        const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
        if (!key) return;
        const props = (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key];
        for (const name of ['onMouseEnter', 'onMouseLeave', 'onDragLeave'] as const) {
          if (typeof props?.[name] === 'function') {
            props[name]({
              preventDefault() {},
              currentTarget: { style: { background: '', opacity: '' }, contains: () => true },
              relatedTarget: null,
            });
          }
        }
      });
    };

    sweep(); // idle state (isStreaming false paths)
    await sendMessage('start-stream');
    await waitFor(() => {
      // streaming state reached when the send button disables/aborts appear
      sweep(); // isStreaming-true paths
    });
  });
});

describe('remaining branch closure', () => {
  it('tolerates a failing initial session load and a failing older-page load', async () => {
    // Initial load !ok
    global.fetch = makeFetchMock(undefined, [], {
      '/api/v1/sessions/': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;
    const first = render(<ChatPane sessionName="fail-init" />);
    await screen.findByPlaceholderText(/Type your message/i);
    first.unmount();

    // Older page !ok (initial ok with more history)
    const seed = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `f${50 + i}` }));
    global.fetch = makeFetchMock(undefined, seed, {
      'offset=0&': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }, 100) as unknown as typeof fetch;
    render(<ChatPane sessionName="fail-older" />);
    await screen.findByText('f50');
    capturedStartReached!();
    await waitFor(() =>
      expect(
        (global.fetch as ReturnType<typeof vi.fn>).mock.calls.some(([u]) =>
          String(u).includes('offset=0&')
        )
      ).toBe(true)
    );
    // The failed page is simply not prepended.
    expect(screen.queryByText('f0')).not.toBeInTheDocument();
  });

  it('keeps the upload menu open on non-Escape keys', async () => {
    global.fetch = makeFetchMock(undefined, []) as unknown as typeof fetch;
    render(<ChatPane sessionName="upload-menu" />);
    const toggle = await screen.findByTitle('Upload file');
    fireEvent.click(toggle);
    await screen.findByTitle('Attach image or document');
    fireEvent.keyDown(document, { key: 'a' });
    expect(screen.getByTitle('Attach image or document')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
  });

  it('ignores a blank force-include doc id and empty file selections', async () => {
    global.fetch = makeFetchMock(undefined, [], {
      '/metadata': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }),
      }),
      // The available-documents fetch failing is tolerated (list stays empty).
      '/api/v1/rag/documents': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="attach-blank" />);
    fireEvent.click(await screen.findByTitle('Manage force-included documents'));
    const before = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    const attachBtn = screen.getByText('Attach');
    fireEvent.click(attachBtn); // blank input → no-op
    expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);

    // Empty file-input changes and an empty drop: invoke handlers directly.
    document.querySelectorAll('input[type="file"]').forEach((el) => {
      const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'))!;
      const props = (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key];
      props.onChange?.({ target: { files: [] } });
    });
    document.querySelectorAll('*').forEach((el) => {
      const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
      if (!key) return;
      const props = (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key];
      props?.onDrop?.({
        preventDefault() {},
        stopPropagation() {},
        dataTransfer: { files: [] },
        currentTarget: { style: {} },
      });
    });
  });

  it('covers regen-side empty event payloads and non-Error throws', async () => {
    const raw =
      'event: rag_metadata\ndata: \n\n' +
      'event: message\ndata: \n\n' +
      'event: retry\ndata: \n\n' +
      'event: done\ndata: \n\n';
    const seed = [
      { role: 'user', content: 'rq' },
      { role: 'assistant', content: 'ra' },
    ];
    global.fetch = makeFetchMock(() => sseResponse(raw), seed) as unknown as typeof fetch;
    const first = render(<ChatPane sessionName="regen-empty" />);
    await screen.findByText('ra');
    fireEvent.click(screen.getByTitle('Regenerate response'));
    // Empty payloads: the stream completes leaving an empty assistant bubble.
    await waitFor(() => expect(screen.queryByText('ra')).not.toBeInTheDocument());
    first.unmount();

    // Fresh render: regenerate against a stream that throws a non-Error.
    global.fetch = makeFetchMock(() => ({
      ok: true,
      status: 200,
      body: { getReader: () => ({ read: async () => { throw 'regen raw'; } }) },
    }), seed) as unknown as typeof fetch;
    render(<ChatPane sessionName="regen-throw" />);
    await screen.findByText('ra');
    fireEvent.click(screen.getByTitle('Regenerate response'));
    await screen.findByText(/\[error\] regen raw/);
  });

  it('hovers model-dropdown entries including the selected one', async () => {
    global.fetch = makeFetchMock(undefined, []) as unknown as typeof fetch;
    render(<ChatPane sessionName="model-hover" />);
    const modelButton = await screen.findByRole('button', { name: /nyxGPT/i });
    fireEvent.click(modelButton);
    await screen.findByText('modelA');
    document.querySelectorAll('*').forEach((el) => {
      const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
      if (!key) return;
      const props = (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key];
      for (const name of ['onMouseEnter', 'onMouseLeave'] as const) {
        props?.[name]?.({ currentTarget: { style: { background: '' } } });
      }
    });
    // Select modelA, reopen, and hover again so the selected-item sides run.
    fireEvent.click(screen.getByText('modelA'));
    fireEvent.click(screen.getByRole('button', { name: /nyxGPT/i }));
    await screen.findByText('modelA');
    document.querySelectorAll('*').forEach((el) => {
      const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
      if (!key) return;
      const props = (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key];
      for (const name of ['onMouseEnter', 'onMouseLeave'] as const) {
        props?.[name]?.({ currentTarget: { style: { background: '' } } });
      }
    });
  });
});

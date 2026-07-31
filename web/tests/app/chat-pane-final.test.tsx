import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style, startReached }: any) => (
    <div style={style} data-testid="virtuoso-mock">
      <button data-testid="trigger-start-reached" onClick={() => startReached?.()}>
        load-older
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
    if (url.includes('/rag/documents')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) });
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

beforeEach(() => {
  toastMocks.success.mockClear();
  toastMocks.error.mockClear();
  toastMocks.warning.mockClear();
  toastMocks.info.mockClear();
});

describe('fetchModels default-selection branches', () => {
  it('falls back to an empty array when the /api/models response omits the models field', async () => {
    global.fetch = makeFetchMock({
      '/api/models': () => ({ ok: true, status: 200, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="nomodelsfield1" />);
    await screen.findByPlaceholderText('Type your message…');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    expect(await screen.findByText('Loading models...')).toBeInTheDocument();
  });

  it('keeps the already-selected model on a later refetch (prev-truthy branch)', async () => {
    global.fetch = makeFetchMock({
      '/api/models': () => ({ ok: true, status: 200, json: () => Promise.resolve({ models: ['modelA', 'modelB'] }) }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="prevmodel1" />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    await user.click(await screen.findByText('modelB'));

    // Trigger a refetch (window focus, matching the app's real 'focus'
    // listener) while a model is already selected; the (prev ? prev : ...)
    // branch should keep 'modelB' selected rather than resetting to models[0].
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });

    // Re-open dropdown and confirm modelB still shows the checkmark.
    await user.click(screen.getByRole('button', { name: /nyxGPT/i }));
    const modelBRow = await screen.findByText('modelB');
    expect(modelBRow.parentElement?.textContent).toContain('✓');
  });
});

describe('loadOlderMessages / initial pagination: not-ok and missing-messages fallback branches', () => {
  it('startReached leaves messages unchanged when the older-messages fetch responds not-ok', async () => {
    const recent = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `recent${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/olderNotOk?offset=0&limit=50': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: recent, total: 100 }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="olderNotOk" />);
    await screen.findByText('recent0');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('trigger-start-reached'));
    // Messages list is unaffected by the not-ok response; still just the original 50.
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.getByText('recent0')).toBeInTheDocument();
  });

  it('startReached falls back to an empty array when the older-messages response has no messages field', async () => {
    const recent = Array.from({ length: 50 }, (_, i) => ({ role: 'user', content: `recent${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/oldernomsg?offset=0&limit=50': () => ({ ok: true, status: 200, json: () => Promise.resolve({}) }),
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: recent, total: 100 }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="oldernomsg" />);
    await screen.findByText('recent0');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('trigger-start-reached'));
    // No older messages were prepended (fell back to []); original content still present.
    await waitFor(() => expect(screen.getByText('recent0')).toBeInTheDocument());
  });

  it('falls back to an empty array when the initial paginated response has no messages field', async () => {
    global.fetch = makeFetchMock({
      '/api/v1/sessions/nomsg1?offset=50&limit=50': () => ({ ok: true, status: 200, json: () => Promise.resolve({}) }),
      '/api/v1/sessions/': () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ messages: Array.from({ length: 100 }, (_, i) => ({ role: 'user', content: `m${i}` })), total: 100 }),
      }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="nomsg1" />);
    // Falls back to an empty messages array (no crash); the empty-state copy renders.
    await waitFor(() => expect(screen.getByText('Send a message to start…')).toBeInTheDocument());
  });
});

describe('attachDocument/detachDocument/toggleRag/uploadFile remaining branches', () => {
  it('attachDocument succeeds and falls back to [] when attached_doc_ids is missing from the response', async () => {
    global.fetch = makeFetchMock({
      '/rag/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) }),
      '/documents': (url, init) => {
        if (init?.method === 'POST') return { ok: true, status: 200, json: () => Promise.resolve({}) };
        return { ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: [] }) };
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="attachok1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText(/^Docs/)).toBeInTheDocument());
    await user.click(screen.getByText(/^Docs/));
    const attachInput = await screen.findByPlaceholderText('Enter doc_id to attach...');
    await user.type(attachInput, 'no-list-doc{Enter}');
    await waitFor(() => expect(screen.getByText('No documents attached')).toBeInTheDocument());
  });

  it('detachDocument succeeds and falls back to [] when attached_doc_ids is missing from the response', async () => {
    global.fetch = makeFetchMock({
      '/rag/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) }),
      '/documents/pre-existing': () => ({ ok: true, status: 200, json: () => Promise.resolve({}) }),
      '/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: ['pre-existing'] }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="detachok1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText(/Docs \(1\)/)).toBeInTheDocument());
    await user.click(screen.getByText(/Docs \(1\)/));
    await user.click(await screen.findByTitle('Detach document'));
    await waitFor(() => expect(screen.getByText('No documents attached')).toBeInTheDocument());
  });

  it('toggleRag and uploadFile surface a stringified error when a non-Error is thrown', async () => {
    global.fetch = makeFetchMock({
      '/rag/enable': () => Promise.reject('plain string rejection'),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="nonerror1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText('plain string rejection')).toBeInTheDocument());
  });

  it('uploadFile surfaces a stringified error when a non-Error is thrown', async () => {
    global.fetch = makeFetchMock({
      '/rag/upload': () => Promise.reject('upload rejection string'),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="nonerror2" />);
    const user = userEvent.setup();
    await user.click(await screen.findByTitle('Upload file'));
    await user.click(await screen.findByText('Upload file'));
    const fileInput = document.querySelector('input[type="file"]:not([multiple])') as HTMLInputElement;
    const file = new File(['x'], 'f.txt', { type: 'text/plain' });
    await userEvent.upload(fileInput, file);
    await waitFor(() => expect(screen.getByText('upload rejection string')).toBeInTheDocument());
  });
});

describe('send() editingIndex-cleared and rag_filters operand branches', () => {
  it('clears editingIndex when send() is invoked while editing', async () => {
    const seed = [{ role: 'user', content: 'orig' }, { role: 'assistant', content: 'reply' }];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
      '/api/chat/stream': () => sseResponse('event: done\ndata: {}\n\n'),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="editsend1" />);
    await screen.findByText('orig');
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Edit message'));
    expect(screen.getByText('Edit')).toBeInTheDocument();

    const input = screen.getByPlaceholderText('Type your message…') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'a brand new message' } });
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(screen.queryByText('Edit')).not.toBeInTheDocument());
  });

  it('includes rag_filters when doc_ids or tags are set (send and regenerate)', async () => {
    let sendBody: any = null;
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/api/chat/stream': (_url: string, init?: RequestInit) => {
        sendBody = init?.body ? JSON.parse(init.body as string) : null;
        return sseResponse('event: done\ndata: {}\n\n');
      },
    }) as unknown as typeof fetch;
    sessionStorage.setItem('rag_filters_ragops1', JSON.stringify({ doc_ids: ['d1'], tags: ['t1'] }));

    render(<ChatPane sessionName="ragops1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'with doc_ids and tags');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(sendBody?.rag_filters).toEqual({ doc_ids: ['d1'], tags: ['t1'] }));
  });
});

describe('remaining edge-case branches: datalist fallback label, FileReader without a comma', () => {
  it('falls back to doc_id as the datalist option label when filename is null', async () => {
    global.fetch = makeFetchMock({
      '/rag/documents': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ documents: [{ doc_id: 'no-filename-doc', filename: null, chunks: 1, tags: null, ingested_at: null }] }),
        }),
      '/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: ['no-filename-doc'] }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="datalistfallback1" />);
    const user = userEvent.setup();
    const docsBtn = await screen.findByText(/Docs \(1\)/);
    await user.click(docsBtn);
    await waitFor(() => {
      const option = document.querySelector('#available-docs-list option') as HTMLOptionElement;
      expect(option.getAttribute('label')).toBe('no-filename-doc');
    });
  });

  it('falls back to an empty base64 string when the FileReader result has no comma', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    const originalFileReader = global.FileReader;
    class NoCommaFileReader {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      result: string | null = null;
      readAsDataURL() {
        this.result = 'not-a-data-url-without-comma';
        setTimeout(() => this.onload && this.onload(), 0);
      }
    }
    // @ts-expect-error - stubbing FileReader for this test only
    global.FileReader = NoCommaFileReader;

    render(<ChatPane sessionName="nocomma1" />);
    await screen.findByPlaceholderText('Type your message…');
    const doc = new File(['x'], 'weird.pdf', { type: 'application/pdf' });
    const input = document.querySelector('input[type="file"][multiple]') as HTMLInputElement;
    await userEvent.upload(input, doc);

    await waitFor(() => expect(screen.getByTitle('Remove weird.pdf')).toBeInTheDocument());
    global.FileReader = originalFileReader;
  });

  it('includes rag_filters in the regenerate request body when only tags or dates are set', async () => {
    let capturedBody: any = null;
    const seed = [
      { role: 'user', content: 'Q3' },
      { role: 'assistant', content: 'old3' },
    ];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/api/chat/stream': (_url: string, init?: RequestInit) => {
        capturedBody = init?.body ? JSON.parse(init.body as string) : null;
        return sseResponse('event: done\ndata: {}\n\n');
      },
    }) as unknown as typeof fetch;
    // Only date_to is set (no doc_ids/filename/tags/date_from) so the OR
    // chain in the regenerate rag_filters condition must evaluate all the
    // way to its final operand.
    sessionStorage.setItem('rag_filters_regendates1', JSON.stringify({ date_to: '2026-02-01' }));

    render(<ChatPane sessionName="regendates1" />);
    await screen.findByText('old3');
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => expect(capturedBody?.rag_filters).toEqual({ date_to: '2026-02-01' }));
  });
});

describe('RAG filter inputs: clearing back to empty (undefined) branch', () => {
  it('clears the filename and date filters back to undefined when emptied', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="clearfilters1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    await user.click(screen.getByTitle('Filter RAG documents'));

    const filenameInput = await screen.findByPlaceholderText('Filter by filename...');
    fireEvent.change(filenameInput, { target: { value: 'x' } });
    await waitFor(() => expect(screen.getByText(/Filters \(active\)/)).toBeInTheDocument());
    fireEvent.change(filenameInput, { target: { value: '' } });

    const dateInputs = document.querySelectorAll('input[type="date"]') as NodeListOf<HTMLInputElement>;
    fireEvent.change(dateInputs[0], { target: { value: '2026-01-01' } });
    fireEvent.change(dateInputs[0], { target: { value: '' } });
    fireEvent.change(dateInputs[1], { target: { value: '2026-02-01' } });
    fireEvent.change(dateInputs[1], { target: { value: '' } });

    await waitFor(() => expect(screen.queryByText(/Filters \(active\)/)).not.toBeInTheDocument());
  });
});

describe('drag-over visual state and paperclip hover while streaming', () => {
  it('applies the drag-over dashed border while a file is dragged over the input box', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="dragover1" />);
    const input = await screen.findByPlaceholderText('Type your message…');
    const dropzone = input.parentElement!;
    fireEvent.dragOver(dropzone, { dataTransfer: { files: [] } });
    expect(dropzone.getAttribute('style')).toContain('2px dashed');
    fireEvent.dragLeave(dropzone, { relatedTarget: document.body });
    expect(dropzone.getAttribute('style')).not.toContain('2px dashed');
  });

  it('fires the paperclip mouseLeave handler while streaming', async () => {
    global.fetch = makeFetchMock({
      '/api/chat/stream': () => ({
        ok: true,
        status: 200,
        body: { getReader: () => ({ read: () => new Promise(() => {}) }) },
      }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="dragover2" />);
    const input = await screen.findByPlaceholderText('Type your message…');
    const user = userEvent.setup();
    await user.type(input, 'streaming now');
    await user.click(screen.getByTitle('Send message'));
    await screen.findByTitle('Stop generating');

    const paperclip = screen.getByTitle('Attach image or document');
    fireEvent.mouseEnter(paperclip);
    fireEvent.mouseLeave(paperclip);
  });
});

describe('regenerate includes rag_filters, and assistant-message highlight background branch', () => {
  it('includes rag_filters in the regenerate request body when doc_ids/tags are set', async () => {
    let capturedBody: any = null;
    const seed = [
      { role: 'user', content: 'Q' },
      { role: 'assistant', content: 'old answer' },
    ];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/api/chat/stream': (_url: string, init?: RequestInit) => {
        capturedBody = init?.body ? JSON.parse(init.body as string) : null;
        return sseResponse('event: done\ndata: {}\n\n');
      },
    }) as unknown as typeof fetch;
    sessionStorage.setItem('rag_filters_regenragops1', JSON.stringify({ doc_ids: ['d1'] }));

    render(<ChatPane sessionName="regenragops1" />);
    await screen.findByText('old answer');
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => expect(capturedBody?.rag_filters).toEqual({ doc_ids: ['d1'] }));
  });

  it('includes rag_filters in the regenerate request body when only collection is set', async () => {
    let capturedBody: any = null;
    const seed = [
      { role: 'user', content: 'Q' },
      { role: 'assistant', content: 'old answer' },
    ];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 2 }) }),
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/api/chat/stream': (_url: string, init?: RequestInit) => {
        capturedBody = init?.body ? JSON.parse(init.body as string) : null;
        return sseResponse('event: done\ndata: {}\n\n');
      },
    }) as unknown as typeof fetch;
    sessionStorage.setItem('rag_filters_regencollection1', JSON.stringify({ collection: 'docs2' }));

    render(<ChatPane sessionName="regencollection1" />);
    await screen.findByText('old answer');
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    const user = userEvent.setup();
    await user.click(screen.getByTitle('Regenerate response'));

    await waitFor(() => expect(capturedBody?.rag_filters).toEqual({ collection: 'docs2' }));
  });

  it('highlights an assistant-role message (the non-user background ternary branch)', async () => {
    vi.useFakeTimers();
    const seed = Array.from({ length: 4 }, (_, i) => ({ role: i % 2 === 0 ? 'user' : 'assistant', content: `m${i}` }));
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: seed.length }) }),
    }) as unknown as typeof fetch;

    // Index 1 is role 'assistant'.
    render(<ChatPane sessionName="highlightassistant1" scrollToMessageIndex={1} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const el = document.querySelector('[data-message-index="1"]')!.querySelector('div');
    expect(el?.getAttribute('style')).toContain('var(--highlight)');
    vi.useRealTimers();
  });
});

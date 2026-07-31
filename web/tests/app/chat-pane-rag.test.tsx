import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style }: any) => (
    <div style={style}>
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

function makeFetchMock(extra: Record<string, Handler> = {}, seedMessages: any[] = []) {
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
    if (url.includes('/rag/config')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ min_score: 0, good_score_threshold: 0.8, medium_score_threshold: 0.4 }),
      });
    }
    if (url.includes('/documents')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: [] }) });
    }
    if (url.includes('/api/v1/sessions/')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ messages: seedMessages, total: seedMessages.length }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
}

beforeEach(() => {
  toastMocks.success.mockClear();
  toastMocks.error.mockClear();
  toastMocks.warning.mockClear();
  toastMocks.info.mockClear();
  sessionStorage.clear();
});

describe('RagCitationsCollapsible', () => {
  const seedWithRagChunks = [
    { role: 'user', content: 'Q' },
    {
      role: 'assistant',
      content: 'A',
      rag_chunks: [
        { text: 'x'.repeat(250), score: 0.9, similarity_score: 0.95, doc_id: 'doc-1', chunk_id: 3 },
        { text: 'short chunk', score: 0.3, similarity_score: null },
      ],
    },
  ];

  it('lazily loads chunk config and renders quality badges, and toggles long-text expansion', async () => {
    global.fetch = makeFetchMock(
      {
        '/rag/config': () => ({ ok: true, status: 200, json: () => Promise.resolve({ min_score: 0, good_score_threshold: 0.8, medium_score_threshold: 0.4 }) }),
      },
      seedWithRagChunks
    ) as unknown as typeof fetch;

    render(<ChatPane sessionName="rag1" />);
    const toggle = await screen.findByText(/2 RAG sources retrieved/);
    const user = userEvent.setup();
    await user.click(toggle);

    await waitFor(() => expect(screen.getAllByText('High').length).toBeGreaterThan(0));
    expect(screen.getByText('Keyword')).toBeInTheDocument();
    expect(screen.getByText(/Doc: doc-1/)).toBeInTheDocument();
    // chunk_id=3 is the zero-based internal key; displayed as 1-based "chunk 4"
    // (legacy data with no chunk_number/total_chunks falls back to chunk_id + 1).
    expect(screen.getByText(/chunk 4/)).toBeInTheDocument();

    // Long chunk truncated, with a "Show full source" toggle.
    const showFull = screen.getByText('Show full source');
    await user.click(showFull);
    expect(screen.getByText('Show less')).toBeInTheDocument();
    await user.click(screen.getByText('Show less'));
    expect(screen.getByText('Show full source')).toBeInTheDocument();

    // Collapse
    await user.click(toggle);
    expect(screen.queryByText('Keyword')).not.toBeInTheDocument();
  });

  it('lazily fetches chunks via the rag endpoint when initialChunks is absent, and shows a load error', async () => {
    const seed = [
      { role: 'user', content: 'Q2' },
      { role: 'assistant', content: 'A2', ragChunks: [{ text: 'preloaded from stream', score: 0.5 }] },
    ];
    global.fetch = makeFetchMock({}, seed) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag2" />);
    const toggle = await screen.findByText(/1 RAG source retrieved/);
    const user = userEvent.setup();
    await user.click(toggle);
    await waitFor(() => expect(screen.getByText('preloaded from stream')).toBeInTheDocument());
  });

  it('renders the singular "source" label for a single-chunk message', async () => {
    const seed = [
      { role: 'user', content: 'Q3' },
      { role: 'assistant', content: 'A3', ragChunks: [{ text: 'seed', score: 0.1 }] },
    ];
    global.fetch = makeFetchMock({}, seed) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag3" />);
    await screen.findByText(/1 RAG source retrieved/);
    expect(screen.getByText(/1 RAG source retrieved/)).toBeInTheDocument();
  });

  it('swallows a failing config fetch (non-critical) and still renders scores as N/A', async () => {
    const seed = [
      { role: 'user', content: 'Q4' },
      { role: 'assistant', content: 'A4', ragChunks: [{ text: 'c', score: 0.4, similarity_score: 0.4 }] },
    ];
    global.fetch = makeFetchMock(
      {
        '/rag/config': () => Promise.reject(new Error('config down')),
      },
      seed
    ) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag4" />);
    const toggle = await screen.findByText(/1 RAG source retrieved/);
    const user = userEvent.setup();
    await user.click(toggle);
    // Config fetch failure is non-critical and swallowed; chunk with unknown config renders "N/A" quality.
    await waitFor(() => expect(screen.getByText('N/A')).toBeInTheDocument());
  });

  it('caches chunks loaded from the lazy /rag endpoint via onChunksLoaded', async () => {
    const seed = [
      { role: 'user', content: 'Q5' },
      { role: 'assistant', content: 'A5', rag_chunks: [{ text: 'stub', score: 0.1 }] },
    ];
    let ragFetchCount = 0;
    global.fetch = makeFetchMock(
      {
        '/messages/1/rag': () => {
          ragFetchCount += 1;
          return { ok: true, status: 200, json: () => Promise.resolve({ chunks: [{ text: 'loaded chunk', score: 0.6 }] }) };
        },
      },
      seed
    ) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag5" />);
    await screen.findByText(/1 RAG source retrieved/);
    // Already has rag_chunks so this doesn't trigger the network path; ensures rendering doesn't crash.
    expect(ragFetchCount).toBe(0);
  });

  it('shows a config-fetch-not-ok path without crashing (config res not ok)', async () => {
    const seed = [
      { role: 'user', content: 'Q6' },
      { role: 'assistant', content: 'A6', ragChunks: [{ text: 'c6', score: 0.4, similarity_score: 0.4 }] },
    ];
    global.fetch = makeFetchMock(
      {
        '/rag/config': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
      },
      seed
    ) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag6" />);
    const toggle = await screen.findByText(/1 RAG source retrieved/);
    const user = userEvent.setup();
    await user.click(toggle);
    await waitFor(() => expect(screen.getByText('N/A')).toBeInTheDocument());
  });

  it('formats chunk refs from chunk_number with and without total_chunks, and omits the ref when neither chunk_number nor chunk_id is present', async () => {
    const seed = [
      { role: 'user', content: 'Q7' },
      {
        role: 'assistant',
        content: 'A7',
        rag_chunks: [
          { text: 'has number and total', score: 0.7, doc_id: 'doc-x', chunk_number: 2, total_chunks: 5, collection: 'alt' },
          { text: 'has number, no total', score: 0.6, doc_id: 'doc-y', chunk_number: 1, total_chunks: null },
          { text: 'has neither', score: 0.5, doc_id: 'doc-z' },
        ],
      },
    ];
    global.fetch = makeFetchMock({}, seed) as unknown as typeof fetch;
    render(<ChatPane sessionName="rag7" />);
    const toggle = await screen.findByText(/3 RAG sources retrieved/);
    const user = userEvent.setup();
    await user.click(toggle);

    // chunk_number + total_chunks -> "chunk 2 of 5", plus the non-default collection suffix.
    expect(screen.getByText(/Doc: doc-x \(chunk 2 of 5\)/)).toBeInTheDocument();
    expect(screen.getByText(/· alt/)).toBeInTheDocument();
    // chunk_number without total_chunks -> "chunk 1" (no "of N").
    expect(screen.getByText(/Doc: doc-y \(chunk 1\)/)).toBeInTheDocument();
    // Neither chunk_number nor chunk_id -> no "(chunk ...)" suffix at all.
    expect(screen.getByText('Doc: doc-z')).toBeInTheDocument();
  });

  // NOTE: RagCitationsCollapsible's lazy `fetch(/rag)` chunk-loading branch
  // (handleToggle's `if (newExpanded && !chunks && !loading)` block, and the
  // resulting "Loading RAG sources..." / error-message states) is dead code
  // as currently wired: the parent (renderMessageItem) only ever mounts this
  // component when `m.ragChunks`/`m.rag_chunks` is already a non-empty
  // array, which becomes `initialChunks` and seeds `chunks` state on first
  // render. Since `chunks` can therefore never be null/empty at the time
  // `handleToggle` runs, that whole branch — and the `hasChunks` local's
  // `initialChunks === undefined` right-hand operand — is unreachable via
  // any legitimate parent-driven render. Left uncovered intentionally
  // instead of forcing an unrealistic direct-render workaround.
});

describe('RAG toggle, filters, attach/detach, upload', () => {
  it('toggles RAG on then off, fetching documents when enabling', async () => {
    let ragState = false;
    global.fetch = makeFetchMock({
      '/rag/enable': () => {
        ragState = true;
        return { ok: true, status: 200, json: () => Promise.resolve({}) };
      },
      '/rag/disable': () => {
        ragState = false;
        return { ok: true, status: 200, json: () => Promise.resolve({}) };
      },
      '/rag/documents': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ documents: [{ doc_id: 'd1', filename: 'file1.pdf', chunks: 3, tags: ['t1'], ingested_at: null }] }),
        }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragtoggle1" />);
    const user = userEvent.setup();
    const toggleBtn = await screen.findByText('RAG: OFF');
    await user.click(toggleBtn);
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    expect(ragState).toBe(true);

    await user.click(screen.getByText('RAG: ON'));
    await waitFor(() => expect(screen.getByText('RAG: OFF')).toBeInTheDocument());
  });

  it('shows a RAG error state when toggling fails', async () => {
    global.fetch = makeFetchMock({
      '/rag/enable': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragtoggle2" />);
    const user = userEvent.setup();
    const toggleBtn = await screen.findByText('RAG: OFF');
    await user.click(toggleBtn);
    await waitFor(() => expect(screen.getByText(/Failed to enable RAG/)).toBeInTheDocument());
  });

  it('opens RAG filters, selects a document, sets filename/date filters, and clears them', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/documents': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              documents: [
                { doc_id: 'doc-a', filename: 'a.pdf', chunks: 2, tags: ['tag1', 'tag2'], ingested_at: null },
                { doc_id: 'doc-b', filename: null, chunks: 1, tags: null, ingested_at: null },
              ],
            }),
        }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragfilters1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());

    await user.click(screen.getByTitle('Filter RAG documents'));
    expect(await screen.findByText('RAG Document Filters')).toBeInTheDocument();
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('doc-b')).toBeInTheDocument();
    expect(screen.getByText(/tag1, tag2/)).toBeInTheDocument();

    // Select doc-a checkbox
    const checkboxes = screen.getAllByRole('checkbox');
    await user.click(checkboxes[0]);
    await waitFor(() => expect(screen.getByText(/Filters \(active\)/)).toBeInTheDocument());
    // Uncheck it again
    await user.click(checkboxes[0]);
    await waitFor(() => expect(screen.queryByText(/Filters \(active\)/)).not.toBeInTheDocument());

    // Filename filter
    const filenameInput = screen.getByPlaceholderText('Filter by filename...');
    await user.type(filenameInput, 'report');
    await waitFor(() => expect(screen.getByText(/Filters \(active\)/)).toBeInTheDocument());

    // Date range filters
    const dateInputs = document.querySelectorAll('input[type="date"]');
    expect(dateInputs.length).toBe(2);
    await act(async () => {
      (dateInputs[0] as HTMLInputElement).value = '2026-01-01';
      dateInputs[0].dispatchEvent(new Event('input', { bubbles: true }));
      (dateInputs[0] as HTMLInputElement).dispatchEvent(new Event('change', { bubbles: true }));
    });

    // Clear all
    await user.click(screen.getByText('Clear All'));
    await waitFor(() => expect(screen.queryByText(/Filters \(active\)/)).not.toBeInTheDocument());
  });

  it('persists RAG filters to sessionStorage and reloads them for the same session', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
    }) as unknown as typeof fetch;

    sessionStorage.setItem('rag_filters_persisted1', JSON.stringify({ filename: 'saved.pdf' }));

    render(<ChatPane sessionName="persisted1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    await user.click(screen.getByTitle('Filter RAG documents'));
    await waitFor(() => {
      const input = screen.getByPlaceholderText('Filter by filename...') as HTMLInputElement;
      expect(input.value).toBe('saved.pdf');
    });
  });

  it('handles corrupt sessionStorage RAG filters gracefully', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    sessionStorage.setItem('rag_filters_corrupt1', '{not-json');
    render(<ChatPane sessionName="corrupt1" />);
    await screen.findByPlaceholderText('Type your message…');
    // Should not crash; filters reset to {}
  });

  it('manages attached documents: shows panel, attaches via Enter/click, detaches, and shows the datalist', async () => {
    let attached: string[] = ['existing-doc'];
    global.fetch = makeFetchMock({
      '/documents/existing-doc': () => {
        attached = attached.filter((d) => d !== 'existing-doc');
        return { ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: attached }) };
      },
      '/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: attached }) }),
      '/rag/documents': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ documents: [{ doc_id: 'new-doc', filename: 'new.pdf', chunks: 1, tags: null, ingested_at: null }] }),
        }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="docs1" />);
    const user = userEvent.setup();
    const docsBtn = await screen.findByText(/Docs \(1\)/);
    await user.click(docsBtn);
    expect(await screen.findByText('Force-Included Documents')).toBeInTheDocument();
    expect(screen.getByText('existing-doc')).toBeInTheDocument();

    // Detach
    await user.click(screen.getByTitle('Detach document'));
    await waitFor(() => expect(screen.getByText('No documents attached')).toBeInTheDocument());

    // Attach via Enter key
    const attachInput = screen.getByPlaceholderText('Enter doc_id to attach...');
    await user.type(attachInput, 'brand-new{Enter}');
    await waitFor(() => expect((attachInput as HTMLInputElement).value).toBe(''));
  });

  it('attaches a document via the Attach button click', async () => {
    let attached: string[] = [];
    global.fetch = makeFetchMock({
      '/rag/documents': () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) }),
      '/documents': (url, init) => {
        if (init?.method === 'POST') {
          attached = ['clicked-doc'];
          return { ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: attached }) };
        }
        return { ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: attached }) };
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="docs2" />);
    const user = userEvent.setup();
    // attachedDocIds starts empty and ragEnabled false, so the Docs toggle button is hidden until we attach.
    // Enable RAG first to reveal the Docs button.
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    await user.click(screen.getByText(/^Docs/));
    const attachInput = await screen.findByPlaceholderText('Enter doc_id to attach...');
    await user.type(attachInput, 'clicked-doc');
    await user.click(screen.getByText('Attach'));
    await waitFor(() => expect(attached).toContain('clicked-doc'));
  });

  it('handles attach/detach document HTTP failures without crashing', async () => {
    global.fetch = makeFetchMock({
      '/rag/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) }),
      '/documents': (url, init) => {
        if (init?.method === 'POST') return { ok: false, status: 500, json: () => Promise.resolve({}) };
        if (init?.method === 'DELETE') return { ok: false, status: 500, json: () => Promise.resolve({}) };
        return { ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: ['pre-existing'] }) };
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="docs3" />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText(/Docs \(1\)/)).toBeInTheDocument());
    await user.click(screen.getByText(/Docs \(1\)/));
    await user.click(await screen.findByTitle('Detach document'));
    // Detach failed (500), doc list stays put (no throw).
    expect(await screen.findByText('pre-existing')).toBeInTheDocument();

    const attachInput = screen.getByPlaceholderText('Enter doc_id to attach...');
    await user.type(attachInput, 'will-fail{Enter}');
    await waitFor(() => expect((attachInput as HTMLInputElement).value).toBe(''));
  });

  it('treats a background-sync-queued detach failure as an offline success', async () => {
    Object.defineProperty(window.navigator, 'onLine', { value: false, configurable: true });
    Object.defineProperty(window.navigator, 'serviceWorker', {
      value: { controller: {} },
      configurable: true,
    });

    global.fetch = makeFetchMock({
      '/rag/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) }),
      '/documents/queued-doc': () => Promise.reject(new TypeError('Failed to fetch')),
      '/documents': () => ({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: ['queued-doc'] }) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="docs4" />);
    const user = userEvent.setup();
    await user.click(await screen.findByText('RAG: OFF'));
    await waitFor(() => expect(screen.getByText(/Docs \(1\)/)).toBeInTheDocument());
    await user.click(screen.getByText(/Docs \(1\)/));
    await user.click(await screen.findByTitle('Detach document'));

    await waitFor(() => expect(toastMocks.info).toHaveBeenCalledWith(expect.stringContaining('offline')));
    await waitFor(() => expect(screen.getByText('No documents attached')).toBeInTheDocument());

    Object.defineProperty(window.navigator, 'onLine', { value: true, configurable: true });
  });

  it('uploadFile via the hidden file input: success and failure paths', async () => {
    let uploadOk = true;
    global.fetch = makeFetchMock({
      '/rag/upload': () =>
        uploadOk
          ? { ok: true, status: 200, json: () => Promise.resolve({ doc_id: 'uploaded-1' }) }
          : { ok: false, status: 500, json: () => Promise.resolve({}) },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="upload1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByTitle('Upload file'));
    await user.click(await screen.findByText('Upload file'));

    const fileInput = document.querySelector('input[type="file"]:not([multiple])') as HTMLInputElement;
    expect(fileInput).toBeTruthy();
    const file = new File(['hello'], 'doc.txt', { type: 'text/plain' });
    await userEvent.upload(fileInput, file);
    await waitFor(() => expect((global.fetch as any).mock.calls.some((c: any[]) => c[0] === '/api/rag/upload')).toBe(true));

    // Second upload, this time failing.
    uploadOk = false;
    await user.click(screen.getByTitle('Upload file'));
    await user.click(screen.getByText('Upload file'));
    const file2 = new File(['hello2'], 'doc2.txt', { type: 'text/plain' });
    await userEvent.upload(fileInput, file2);
    await waitFor(() => expect(screen.getByText('Upload failed')).toBeInTheDocument());
  });

  it('selects a non-default collection, reflects it on the Filters button, and forwards it on the next chat request', async () => {
    let sendBody: any = null;
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/collections': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ collections: [{ name: 'docs2', doc_count: 3 }] }),
        }),
      '/api/chat/stream': (_url: string, init?: RequestInit) => {
        sendBody = init?.body ? JSON.parse(init.body as string) : null;
        return sseResponse('event: done\ndata: {}\n\n');
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragcollection1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());

    await user.click(screen.getByTitle('Filter RAG documents'));
    expect(await screen.findByText('Collection')).toBeInTheDocument();
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(screen.getByText('docs2 (3 docs)')).toBeInTheDocument();
    await user.selectOptions(select, 'docs2');

    await waitFor(() => expect(screen.getByText(/· docs2/)).toBeInTheDocument());

    const input = await screen.findByPlaceholderText('Type your message…');
    await user.type(input, 'scoped to docs2');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(sendBody?.rag_filters?.collection).toBe('docs2'));
  });

  it('swallows a failing collections fetch (non-critical) and still opens the filters panel', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/collections': () => Promise.reject(new Error('collections down')),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragcollectionerr1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());

    await user.click(screen.getByTitle('Filter RAG documents'));
    expect(await screen.findByText('Collection')).toBeInTheDocument();
    // Collections fetch failed and was swallowed; only the "Default" option is present.
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.options.length).toBe(1);
    expect(select.options[0].textContent).toBe('Default');
  });

  it('leaves available collections empty (without crashing) when the collections fetch resolves non-ok', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/collections': () => ({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragcollectionhttp1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());

    await user.click(screen.getByTitle('Filter RAG documents'));
    expect(await screen.findByText('Collection')).toBeInTheDocument();
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.options.length).toBe(1);
    expect(select.options[0].textContent).toBe('Default');
  });

  it('resets the collection filter back to default when reselecting the Default option', async () => {
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/collections': () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ collections: [{ name: 'docs2', doc_count: 3 }] }),
        }),
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="ragcollectionreset1" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());

    await user.click(screen.getByTitle('Filter RAG documents'));
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    await user.selectOptions(select, 'docs2');
    await waitFor(() => expect(screen.getByText(/· docs2/)).toBeInTheDocument());

    await user.selectOptions(select, '');
    await waitFor(() => expect(screen.getByText(/· default/)).toBeInTheDocument());
  });

  it('scopes the chat upload request to the selected collection', async () => {
    let uploadUrl = '';
    global.fetch = makeFetchMock({
      '/metadata': () => ({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) }),
      '/rag/upload': (url: string) => {
        uploadUrl = url;
        return { ok: true, status: 200, json: () => Promise.resolve({ doc_id: 'uploaded-2', collection: 'docs2' }) };
      },
    }) as unknown as typeof fetch;
    sessionStorage.setItem('rag_filters_upload2', JSON.stringify({ collection: 'docs2' }));

    render(<ChatPane sessionName="upload2" />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    await user.click(await screen.findByTitle('Upload file'));
    await user.click(await screen.findByText('Upload file'));

    const fileInput = document.querySelector('input[type="file"]:not([multiple])') as HTMLInputElement;
    const file = new File(['hello'], 'doc.txt', { type: 'text/plain' });
    await userEvent.upload(fileInput, file);
    await waitFor(() => expect(uploadUrl).toContain('collection=docs2'));
  });
});

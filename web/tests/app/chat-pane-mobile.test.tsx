import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';

// Force the mobile layout branch (width/height/padding ternaries throughout
// ChatPane keyed off isMobile) so those branches get exercised alongside
// the desktop-focused assertions in the other chat-pane-*.test.tsx files.
vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => true,
}));

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style }: any) => (
    <div style={style} data-testid="virtuoso-mock">
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
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ rag_enabled: true, title: '', model: '' }) });
    }
    if (url.includes('/rag/documents')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ documents: [] }) });
    }
    if (url.includes('/documents')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ attached_doc_ids: ['doc-a'] }) });
    }
    if (url.includes('/api/v1/sessions/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            messages: [
              { role: 'user', content: 'hi mobile' },
              { role: 'assistant', content: 'reply mobile' },
            ],
            total: 2,
          }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
}

beforeEach(() => {
  toastMocks.success.mockClear();
  toastMocks.error.mockClear();
});

describe('ChatPane mobile layout branches', () => {
  it('sizes action buttons, upload/attach/RAG controls, and the send button for touch (44px) on mobile', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="mobile1" />);
    await screen.findByText('hi mobile');

    const copyBtn = screen.getByTitle('Copy message');
    expect(copyBtn.getAttribute('style')).toContain('width: 44px');
    expect(copyBtn.getAttribute('style')).toContain('opacity: 1'); // always visible on mobile (no hover)

    const editBtn = screen.getByTitle('Edit message');
    expect(editBtn.getAttribute('style')).toContain('height: 44px');

    const copyResponseBtn = screen.getByTitle('Copy response');
    expect(copyResponseBtn.getAttribute('style')).toContain('width: 44px');

    const regenBtn = screen.getByTitle('Regenerate response');
    expect(regenBtn.getAttribute('style')).toContain('height: 44px');

    const uploadBtn = screen.getByTitle('Upload file');
    expect(uploadBtn.getAttribute('style')).toContain('width: 44px');

    const attachBtn = screen.getByTitle('Attach image or document');
    expect(attachBtn.getAttribute('style')).toContain('width: 44px');

    await waitFor(() => expect(screen.getByText('RAG: ON')).toBeInTheDocument());
    expect(screen.getByText('RAG: ON').getAttribute('style')).toContain('10px 14px');

    const user = userEvent.setup();
    await user.click(screen.getByTitle('Filter RAG documents'));
    expect(screen.getByTitle('Filter RAG documents').getAttribute('style')).toContain('10px 14px');

    await waitFor(() => expect(screen.getByText(/Docs \(1\)/)).toBeInTheDocument());
    await user.click(screen.getByText(/Docs \(1\)/));
    expect(screen.getByText(/Docs \(1\)/).getAttribute('style')).toContain('10px 14px');

    const sendBtn = screen.getByTitle('Send message');
    expect(sendBtn.getAttribute('style')).toContain('width: 44px');
  });

  it('sizes the Stop button for touch while streaming', async () => {
    global.fetch = makeFetchMock({
      '/api/chat/stream': () => ({
        ok: true,
        status: 200,
        body: { getReader: () => ({ read: () => new Promise(() => {}) }) },
      }),
    }) as unknown as typeof fetch;
    render(<ChatPane sessionName="mobile2" />);
    const input = await screen.findByPlaceholderText('Type your message…');
    const user = userEvent.setup();
    await user.type(input, 'go mobile');
    await user.click(screen.getByTitle('Send message'));

    const stopBtn = await screen.findByTitle('Stop generating');
    expect(stopBtn.getAttribute('style')).toContain('width: 44px');

    // Hover handlers on the (disabled while streaming) upload button.
    const uploadBtn = screen.getByTitle('Upload file');
    fireEvent.mouseEnter(uploadBtn);
    fireEvent.mouseLeave(uploadBtn);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';
import type { VirtuosoMockProps } from '../mocks/virtuoso';

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

function makeFetchMock(extra: Record<string, (url: string, init?: RequestInit) => unknown> = {}) {
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

function attachmentInput(): HTMLInputElement {
  return document.querySelector('input[type="file"][multiple]') as HTMLInputElement;
}

describe('ChatPane inline attachments', () => {
  it('rejects an unsupported file type with a toast and adds nothing', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="att1" />);
    await screen.findByPlaceholderText('Type your message…');

    const badFile = new File(['data'], 'archive.zip', { type: 'application/zip' });
    await act(async () => {
      fireEvent.change(attachmentInput(), { target: { files: [badFile] } });
    });

    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith(expect.stringContaining('Unsupported file type')));
    expect(screen.queryByTitle(/Remove archive.zip/)).not.toBeInTheDocument();
  });

  it('adds an image attachment with a preview thumbnail and can remove it', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="att2" />);
    await screen.findByPlaceholderText('Type your message…');

    const img = new File(['imgdata'], 'photo.png', { type: 'image/png' });
    await userEvent.upload(attachmentInput(), img);

    await waitFor(() => expect(screen.getByTitle('Remove photo.png')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTitle('Remove photo.png'));
    await waitFor(() => expect(screen.queryByTitle('Remove photo.png')).not.toBeInTheDocument());
  });

  it('adds a document attachment (non-image, no preview) and shows the pending-attachment badge count', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="att3" />);
    await screen.findByPlaceholderText('Type your message…');

    const doc = new File(['docdata'], 'notes.pdf', { type: 'application/pdf' });
    await userEvent.upload(attachmentInput(), doc);

    await waitFor(() => expect(screen.getByTitle('Remove notes.pdf')).toBeInTheDocument());
    // Paperclip badge shows count of pending attachments.
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('processes multiple files at once, mixing image and document types', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="att4" />);
    await screen.findByPlaceholderText('Type your message…');

    const img = new File(['a'], 'one.png', { type: 'image/png' });
    const doc = new File(['b'], 'two.pdf', { type: 'application/pdf' });
    await userEvent.upload(attachmentInput(), [img, doc]);

    await waitFor(() => {
      expect(screen.getByTitle('Remove one.png')).toBeInTheDocument();
      expect(screen.getByTitle('Remove two.pdf')).toBeInTheDocument();
    });
  });

  it('shows a toast and skips the file when FileReader fails', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    const originalFileReader = global.FileReader;
    class FailingFileReader {
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      result: string | null = null;
      readAsDataURL() {
        setTimeout(() => this.onerror && this.onerror(), 0);
      }
    }
    // @ts-expect-error - stubbing FileReader for this test only
    global.FileReader = FailingFileReader;

    render(<ChatPane sessionName="att5" />);
    await screen.findByPlaceholderText('Type your message…');
    const doc = new File(['b'], 'broken.pdf', { type: 'application/pdf' });
    await userEvent.upload(attachmentInput(), doc);

    await waitFor(() => expect(toastMocks.error).toHaveBeenCalledWith(expect.stringContaining('Failed to process file')));

    global.FileReader = originalFileReader;
  });

  it('adds attachments via drag-and-drop onto the input box', async () => {
    global.fetch = makeFetchMock() as unknown as typeof fetch;
    render(<ChatPane sessionName="att6" />);
    await screen.findByPlaceholderText('Type your message…');

    const dropzone = screen.getByPlaceholderText('Type your message…').parentElement!;
    const file = new File(['x'], 'dropped.pdf', { type: 'application/pdf' });

    await act(async () => {
      fireEvent.dragOver(dropzone, { dataTransfer: { files: [file] } });
      fireEvent.dragLeave(dropzone, { relatedTarget: document.body });
      fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });
    });

    await waitFor(() => expect(screen.getByTitle('Remove dropped.pdf')).toBeInTheDocument());
  });

  it('sends the message with attachments included in the request body, and clears pending attachments', async () => {
    let capturedBody: Record<string, unknown> | null = null;
    global.fetch = makeFetchMock({
      '/api/chat/stream': (_url, init) => {
        capturedBody = init?.body ? JSON.parse(init.body as string) : null;
        return {
          ok: true,
          status: 200,
          body: {
            getReader() {
              let sent = false;
              return {
                read: async () => {
                  if (!sent) {
                    sent = true;
                    return { value: new TextEncoder().encode('event: done\ndata: {}\n\n'), done: false };
                  }
                  return { value: undefined, done: true };
                },
              };
            },
          },
        };
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="att7" />);
    const user = userEvent.setup();
    await screen.findByPlaceholderText('Type your message…');

    const doc = new File(['b'], 'attach-me.pdf', { type: 'application/pdf' });
    await userEvent.upload(attachmentInput(), doc);
    await waitFor(() => expect(screen.getByTitle('Remove attach-me.pdf')).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText('Add a message (optional)…'), 'with attachment');
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(capturedBody?.attachments?.length).toBe(1));
    expect(capturedBody.attachments[0].filename).toBe('attach-me.pdf');

    // Pending attachments cleared after send, and the sent message shows the inline thumbnail.
    await waitFor(() => expect(screen.queryByTitle('Remove attach-me.pdf')).not.toBeInTheDocument());
  });

  it('sends attachment-only messages (no text) using a single space as the prompt', async () => {
    let capturedBody: Record<string, unknown> | null = null;
    global.fetch = makeFetchMock({
      '/api/chat/stream': (_url, init) => {
        capturedBody = init?.body ? JSON.parse(init.body as string) : null;
        return {
          ok: true,
          status: 200,
          body: {
            getReader() {
              let sent = false;
              return {
                read: async () => {
                  if (!sent) {
                    sent = true;
                    return { value: new TextEncoder().encode('event: done\ndata: {}\n\n'), done: false };
                  }
                  return { value: undefined, done: true };
                },
              };
            },
          },
        };
      },
    }) as unknown as typeof fetch;

    render(<ChatPane sessionName="att8" />);
    await screen.findByPlaceholderText('Type your message…');
    const img = new File(['x'], 'pic.png', { type: 'image/png' });
    await userEvent.upload(attachmentInput(), img);
    await waitFor(() => expect(screen.getByTitle('Remove pic.png')).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTitle('Send message'));

    await waitFor(() => expect(capturedBody?.prompt).toBe(' '));
  });

  it('clicking an inline image attachment thumbnail opens it in a new window', async () => {
    const seed = [
      {
        role: 'user',
        content: 'look at this',
        attachments: [
          { file: {} as File, preview: 'data:image/png;base64,AAA', type: 'image', media_type: 'image/png', data: 'AAA', filename: 'inline.png' },
        ],
      },
    ];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 1 }) }),
    }) as unknown as typeof fetch;

    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ChatPane sessionName="att9" />);
    await screen.findByText('look at this');

    const user = userEvent.setup();
    await user.click(screen.getByTitle('inline.png'));
    expect(openSpy).toHaveBeenCalledWith('data:image/png;base64,AAA', '_blank');
    openSpy.mockRestore();
  });

  it('renders an inline document attachment thumbnail without opening a window on click', async () => {
    const seed = [
      {
        role: 'user',
        content: 'a doc',
        attachments: [
          { file: {} as File, preview: '', type: 'document', media_type: 'application/pdf', data: 'AAA', filename: 'inline.pdf' },
        ],
      },
    ];
    global.fetch = makeFetchMock({
      '/api/v1/sessions/': () => ({ ok: true, status: 200, json: () => Promise.resolve({ messages: seed, total: 1 }) }),
    }) as unknown as typeof fetch;

    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ChatPane sessionName="att10" />);
    await screen.findByText('a doc');
    const user = userEvent.setup();
    await user.click(screen.getByTitle('inline.pdf'));
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });
});

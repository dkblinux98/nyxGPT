import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPane from '@/app/components/ChatPane';

type VirtuosoMockProps = {
  totalCount: number;
  itemContent: (index: number) => React.ReactNode;
  style?: React.CSSProperties;
  'aria-label'?: string;
  role?: string;
};

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: VirtuosoMockProps) => (
    <div style={style} aria-label={props['aria-label']} role={props.role}>
      {Array.from({ length: totalCount }).map((_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
}));

let currentModels: string[] = ['llama3.1:8b'];
let modelsFetchCount = 0;

function mockFetch(url: string) {
  if (url.includes('/api/models')) {
    modelsFetchCount += 1;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ models: currentModels }),
    });
  }
  if (url.includes('/metadata')) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ rag_enabled: false, title: '', model: '' }),
    });
  }
  if (url.includes('/documents')) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ attached_doc_ids: [] }),
    });
  }
  if (url.includes('/api/v1/sessions/')) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ messages: [], total: 0 }),
    });
  }
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
  });
}

describe('ChatPane model selector refresh (#3190)', () => {
  beforeEach(() => {
    currentModels = ['llama3.1:8b'];
    modelsFetchCount = 0;
    global.fetch = vi.fn().mockImplementation(mockFetch) as unknown as typeof fetch;
  });

  it('fetches models on mount', async () => {
    render(<ChatPane sessionName="default" />);
    await waitFor(() => expect(modelsFetchCount).toBeGreaterThanOrEqual(1));
  });

  it('re-fetches models when the model dropdown is opened, picking up newly pulled models', async () => {
    render(<ChatPane sessionName="default" />);
    await waitFor(() => expect(modelsFetchCount).toBeGreaterThanOrEqual(1));

    // Simulate a model pull completing elsewhere (e.g. Manage Models page).
    currentModels = ['llama3.1:8b', 'llama3.1:70b'];

    const user = userEvent.setup();
    const modelButton = screen.getByRole('button', { name: /nyxGPT/i });
    await user.click(modelButton);

    await waitFor(() => {
      expect(screen.getByText('llama3.1:70b')).toBeInTheDocument();
    });
  });

  it('re-fetches models when the window regains focus', async () => {
    render(<ChatPane sessionName="default" />);
    await waitFor(() => expect(modelsFetchCount).toBeGreaterThanOrEqual(1));

    const countBeforeFocus = modelsFetchCount;
    currentModels = ['llama3.1:8b', 'mistral:7b'];
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => expect(modelsFetchCount).toBeGreaterThan(countBeforeFocus));
  });
});

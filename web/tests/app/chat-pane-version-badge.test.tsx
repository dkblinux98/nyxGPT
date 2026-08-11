import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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

function mockFetch(url: string) {
  if (url.includes('/api/models')) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ models: ['llama3.1:8b'] }),
    });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
}

describe('ChatPane header version badge (#3716)', () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockImplementation(mockFetch) as unknown as typeof fetch;
  });

  it('displays the running version reported by the API', async () => {
    render(<ChatPane sessionName="default" releaseVersion="3.0.0" />);

    await waitFor(() => expect(screen.getByText('v3.0.0')).toBeInTheDocument());
  });

  it('does not double-prefix a version that already starts with v', async () => {
    render(<ChatPane sessionName="default" releaseVersion="v3.0.0" />);

    await waitFor(() => expect(screen.getByText('v3.0.0')).toBeInTheDocument());
    expect(screen.queryByText('vv3.0.0')).not.toBeInTheDocument();
  });

  it('renders the badge without a version when the API reports none', async () => {
    render(<ChatPane sessionName="default" releaseVersion={null} />);

    await waitFor(() => expect(screen.getByText('nyxGPT')).toBeInTheDocument());
    expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument();
  });
});

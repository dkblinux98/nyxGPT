import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import ChatPane from '@/app/components/ChatPane';
import type { VirtuosoMockProps } from '../mocks/virtuoso';

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

/**
 * #3982: the header could describe a build other than the one rendering it.
 * Owner acceptance ran rc13 kegs and read `v3.0.0`, and separately ran a
 * 2.1.0 web build against a 3.0.0-line API with the header still reading
 * `v3.0.0` -- it described neither tier. Each case below fails against the
 * pre-#3982 header, which took a single `releaseVersion` and rendered it raw.
 */
describe('ChatPane header stack tier (#3982)', () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockImplementation(mockFetch) as unknown as typeof fetch;
  });

  it('shows a release candidate with its rc suffix intact', async () => {
    render(
      <ChatPane sessionName="default" releaseVersion="3.0.0rc13" webVersion="3.0.0rc13" />,
    );

    await waitFor(() => expect(screen.getByText('v3.0.0rc13')).toBeInTheDocument());
    expect(screen.queryByText('v3.0.0')).not.toBeInTheDocument();
  });

  it('badges a release candidate as rc so it cannot be read as the release', async () => {
    render(
      <ChatPane sessionName="default" releaseVersion="3.0.0rc13" webVersion="3.0.0rc13" />,
    );

    await waitFor(() =>
      expect(screen.getByTestId('version-channel-badge')).toHaveTextContent('rc'),
    );
  });

  it('leaves a matched stable stack unbadged and unwarned', async () => {
    render(<ChatPane sessionName="default" releaseVersion="3.0.0" webVersion="3.0.0" />);

    await waitFor(() => expect(screen.getByText('v3.0.0')).toBeInTheDocument());
    expect(screen.queryByTestId('version-channel-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('version-mismatch-warning')).not.toBeInTheDocument();
  });

  it('warns visibly when the web tier and the API are different builds', async () => {
    render(<ChatPane sessionName="default" releaseVersion="3.0.0" webVersion="2.1.0" />);

    const warning = await screen.findByTestId('version-mismatch-warning');
    // Naming both versions is the point: the 2.1.0/3.0.0 stack was found
    // only because a feature was visibly missing, and a false acceptance
    // failure was nearly filed against that feature instead.
    expect(warning).toHaveTextContent('web v2.1.0');
    expect(warning).toHaveTextContent('API v3.0.0');
    expect(screen.getByTestId('version-channel-badge')).toHaveTextContent('mixed');
  });

  it('stays silent when the web tier version cannot be established', async () => {
    render(<ChatPane sessionName="default" releaseVersion="3.0.0" webVersion={null} />);

    await waitFor(() => expect(screen.getByText('v3.0.0')).toBeInTheDocument());
    expect(screen.queryByTestId('version-mismatch-warning')).not.toBeInTheDocument();
  });

  it('does not warn on the stack `docker compose up` produces by default', async () => {
    // docker-compose.yml defaults NYXGPT_WEB_VERSION to the image tag, so a
    // stack built from a checkout reports `local` for the web tier and the
    // package version for the API. Warning here would put a permanent red
    // badge on every default Compose stack -- a false alarm about two images
    // from one tree, and the surest way to teach an operator to ignore the
    // warning on the day it is real.
    render(<ChatPane sessionName="default" releaseVersion="3.0.0" webVersion="local" />);

    await waitFor(() => expect(screen.getByText('v3.0.0')).toBeInTheDocument());
    expect(screen.queryByTestId('version-mismatch-warning')).not.toBeInTheDocument();
    expect(screen.queryByText(/vlocal/)).not.toBeInTheDocument();
  });
});

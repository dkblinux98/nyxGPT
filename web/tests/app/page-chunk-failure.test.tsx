/**
 * Home page chunk-failure tests (#3857)
 *
 * The defect: both lazily-loaded pieces of the home page (ChatPane and
 * VirtualizedSessionList) are `next/dynamic` with `ssr: false` and a
 * `loading:` fallback, so when their chunks do not arrive the spinner and the
 * session skeletons render forever -- with no error and no retry. That is
 * what the owner saw in #3857 while every backend endpoint was answering in
 * under 110 ms, and it is indistinguishable from the app still working.
 *
 * This suite renders the real page.tsx with both chunk imports failing and
 * asserts the opposite: an error surface appears, and the loading placeholders
 * do not persist. It fails against the pre-#3857 code, where the fallbacks
 * simply stayed on screen.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import Home from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';

// Every chunk import on this page fails, exactly as it would against a web
// tier that no longer serves the URLs this document asked for. Failing at the
// loader boundary rather than by mocking each component module keeps the real
// page.tsx wiring -- dynamic(), the loading fallbacks and the boundaries -- in
// the test. The factory is self-contained because vi.mock is hoisted above
// every module-level binding in the file.
vi.mock('@/lib/chunkLoader', () => ({
  CHUNK_LOAD_TIMEOUT_MS: 20_000,
  withChunkTimeout: (_load: unknown, chunkName: string) => () => {
    const error = new Error(`Loading chunk ${chunkName} failed.`);
    error.name = 'ChunkLoadError';
    return Promise.reject(error);
  },
}));

vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const fetchMock = vi.fn();

function jsonRes(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: { get: () => null },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  // React logs every error a boundary catches; keep the suite output readable.
  vi.spyOn(console, 'error').mockImplementation(() => {});
  fetchMock.mockImplementation((url: string) => {
    if (String(url).includes('/api/info')) return Promise.resolve(jsonRes({ release_version: '3.0.0' }));
    if (String(url).includes('/api/sessions')) {
      return Promise.resolve(jsonRes({ sessions: [{ name: 'default', title: 'Default', messages: 2 }] }));
    }
    return Promise.resolve(jsonRes({}));
  });
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Home page — a chunk that never loads', () => {
  it('surfaces the chat pane failure instead of spinning forever', async () => {
    render(
      <ThemeProvider>
        <Home />
      </ThemeProvider>,
    );

    const alerts = await screen.findAllByRole('alert');
    const chatAlert = alerts.find((el) => el.textContent?.includes('the chat pane'));
    expect(chatAlert, 'no failure surface rendered for the chat pane').toBeDefined();
    expect(chatAlert).toHaveTextContent('Failed to load the interface');
    expect(chatAlert!.querySelector('button')).toHaveTextContent('Reload');
  });

  it('surfaces the session list failure and stops showing the skeleton', async () => {
    render(
      <ThemeProvider>
        <Home />
      </ThemeProvider>,
    );

    await waitFor(() => {
      const alerts = screen.queryAllByRole('alert');
      expect(alerts.some((el) => el.textContent?.includes('the session list'))).toBe(true);
    });

    // The exact symptom from the report: skeleton rows that never go away.
    expect(screen.queryByLabelText('Loading sessions')).not.toBeInTheDocument();
  });
});

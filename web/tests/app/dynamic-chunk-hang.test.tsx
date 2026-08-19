/**
 * Hung-chunk composition test (#3857)
 *
 * The reported failure was not a chunk that *failed* -- it was a chunk that
 * never answered at all. `next/dynamic` has no timeout, so its `loading:`
 * fallback rendered indefinitely with nothing to distinguish it from a slow
 * load. This assembles the exact composition page.tsx uses
 * (`dynamic(withChunkTimeout(...), { ssr: false, loading })` inside a
 * `ChunkErrorBoundary`), drives it with a loader that never settles, and
 * asserts the placeholder gives way to a visible failure.
 */

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import dynamic from 'next/dynamic';
import { ChunkErrorBoundary } from '../../src/components/ChunkErrorBoundary';
import { withChunkTimeout } from '../../src/lib/chunkLoader';

const TIMEOUT_MS = 50;

/** Never resolves and never rejects -- the #3857 request state. */
const HungComponent = dynamic(
  withChunkTimeout(
    () => new Promise<{ default: () => React.ReactElement }>(() => {}),
    'HungComponent',
    TIMEOUT_MS,
  ),
  {
    ssr: false,
    loading: () => <div aria-label="Loading sessions">skeleton</div>,
  },
);

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('a dynamic chunk that never settles', () => {
  it('shows the loading placeholder first, then a failure surface', async () => {
    render(
      <ChunkErrorBoundary label="the session list">
        <HungComponent />
      </ChunkErrorBoundary>,
    );

    // Before the timeout this is indistinguishable from a slow load -- which
    // is correct, and is exactly why the timeout has to exist.
    expect(screen.getByLabelText('Loading sessions')).toBeInTheDocument();

    await waitFor(
      () => expect(screen.getByRole('alert')).toHaveTextContent('Failed to load the interface'),
      { timeout: 2000 },
    );
    expect(screen.queryByLabelText('Loading sessions')).not.toBeInTheDocument();
  });
});

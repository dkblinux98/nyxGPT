/**
 * ChunkErrorBoundary tests (#3857)
 *
 * The boundary is what makes a failed lazy chunk *visible*. It must show an
 * actionable surface for chunk failures, must not swallow ordinary render
 * bugs (those belong to the surrounding boundaries), and its reload must
 * clear the service worker on the way out -- a plain refresh does not.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Component, ReactNode } from 'react';
import { ChunkErrorBoundary } from '../../src/components/ChunkErrorBoundary';

const recoverAndReload = vi.fn(async (reload: () => void) => {
  reload();
});
let controlled = false;

vi.mock('../../src/lib/serviceWorkerRecovery', () => ({
  hasControllingServiceWorker: () => controlled,
  recoverAndReload: (reload: () => void) => recoverAndReload(reload),
}));

function Boom({ error }: { error: Error }): never {
  throw error;
}

/** Stand-in for the boundaries page.tsx already wraps this one in. */
class Catcher extends Component<{ children: ReactNode }, { caught: Error | null }> {
  state: { caught: Error | null } = { caught: null };

  static getDerivedStateFromError(error: Error) {
    return { caught: error };
  }

  render() {
    return this.state.caught ? <div>outer: {this.state.caught.message}</div> : this.props.children;
  }
}

function chunkError(): Error {
  const error = new Error('Loading chunk ChatPane failed: still pending after 20000ms');
  error.name = 'ChunkLoadError';
  return error;
}

beforeEach(() => {
  controlled = false;
  recoverAndReload.mockClear();
  // React logs every error a boundary catches; silence it so the suite output
  // stays readable.
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ChunkErrorBoundary', () => {
  it('renders its children when nothing fails', () => {
    render(
      <ChunkErrorBoundary label="the chat pane">
        <div>chat</div>
      </ChunkErrorBoundary>,
    );
    expect(screen.getByText('chat')).toBeInTheDocument();
  });

  it('shows an actionable failure surface for a chunk load error', () => {
    render(
      <ChunkErrorBoundary label="the chat pane">
        <Boom error={chunkError()} />
      </ChunkErrorBoundary>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load the interface');
    expect(screen.getByRole('alert')).toHaveTextContent('the chat pane');
    expect(screen.getByRole('button', { name: /^reload$/i })).toBeInTheDocument();
  });

  it('reports that no service worker is involved when none controls the page', () => {
    render(
      <ChunkErrorBoundary label="the session list">
        <Boom error={chunkError()} />
      </ChunkErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('No service worker is controlling');
  });

  it('reports a controlling service worker, which is the #3857 discriminator', () => {
    controlled = true;
    render(
      <ChunkErrorBoundary label="the session list">
        <Boom error={chunkError()} />
      </ChunkErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('A service worker is controlling this page');
  });

  it('purges the service worker and reloads when the button is clicked', async () => {
    render(
      <ChunkErrorBoundary label="the chat pane">
        <Boom error={chunkError()} />
      </ChunkErrorBoundary>,
    );

    fireEvent.click(screen.getByRole('button', { name: /^reload$/i }));

    await waitFor(() => expect(recoverAndReload).toHaveBeenCalledTimes(1));
    // The button reports progress rather than looking inert while the caches
    // are being cleared.
    expect(screen.getByRole('button', { name: /reloading/i })).toBeDisabled();
  });

  it('re-throws a non-chunk error so the surrounding boundaries still see it', () => {
    render(
      <Catcher>
        <ChunkErrorBoundary label="the chat pane">
          <Boom error={new Error('a real bug')} />
        </ChunkErrorBoundary>
      </Catcher>,
    );

    expect(screen.getByText('outer: a real bug')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load the interface')).not.toBeInTheDocument();
  });
});

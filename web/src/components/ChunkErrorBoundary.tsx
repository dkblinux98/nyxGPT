'use client';

import { Component, ReactNode } from 'react';
import { isChunkLoadError } from '../hooks/useAppUpdate';
import { hasControllingServiceWorker, recoverAndReload } from '../lib/serviceWorkerRecovery';

type Props = {
  children: ReactNode;
  /** What failed to load, e.g. "the chat pane" -- used in the message. */
  label: string;
};

type State = {
  error: Error | null;
  /** Whether a service worker was controlling the page when this failed. */
  serviceWorkerControlled: boolean;
  recovering: boolean;
};

/**
 * Turns a failed `next/dynamic` chunk into a visible, actionable surface
 * (#3857).
 *
 * Paired with `withChunkTimeout`, this closes the hole that made the whole UI
 * look like it was still working while it was permanently broken: a chunk
 * that never arrives now rejects, the rejection lands here, and the user sees
 * "Failed to load the interface" with a reload that also clears the service
 * worker -- instead of a skeleton or spinner that never resolves.
 *
 * Only chunk-load failures are handled. Anything else is re-thrown from
 * `render` so it reaches the surrounding boundaries (and Sentry) unchanged --
 * a genuine bug inside a lazily-loaded component must not be mislabelled as a
 * loading failure.
 */
export class ChunkErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { error: null, serviceWorkerControlled: false, recovering: false };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, serviceWorkerControlled: hasControllingServiceWorker() };
  }

  handleReload = () => {
    this.setState({ recovering: true });
    void recoverAndReload(() => window.location.reload());
  };

  render() {
    const { error, serviceWorkerControlled, recovering } = this.state;
    if (!error) return this.props.children;

    // Not a chunk failure: this boundary is the wrong owner. Re-throwing from
    // render hands it to the next boundary up rather than swallowing it.
    if (!isChunkLoadError(error)) throw error;

    return (
      <div
        role="alert"
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          height: '100%',
          padding: '1.5rem',
          textAlign: 'center',
        }}
      >
        <div style={{ fontSize: 24 }}>⚠️</div>
        <div style={{ fontWeight: 600 }}>Failed to load the interface</div>
        <div style={{ fontSize: 14, opacity: 0.85, maxWidth: 420 }}>
          {`The code for ${this.props.label} did not load. The server is not
            necessarily down -- this usually means the browser is holding an
            outdated copy of the app. Reloading clears it.`}
        </div>
        <button
          onClick={this.handleReload}
          disabled={recovering}
          style={{
            padding: '8px 16px',
            borderRadius: 6,
            border: 'none',
            fontWeight: 600,
            fontSize: 14,
            cursor: recovering ? 'not-allowed' : 'pointer',
          }}
        >
          {recovering ? 'Reloading...' : 'Reload'}
        </button>
        {/* The discriminator #3857 asks for, without needing DevTools: a
            controlling service worker points at a stale cached client; its
            absence points at the chunk URLs themselves being unavailable. */}
        <details style={{ fontSize: 12, opacity: 0.7, maxWidth: 420 }}>
          <summary style={{ cursor: 'pointer' }}>Details</summary>
          <div style={{ marginTop: 6, textAlign: 'left' }}>
            <div>{error.message}</div>
            <div>
              {serviceWorkerControlled
                ? 'A service worker is controlling this page; reloading unregisters it.'
                : 'No service worker is controlling this page.'}
            </div>
          </div>
        </details>
      </div>
    );
  }
}

export default ChunkErrorBoundary;

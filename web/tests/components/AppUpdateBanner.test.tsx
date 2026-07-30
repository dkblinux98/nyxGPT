/**
 * AppUpdateBanner component tests (#3445)
 *
 * Verifies rendering behaviour of the stranded-client recovery banner:
 *   - Renders nothing until useAppUpdate reports an update is available
 *   - Renders an actionable alert once a stale-chunk/service-worker
 *     update signal fires
 *   - Clicking "Reload to update" reloads the page
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import AppUpdateBanner from '../../src/components/AppUpdateBanner';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AppUpdateBanner', () => {
  it('renders nothing before an update is detected', () => {
    const { container } = render(<AppUpdateBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders an alert with a reload button once a chunk-load failure is detected', () => {
    render(<AppUpdateBanner />);

    act(() => {
      const event = new Event('unhandledrejection') as unknown as PromiseRejectionEvent;
      const err = new Error('boom');
      err.name = 'ChunkLoadError';
      Object.defineProperty(event, 'reason', { value: err });
      window.dispatchEvent(event);
    });

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('A new version of nyxGPT is available.');
    expect(screen.getByRole('button', { name: /reload to update/i })).toBeInTheDocument();
  });

  it('reloads the page when the reload button is clicked', () => {
    render(<AppUpdateBanner />);

    act(() => {
      const event = new Event('unhandledrejection') as unknown as PromiseRejectionEvent;
      const err = new Error('boom');
      err.name = 'ChunkLoadError';
      Object.defineProperty(event, 'reason', { value: err });
      window.dispatchEvent(event);
    });

    const originalLocation = window.location;
    const reload = vi.fn();
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    window.location = { ...originalLocation, reload };

    fireEvent.click(screen.getByRole('button', { name: /reload to update/i }));

    expect(reload).toHaveBeenCalledTimes(1);
    window.location = originalLocation;
  });
});

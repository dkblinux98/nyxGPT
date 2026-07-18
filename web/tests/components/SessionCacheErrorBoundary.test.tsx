import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { SessionCacheErrorBoundary } from '../../src/components/SessionCacheErrorBoundary';

// A child that throws during render so we can exercise the error boundary's
// getDerivedStateFromError / componentDidCatch / fallback UI logic.
function Bomb({ message = 'boom' }: { message?: string }): React.ReactElement {
  throw new Error(message);
}

function Safe() {
  return <div>All good</div>;
}

describe('SessionCacheErrorBoundary', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // React (and our componentDidCatch) log to console.error when an error is
    // caught; suppress it so test output stays clean, but keep the spy so we
    // can assert on the logging side effect.
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it('renders children when there is no error', () => {
    render(
      <SessionCacheErrorBoundary>
        <Safe />
      </SessionCacheErrorBoundary>
    );

    expect(screen.getByText('All good')).toBeInTheDocument();
  });

  it('renders default fallback UI and logs the error when a child throws', () => {
    render(
      <SessionCacheErrorBoundary>
        <Bomb message="cache init failed" />
      </SessionCacheErrorBoundary>
    );

    expect(screen.getByText('Session Cache Error')).toBeInTheDocument();
    expect(
      screen.getByText(
        'An error occurred while initializing the session cache. This might be due to a configuration issue.'
      )
    ).toBeInTheDocument();

    // Error details should be present, using the caught error's message.
    expect(screen.getByText('Error Details')).toBeInTheDocument();
    expect(screen.getByText(/cache init failed/)).toBeInTheDocument();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      'SessionCacheErrorBoundary caught an error:',
      expect.any(Error),
      expect.objectContaining({ componentStack: expect.any(String) })
    );
  });

  it('renders the custom fallback prop instead of the default UI when provided', () => {
    render(
      <SessionCacheErrorBoundary fallback={<div>Custom fallback</div>}>
        <Bomb />
      </SessionCacheErrorBoundary>
    );

    expect(screen.getByText('Custom fallback')).toBeInTheDocument();
    expect(screen.queryByText('Session Cache Error')).not.toBeInTheDocument();
  });

  it('reloads the page when "Reload Page" is clicked', () => {
    const reload = vi.fn();
    const originalLocation = window.location;
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    // @ts-expect-error - partial Location stub
    window.location = { ...originalLocation, reload };

    render(
      <SessionCacheErrorBoundary>
        <Bomb />
      </SessionCacheErrorBoundary>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Reload Page' }));

    expect(reload).toHaveBeenCalledTimes(1);

    window.location = originalLocation;
  });
});

/**
 * PWAInstallButton component tests
 *
 * Verifies rendering behaviour of the PWA install prompt button:
 *   - Renders nothing when the app is not installable
 *   - Renders a button when the browser raises a beforeinstallprompt event
 *   - Clicking the button triggers the install prompt
 *   - Accessible label and text content are correct
 *   - Button disappears after the user accepts installation
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import PWAInstallButton from '../../src/components/PWAInstallButton';

// ---------------------------------------------------------------------------
// Helper – build a minimal BeforeInstallPromptEvent-alike
// ---------------------------------------------------------------------------

interface MockPromptEvent extends Event {
  prompt: ReturnType<typeof vi.fn>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

function makeInstallPromptEvent(outcome: 'accepted' | 'dismissed' = 'accepted'): MockPromptEvent {
  const evt = new Event('beforeinstallprompt') as MockPromptEvent;
  evt.prompt = vi.fn().mockResolvedValue(undefined);
  evt.userChoice = Promise.resolve({ outcome });
  return evt;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PWAInstallButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders nothing when no install prompt is available', () => {
    const { container } = render(<PWAInstallButton />);
    expect(container.firstChild).toBeNull();
  });

  it('renders an install button when beforeinstallprompt fires', async () => {
    render(<PWAInstallButton />);

    act(() => {
      window.dispatchEvent(makeInstallPromptEvent());
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /install nyxgpt app/i })).toBeInTheDocument();
    });
  });

  it('button has accessible label and visible text', async () => {
    render(<PWAInstallButton />);

    act(() => {
      window.dispatchEvent(makeInstallPromptEvent());
    });

    await waitFor(() => {
      const btn = screen.getByRole('button');
      expect(btn).toHaveAttribute('aria-label', 'Install nyxGPT app');
      expect(btn).toHaveTextContent('Install App');
    });
  });

  it('calls the native install prompt when the button is clicked', async () => {
    render(<PWAInstallButton />);
    const evt = makeInstallPromptEvent('accepted');

    act(() => {
      window.dispatchEvent(evt);
    });

    await waitFor(() => {
      expect(screen.getByRole('button')).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button'));
    });

    expect(evt.prompt).toHaveBeenCalledTimes(1);
  });

  it('hides the button after the user accepts installation', async () => {
    const { container } = render(<PWAInstallButton />);
    const evt = makeInstallPromptEvent('accepted');

    act(() => {
      window.dispatchEvent(evt);
    });

    await waitFor(() => {
      expect(screen.getByRole('button')).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button'));
    });

    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it('keeps the button visible when the user dismisses the install prompt', async () => {
    render(<PWAInstallButton />);
    const evt = makeInstallPromptEvent('dismissed');

    act(() => {
      window.dispatchEvent(evt);
    });

    await waitFor(() => {
      expect(screen.getByRole('button')).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button'));
    });

    // dismissed → button must stay visible
    await waitFor(() => {
      expect(screen.getByRole('button')).toBeInTheDocument();
    });
  });
});

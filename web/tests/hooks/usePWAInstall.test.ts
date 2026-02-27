/**
 * usePWAInstall hook tests
 *
 * Verifies the Progressive Web App install-prompt hook:
 *   - isInstallable is false when no prompt has been received
 *   - isInstallable becomes true when beforeinstallprompt fires
 *   - promptInstall calls prompt() on the captured event
 *   - state resets to not-installable after the user accepts
 *   - isInstalled becomes true after the appinstalled event fires
 *   - event listeners are cleaned up on unmount
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePWAInstall } from '../../src/hooks/usePWAInstall';

// ---------------------------------------------------------------------------
// Helpers
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

describe('usePWAInstall', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('starts with isInstallable false and isInstalled false', () => {
    const { result } = renderHook(() => usePWAInstall());

    expect(result.current.isInstallable).toBe(false);
    expect(result.current.isInstalled).toBe(false);
  });

  it('sets isInstallable to true when beforeinstallprompt fires', () => {
    const { result } = renderHook(() => usePWAInstall());

    act(() => {
      window.dispatchEvent(makeInstallPromptEvent());
    });

    expect(result.current.isInstallable).toBe(true);
  });

  it('calls prompt() on the captured event when promptInstall is invoked', async () => {
    const { result } = renderHook(() => usePWAInstall());
    const evt = makeInstallPromptEvent('accepted');

    act(() => {
      window.dispatchEvent(evt);
    });

    expect(result.current.isInstallable).toBe(true);

    await act(async () => {
      await result.current.promptInstall();
    });

    expect(evt.prompt).toHaveBeenCalledTimes(1);
  });

  it('clears isInstallable after the user accepts the install prompt', async () => {
    const { result } = renderHook(() => usePWAInstall());
    const evt = makeInstallPromptEvent('accepted');

    act(() => {
      window.dispatchEvent(evt);
    });

    await act(async () => {
      await result.current.promptInstall();
    });

    expect(result.current.isInstallable).toBe(false);
  });

  it('keeps isInstallable true when the user dismisses the install prompt', async () => {
    const { result } = renderHook(() => usePWAInstall());
    const evt = makeInstallPromptEvent('dismissed');

    act(() => {
      window.dispatchEvent(evt);
    });

    await act(async () => {
      await result.current.promptInstall();
    });

    // dismissed → prompt should NOT be cleared
    expect(result.current.isInstallable).toBe(true);
  });

  it('does nothing when promptInstall is called before a prompt event', async () => {
    const { result } = renderHook(() => usePWAInstall());

    // Should not throw
    await act(async () => {
      await result.current.promptInstall();
    });

    expect(result.current.isInstallable).toBe(false);
  });

  it('sets isInstalled to true when appinstalled fires', () => {
    const { result } = renderHook(() => usePWAInstall());

    act(() => {
      window.dispatchEvent(new Event('appinstalled'));
    });

    expect(result.current.isInstalled).toBe(true);
    // Prompt is no longer relevant once installed
    expect(result.current.isInstallable).toBe(false);
  });

  it('clears the stored prompt when appinstalled fires', () => {
    const { result } = renderHook(() => usePWAInstall());

    act(() => {
      window.dispatchEvent(makeInstallPromptEvent());
    });
    expect(result.current.isInstallable).toBe(true);

    act(() => {
      window.dispatchEvent(new Event('appinstalled'));
    });
    expect(result.current.isInstallable).toBe(false);
  });

  it('removes event listeners on unmount', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const { unmount } = renderHook(() => usePWAInstall());

    unmount();

    const removedTypes = removeSpy.mock.calls.map((call) => call[0]);
    expect(removedTypes).toContain('beforeinstallprompt');
    expect(removedTypes).toContain('appinstalled');
  });
});

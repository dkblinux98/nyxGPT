/**
 * useSidebarVisibility hook tests
 *
 * This hook backs the mobile sidebar behavior in `src/app/page.tsx`:
 *   - collapsed by default on a mobile viewport, shown by default on desktop
 *   - auto-collapses the moment `isMobile` flips to true (e.g. on first
 *     mobile detection after mount), matching the issue's "Collapsible
 *     sidebar on mobile" requirement
 *   - once the user explicitly shows/hides it (functional or direct update,
 *     as used by the toggle button and the Cmd/Ctrl+/ shortcut), that choice
 *     sticks regardless of further viewport changes
 *   - supports being force-hidden on session selection (mobile "auto-dismiss
 *     on select" behavior), the same call shape `page.tsx`'s `selectSession`
 *     uses
 */

import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSidebarVisibility } from '../../src/hooks/useSidebarVisibility';

describe('useSidebarVisibility', () => {
  it('defaults to visible on desktop (isMobile = false)', () => {
    const { result } = renderHook(() => useSidebarVisibility(false));
    expect(result.current[0]).toBe(true);
  });

  it('defaults to hidden on mobile (isMobile = true)', () => {
    const { result } = renderHook(() => useSidebarVisibility(true));
    expect(result.current[0]).toBe(false);
  });

  it('auto-collapses when isMobile flips to true after mount', () => {
    const { result, rerender } = renderHook(({ isMobile }) => useSidebarVisibility(isMobile), {
      initialProps: { isMobile: false },
    });

    expect(result.current[0]).toBe(true);

    rerender({ isMobile: true });

    expect(result.current[0]).toBe(false);
  });

  it('auto-shows again when isMobile flips back to false, absent an explicit override', () => {
    const { result, rerender } = renderHook(({ isMobile }) => useSidebarVisibility(isMobile), {
      initialProps: { isMobile: true },
    });

    expect(result.current[0]).toBe(false);

    rerender({ isMobile: false });

    expect(result.current[0]).toBe(true);
  });

  it('applies a direct override (e.g. dismissing on session select on mobile)', () => {
    const { result } = renderHook(() => useSidebarVisibility(true));

    // Sidebar starts open (e.g. the user tapped the "Show sidebar" button).
    act(() => {
      result.current[1](true);
    });
    expect(result.current[0]).toBe(true);

    // Selecting a session on mobile dismisses the overlay.
    act(() => {
      result.current[1](false);
    });
    expect(result.current[0]).toBe(false);
  });

  it('supports a functional update, as used by the Cmd/Ctrl+/ toggle shortcut', () => {
    const { result } = renderHook(() => useSidebarVisibility(false));

    expect(result.current[0]).toBe(true);

    act(() => {
      result.current[1]((prev) => !prev);
    });
    expect(result.current[0]).toBe(false);

    act(() => {
      result.current[1]((prev) => !prev);
    });
    expect(result.current[0]).toBe(true);
  });

  it('keeps an explicit override across further isMobile changes', () => {
    const { result, rerender } = renderHook(({ isMobile }) => useSidebarVisibility(isMobile), {
      initialProps: { isMobile: true },
    });

    // Default is hidden on mobile; user explicitly shows it.
    expect(result.current[0]).toBe(false);
    act(() => {
      result.current[1](true);
    });
    expect(result.current[0]).toBe(true);

    // Viewport toggles mobile -> desktop -> mobile again; the user's explicit
    // "shown" choice should not be silently overridden.
    rerender({ isMobile: false });
    expect(result.current[0]).toBe(true);

    rerender({ isMobile: true });
    expect(result.current[0]).toBe(true);
  });
});

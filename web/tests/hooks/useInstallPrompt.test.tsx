import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useInstallPrompt } from "../../src/hooks/useInstallPrompt";

describe("useInstallPrompt", () => {
  let beforeInstallPromptEvent: Event;
  let appInstalledEvent: Event;

  beforeEach(() => {
    // Reset mocks
    vi.clearAllMocks();

    // Mock window.matchMedia
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    // Create mock events
    beforeInstallPromptEvent = new Event("beforeinstallprompt");
    appInstalledEvent = new Event("appinstalled");
  });

  it("should initialize with default values", () => {
    const { result } = renderHook(() => useInstallPrompt());

    expect(result.current.isInstallable).toBe(false);
    expect(result.current.isInstalled).toBe(false);
    expect(typeof result.current.promptInstall).toBe("function");
  });

  it("should detect if app is already installed", () => {
    // Mock standalone display mode
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query) => ({
        matches: query === "(display-mode: standalone)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    const { result } = renderHook(() => useInstallPrompt());

    expect(result.current.isInstalled).toBe(true);
    expect(result.current.isInstallable).toBe(false);
  });

  it("should become installable when beforeinstallprompt event fires", async () => {
    const { result } = renderHook(() => useInstallPrompt());

    expect(result.current.isInstallable).toBe(false);

    // Simulate beforeinstallprompt event
    act(() => {
      window.dispatchEvent(beforeInstallPromptEvent);
    });

    await waitFor(() => {
      expect(result.current.isInstallable).toBe(true);
    });
  });

  it("should handle app installation", async () => {
    const { result } = renderHook(() => useInstallPrompt());

    // Make it installable first
    act(() => {
      window.dispatchEvent(beforeInstallPromptEvent);
    });

    await waitFor(() => {
      expect(result.current.isInstallable).toBe(true);
    });

    // Simulate app installation
    act(() => {
      window.dispatchEvent(appInstalledEvent);
    });

    await waitFor(() => {
      expect(result.current.isInstalled).toBe(true);
      expect(result.current.isInstallable).toBe(false);
    });
  });

  it("should clean up event listeners on unmount", () => {
    const removeEventListenerSpy = vi.spyOn(window, "removeEventListener");

    const { unmount } = renderHook(() => useInstallPrompt());

    unmount();

    expect(removeEventListenerSpy).toHaveBeenCalledWith(
      "beforeinstallprompt",
      expect.any(Function)
    );
    expect(removeEventListenerSpy).toHaveBeenCalledWith(
      "appinstalled",
      expect.any(Function)
    );
  });
});

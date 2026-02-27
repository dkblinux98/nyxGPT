'use client';

import { useCallback, useEffect, useState } from 'react';

/**
 * Extends the standard Event interface with the non-standard
 * BeforeInstallPromptEvent properties supported by Chromium-based browsers.
 */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

/**
 * usePWAInstall – Progressive Web App install-prompt hook
 *
 * Listens for the browser's `beforeinstallprompt` event (fired by
 * Chromium-based browsers when the PWA install criteria are met) and
 * exposes a stable `promptInstall` function that triggers the native
 * install dialog.
 *
 * Returns:
 *  - `isInstallable` – true when an install prompt is available
 *  - `isInstalled`   – true after the app has been installed this session
 *  - `promptInstall` – call this (e.g. from a button click) to show the dialog
 */
export function usePWAInstall() {
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [isInstalled, setIsInstalled] = useState(false);

  useEffect(() => {
    const handleBeforeInstallPrompt = (e: Event) => {
      // Prevent the mini-infobar from appearing on mobile
      e.preventDefault();
      setInstallPrompt(e as BeforeInstallPromptEvent);
    };

    const handleAppInstalled = () => {
      setInstallPrompt(null);
      setIsInstalled(true);
    };

    window.addEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
    window.addEventListener('appinstalled', handleAppInstalled);

    return () => {
      window.removeEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
      window.removeEventListener('appinstalled', handleAppInstalled);
    };
  }, []);

  const promptInstall = useCallback(async () => {
    if (!installPrompt) return;

    await installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice;

    if (outcome === 'accepted') {
      setInstallPrompt(null);
    }
  }, [installPrompt]);

  return {
    /** True when the browser has offered an install prompt */
    isInstallable: installPrompt !== null,
    /** True after the user has installed the app this session */
    isInstalled,
    /** Show the native install dialog */
    promptInstall,
  };
}

'use client';

import { usePWAInstall } from '../hooks/usePWAInstall';

/**
 * PWAInstallButton
 *
 * Renders an "Install App" button only when the browser has raised a
 * `beforeinstallprompt` event (i.e. the PWA install criteria are met).
 * Clicking the button triggers the native install dialog.
 *
 * Returns null when the app is already installed or the browser does not
 * support the install prompt, so the component is safe to render anywhere.
 */
export default function PWAInstallButton() {
  const { isInstallable, promptInstall } = usePWAInstall();

  if (!isInstallable) return null;

  return (
    <button
      onClick={promptInstall}
      aria-label="Install nyxGPT app"
      title="Install nyxGPT as an app"
      style={{
        padding: '8px 12px',
        background: 'var(--button-bg)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        fontSize: 14,
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
      }}
    >
      <span aria-hidden="true">⬇️</span>
      <span>Install App</span>
    </button>
  );
}

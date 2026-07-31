'use client';

import { useAppUpdate } from '../hooks/useAppUpdate';

/**
 * Renders a persistent, actionable "reload to update" banner when the
 * client is stranded on a stale build after a web rebuild (#3445) --
 * detected via `useAppUpdate` (chunk-load failures or a service worker
 * version swap). Renders nothing otherwise, so it's safe to mount anywhere.
 */
export default function AppUpdateBanner() {
  const { updateAvailable, reload } = useAppUpdate();

  if (!updateAvailable) return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      style={{
        position: 'fixed',
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 10000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 16,
        padding: '12px 16px',
        background: 'var(--toast-info-bg, #3b82f6)',
        color: 'white',
        fontSize: 14,
        fontWeight: 500,
        boxShadow: '0 -2px 12px rgba(0, 0, 0, 0.15)',
      }}
    >
      <span>A new version of nyxGPT is available.</span>
      <button
        onClick={reload}
        style={{
          padding: '6px 14px',
          background: 'white',
          color: 'var(--toast-info-bg, #3b82f6)',
          border: 'none',
          borderRadius: 6,
          fontWeight: 600,
          fontSize: 14,
          cursor: 'pointer',
        }}
      >
        Reload to update
      </button>
    </div>
  );
}

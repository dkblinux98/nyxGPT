'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiErrorText, errorMessage } from '../lib/apiError';

/**
 * The persistent "saved, but not yet in effect" notice (#3806).
 *
 * The Configuration Wizard's design promise is that a change applies to the
 * running instance with no restart. Some settings genuinely cannot honour
 * that -- a service that reads a value once at process start cannot see a
 * later edit, whatever the wizard does. The answer is not to hide those
 * settings or to special-case one of them, but to make the exception visible,
 * on the IntelliJ plugin-upgrade pattern: the value is still saved, the user
 * is told it is not yet in effect and which services it needs, and a Restart
 * control is offered that they may decline indefinitely.
 *
 * Deliberately NOT a toast. The state lives on the backend
 * (`GET /api/v1/infra/restart-status`, backed by
 * `~/.nyxGPT/pending-restart.json`), so this notice survives a page reload, a
 * navigation away and back, a browser restart, and an API restart -- the user
 * can always find out that the saved value and the running value differ. It
 * disappears only when the restart actually happens or the value is reverted.
 *
 * Mounted on both `/admin` (the wizard, where the change is made) and
 * `/admin/dashboard` (where the user returns), from one component so the two
 * cannot drift in what they claim or offer.
 */

/** `{component: {keys, since}}` -- what is pending for each `nyxgpt ops restart` target. */
export type RestartPending = Record<string, { keys: string[]; since: number }>;

export interface RestartStatus {
  pending: RestartPending;
  /** The wrapped CLI command that applies everything pending, or null when nothing is. */
  restart_command: string | null;
  /** Pending components whose restart drops this browser session (i.e. `web`). */
  session_disrupting: string[];
}

/** How many times to poll `restart-status` after triggering a restart before giving up. */
const RESTART_POLL_ATTEMPTS = 30;
const RESTART_POLL_INTERVAL_MS = 1000;

export async function fetchRestartStatus(): Promise<RestartStatus | null> {
  try {
    const res = await fetch('/api/v1/infra/restart-status', { cache: 'no-store' });
    if (!res.ok) return null;
    const data = await res.json();
    return {
      pending: (data.pending || {}) as RestartPending,
      restart_command: data.restart_command ?? null,
      session_disrupting: (data.session_disrupting || []) as string[],
    };
  } catch {
    // Best-effort: the caller keeps its last-known state rather than
    // flickering the notice away on a transient network error.
    return null;
  }
}

function humanizeSince(since: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - since));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

export interface PendingRestartNoticeProps {
  /** Current status; when null or empty the component renders nothing. */
  status: RestartStatus | null;
  /** Called with the refreshed status after a restart attempt settles. */
  onStatusChange: (status: RestartStatus) => void;
}

export default function PendingRestartNotice({
  status,
  onStatusChange,
}: PendingRestartNoticeProps) {
  const [action, setAction] = useState<'idle' | 'running' | 'failed'>('idle');
  const [actionError, setActionError] = useState<string | null>(null);

  const components = status ? Object.keys(status.pending).sort() : [];
  const sessionDisrupting = status?.session_disrupting ?? [];

  // A restart that succeeded leaves nothing pending, which unmounts this
  // notice -- so 'running' must be reset if the notice is re-shown later.
  useEffect(() => {
    if (components.length === 0) setAction('idle');
  }, [components.length]);

  const handleRestart = useCallback(async () => {
    // The IntelliJ-style warning: say what is about to happen *before* it
    // happens. Restarting `web` tears down the server rendering this very
    // page, so the tab will error or hang for a few seconds and need a
    // reload. Without this, that looks exactly like a crash.
    if (sessionDisrupting.length > 0) {
      const ok = confirm(
        'Restarting the web UI will drop this browser session.\n\n' +
          'This page is served by the service about to restart, so it will ' +
          'become briefly unreachable and you will need to reload it once the ' +
          'restart finishes. Your saved configuration is unaffected.\n\n' +
          'Restart now?'
      );
      if (!ok) return;
    }

    setAction('running');
    setActionError(null);
    try {
      const res = await fetch('/api/v1/infra/restart-required', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(apiErrorText(data, `HTTP ${res.status}`));
      }
    } catch (e: unknown) {
      setAction('failed');
      setActionError(errorMessage(e));
      return;
    }

    for (let attempt = 0; attempt < RESTART_POLL_ATTEMPTS; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RESTART_POLL_INTERVAL_MS));
      // A connection error mid-restart is expected -- the component being
      // restarted may be the one serving this request -- so
      // `fetchRestartStatus` returning null keeps the loop going rather than
      // failing early.
      const next = await fetchRestartStatus();
      if (next) {
        onStatusChange(next);
        if (Object.keys(next.pending).length === 0) {
          setAction('idle');
          return;
        }
      }
    }
    setAction('failed');
    setActionError('Restart did not complete in time -- check status and retry.');
  }, [onStatusChange, sessionDisrupting.length]);

  if (components.length === 0) return null;

  return (
    <div
      role="alert"
      aria-label="Restart required"
      style={{
        padding: '1rem',
        borderRadius: 8,
        border: '1px solid var(--link)',
        background: 'var(--info-bg)',
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6 }}>
        Saved — but not yet in effect
      </div>
      <div style={{ fontSize: 13, color: 'var(--muted-foreground)', marginBottom: 10 }}>
        These settings are saved to your configuration, but the service that uses them read
        their old value at startup and is still running with it. They take effect when you
        restart:
      </div>
      <ul style={{ fontSize: 13, margin: '0 0 10px 0', paddingLeft: '1.25rem' }}>
        {components.map((component) => (
          <li key={component} style={{ marginBottom: 4 }}>
            <strong>{component}</strong> — {status!.pending[component].keys.join(', ')}{' '}
            <span style={{ color: 'var(--muted-foreground)' }}>
              (changed {humanizeSince(status!.pending[component].since)})
            </span>
          </li>
        ))}
      </ul>

      {sessionDisrupting.length > 0 && (
        <div style={{ fontSize: 13, color: 'var(--muted-foreground)', marginBottom: 10 }}>
          Restarting <strong>web</strong> will drop this browser session: this page is served by
          that service, so it will be briefly unreachable and you will need to reload it
          afterwards.
        </div>
      )}

      <button
        onClick={handleRestart}
        disabled={action === 'running'}
        aria-busy={action === 'running'}
        style={{
          padding: '8px 16px',
          background: action === 'running' ? '#ccc' : '#0066cc',
          color: 'white',
          border: 'none',
          borderRadius: 6,
          fontSize: 14,
          fontWeight: 600,
          cursor: action === 'running' ? 'not-allowed' : 'pointer',
        }}
      >
        {action === 'running' ? 'Restarting…' : 'Restart now'}
      </button>

      <div style={{ fontSize: 12, color: 'var(--muted-foreground)', marginTop: 10 }}>
        Restarting is optional — you can leave this for later. This notice stays until the
        restart happens or you change the value back.
        {status!.restart_command && (
          <>
            {' '}
            From a terminal: <code>{status!.restart_command}</code>
          </>
        )}
      </div>

      {action === 'failed' && actionError && (
        <div style={{ color: 'var(--error-text)', fontSize: 13, marginTop: 8 }}>{actionError}</div>
      )}
    </div>
  );
}

/**
 * The persistent "saved, but not yet in effect" notice (#3806).
 *
 * These tests pin the behaviours the owner asked for by name, because each
 * one is a thing the previous implementation got wrong or did not do at all:
 * the notice must *persist* rather than flash past like a toast, it must name
 * the affected services, it must offer a restart the user is free to decline,
 * and it must say out loud that restarting the web tier will drop the session
 * it is being clicked from.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import PendingRestartNotice, {
  fetchRestartStatus,
  failedAttemptText,
  type RestartStatus,
} from '../../src/components/PendingRestartNotice';

const WEB_PENDING: RestartStatus = {
  pending: { web: { keys: ['auth.api_key'], since: Math.floor(Date.now() / 1000) } },
  attempts: {},
  restart_command: 'nyxgpt ops restart web',
  session_disrupting: ['web'],
};

const API_PENDING: RestartStatus = {
  pending: { api: { keys: ['api.port'], since: Math.floor(Date.now() / 1000) } },
  attempts: {},
  restart_command: 'nyxgpt ops restart api',
  session_disrupting: [],
};

describe('PendingRestartNotice', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('renders nothing when nothing is pending', () => {
    const { container } = render(
      <PendingRestartNotice
        status={{ pending: {}, attempts: {}, restart_command: null, session_disrupting: [] }}
        onStatusChange={vi.fn()}
      />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing before the status has loaded', () => {
    const { container } = render(<PendingRestartNotice status={null} onStatusChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says the value is saved but not in effect, and names the service and key', () => {
    render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
    expect(screen.getByText(/not yet in effect/i)).toBeInTheDocument();
    expect(screen.getByText(/auth\.api_key/)).toBeInTheDocument();
    expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
  });

  it('states that the restart is optional and that the notice persists', () => {
    render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
    expect(screen.getByText(/Restarting is optional/i)).toBeInTheDocument();
    expect(screen.getByText(/stays until the restart happens/i)).toBeInTheDocument();
  });

  it('shows the wrapped CLI equivalent, never a raw docker/brew/kubectl command', () => {
    render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
    const code = screen.getByText('nyxgpt ops restart web');
    expect(code).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/docker compose|brew services|kubectl/);
  });

  it('survives a remount -- the notice is server state, not a dismissed toast', () => {
    const { unmount } = render(
      <PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />
    );
    expect(screen.getByText(/not yet in effect/i)).toBeInTheDocument();
    unmount();

    // Navigating away and back re-renders from the same server-side status.
    render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
    expect(screen.getByText(/not yet in effect/i)).toBeInTheDocument();
  });

  describe('session-drop warning before restarting web', () => {
    it('warns in the body copy that restarting web drops this session', () => {
      render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
      expect(screen.getByText(/drop this browser session/i)).toBeInTheDocument();
    });

    it('confirms before restarting web, saying so rather than appearing to hang', async () => {
      const confirmSpy = vi.fn().mockReturnValue(true);
      global.confirm = confirmSpy;
      server.use(
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ targets: ['web'], status: 'scheduled' })
        )
      );

      render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
      expect(confirmSpy.mock.calls[0][0]).toMatch(/drop this browser session/i);
      expect(confirmSpy.mock.calls[0][0]).toMatch(/reload/i);
    });

    it('does not restart when the user declines the warning', async () => {
      global.confirm = vi.fn().mockReturnValue(false);
      const post = vi.fn(() => HttpResponse.json({ targets: ['web'], status: 'scheduled' }));
      server.use(http.post('/api/v1/infra/restart-required', post));

      render(<PendingRestartNotice status={WEB_PENDING} onStatusChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await new Promise((r) => setTimeout(r, 20));
      expect(post).not.toHaveBeenCalled();
      // Declining is deferral, not dismissal -- the notice stays.
      expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
    });

    it('does not warn when only api is pending -- that restart keeps the page alive', async () => {
      const confirmSpy = vi.fn().mockReturnValue(true);
      global.confirm = confirmSpy;
      server.use(
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ targets: ['api'], status: 'scheduled' })
        )
      );

      render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await waitFor(() =>
        expect(screen.getByRole('button', { name: /restarting/i })).toBeInTheDocument()
      );
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(screen.queryByText(/drop this browser session/i)).not.toBeInTheDocument();
    });
  });

  it('clears once the restart lands, and reports the cleared status upward', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      server.use(
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ targets: ['api'], status: 'scheduled' })
        ),
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({ pending: {}, restart_command: null, session_disrupting: [] })
        )
      );

      const onStatusChange = vi.fn();
      render(<PendingRestartNotice status={API_PENDING} onStatusChange={onStatusChange} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });

      await waitFor(() => expect(onStatusChange).toHaveBeenCalled());
      expect(onStatusChange.mock.calls.at(-1)![0].pending).toEqual({});
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps polling while the restarted api is down, then clears when it returns', async () => {
    // #3806 round two. Restarting `api` takes down the process that answers
    // this very poll, and the pending flag is only retired at the end of the
    // replacement process's startup. So the poll MUST tolerate a stretch of
    // failures and then see the empty set -- giving up early is what reported
    // a restart that worked as "Saved -- but not yet in effect".
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let attempts = 0;
      server.use(
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ targets: ['api'], status: 'scheduled' })
        ),
        http.get('/api/v1/infra/restart-status', () => {
          attempts += 1;
          // The api is down for the first 20 polls: the process is gone.
          if (attempts <= 20) return HttpResponse.error();
          return HttpResponse.json({ pending: {}, restart_command: null, session_disrupting: [] });
        })
      );

      const onStatusChange = vi.fn();
      render(<PendingRestartNotice status={API_PENDING} onStatusChange={onStatusChange} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(25000);
      });

      await waitFor(() => expect(onStatusChange).toHaveBeenCalled());
      expect(onStatusChange.mock.calls.at(-1)![0].pending).toEqual({});
      expect(screen.queryByText(/did not report finished in time/i)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('surfaces a failed restart request and re-enables the button', async () => {
    server.use(
      http.post('/api/v1/infra/restart-required', () =>
        HttpResponse.json({ detail: 'no restart is currently pending' }, { status: 400 })
      )
    );

    render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

    await waitFor(() =>
      expect(screen.getByText('no restart is currently pending')).toBeInTheDocument()
    );
    expect(screen.getByRole('button', { name: /restart now/i })).not.toBeDisabled();
    // The underlying condition is still pending, so the notice stays up.
    expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
  });

  /**
   * The #4043 acceptance failure, from this component's side.
   *
   * The backend refused the restart (`nyxgpt-api@3.0.0rc` failed an injection
   * barrier with no `@` in it) and correctly left the pending flag standing.
   * But the flag standing is *also* what a restart still coming back looks
   * like, and the pending set was the only thing polled here -- so the button
   * said "Restarting…" for ninety seconds and then blamed the clock for a
   * restart that had never been attempted. The attempt outcome is now part of
   * restart-status, and these pin that this component reads it.
   */
  describe('a restart the backend reports as failed', () => {
    const REFUSAL = "Refused to act on invalid service name: 'nyxgpt-api@3.0.0rc'";

    const failedStatus = {
      pending: { api: { keys: ['api.port'], since: Math.floor(Date.now() / 1000) } },
      attempts: { api: { status: 'failed', message: REFUSAL, at: Date.now() / 1000 } },
      restart_command: 'nyxgpt ops restart api',
      session_disrupting: [],
    };

    it('ends the poll with the reason instead of waiting out the timeout', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        server.use(
          http.post('/api/v1/infra/restart-required', () =>
            HttpResponse.json({ targets: ['api'], status: 'scheduled' })
          ),
          http.get('/api/v1/infra/restart-status', () => HttpResponse.json(failedStatus))
        );

        render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

        // One poll interval is enough: the answer is in the first response.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1500);
        });

        await waitFor(() =>
          expect(screen.getByText(/did not happen/i)).toBeInTheDocument()
        );
        expect(screen.getByText(new RegExp(REFUSAL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))))
          .toBeInTheDocument();
        // Not the clock's fault, and not a 90-second wait to say so.
        expect(screen.queryByText(/did not report finished in time/i)).not.toBeInTheDocument();
        // Retryable, and the notice stays: the settings are still pending.
        expect(screen.getByRole('button', { name: /restart now/i })).not.toBeDisabled();
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it('points at the wrapped CLI command, never a raw brew/docker/kubectl one', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        server.use(
          http.post('/api/v1/infra/restart-required', () =>
            HttpResponse.json({ targets: ['api'], status: 'scheduled' })
          ),
          http.get('/api/v1/infra/restart-status', () => HttpResponse.json(failedStatus))
        );

        render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /restart now/i }));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1500);
        });

        await waitFor(() => expect(screen.getByText(/did not happen/i)).toBeInTheDocument());
        expect(document.body.textContent).toMatch(/nyxgpt ops restart api/);
        expect(document.body.textContent).toMatch(/nyxgpt self-heal status/);
        expect(document.body.textContent).not.toMatch(/docker compose|brew services|kubectl/);
      } finally {
        vi.useRealTimers();
      }
    });

    it('shows a failure recorded before this page loaded', () => {
      // The notice outlives the page that started the restart (that is the
      // whole point of it being server state), so its explanation has to too:
      // a user who reloads after a refusal must not be shown a bare notice
      // and a button that already failed silently once.
      render(<PendingRestartNotice status={failedStatus as RestartStatus} onStatusChange={vi.fn()} />);
      expect(screen.getByText(/last restart attempt did not happen/i)).toBeInTheDocument();
      expect(screen.getByText(new RegExp('nyxgpt-api@3\\.0\\.0rc'))).toBeInTheDocument();
    });

    it('ignores an attempt recorded for a component that is no longer pending', () => {
      // A stale record must not conjure an error onto a notice about something
      // else -- the backend drops it on a new save for the same reason.
      render(
        <PendingRestartNotice
          status={{
            ...API_PENDING,
            attempts: { web: { status: 'failed', message: 'an old failure', at: 1 } },
          }}
          onStatusChange={vi.fn()}
        />
      );
      expect(screen.queryByText(/did not happen/i)).not.toBeInTheDocument();
    });

    it('does not treat a running attempt as a failure', () => {
      render(
        <PendingRestartNotice
          status={{
            ...API_PENDING,
            attempts: { api: { status: 'running', message: '', at: 1 } },
          }}
          onStatusChange={vi.fn()}
        />
      );
      expect(screen.queryByText(/did not happen/i)).not.toBeInTheDocument();
    });
  });
});

describe('fetchRestartStatus', () => {
  it('normalizes a response and returns null on failure so callers keep their state', async () => {
    server.use(
      http.get('/api/v1/infra/restart-status', () =>
        HttpResponse.json({ pending: { web: { keys: ['auth.api_key'], since: 1 } } })
      )
    );
    const status = await fetchRestartStatus();
    expect(status).not.toBeNull();
    expect(status!.pending.web.keys).toEqual(['auth.api_key']);
    expect(status!.restart_command).toBeNull();

    server.use(
      http.get('/api/v1/infra/restart-status', () => HttpResponse.json({}, { status: 502 }))
    );
    expect(await fetchRestartStatus()).toBeNull();
  });
});

/**
 * How long the divergence has been standing is part of what makes the notice
 * a *persistent* one rather than a toast: a user returning days later should
 * be told it has been days, not "just now". Each unit boundary and each
 * singular/plural form is a branch, and an unexercised one renders the wrong
 * text at exactly the moment the notice matters most.
 */
describe('the age of a pending change', () => {
  const secondsAgo = (seconds: number): RestartStatus => ({
    pending: { web: { keys: ['auth.api_key'], since: Math.floor(Date.now() / 1000) - seconds } },
    attempts: {},
    restart_command: 'nyxgpt ops restart web',
    session_disrupting: ['web'],
  });

  it.each([
    [5, 'changed just now'],
    [90, 'changed 1 minute ago'],
    [150, 'changed 2 minutes ago'],
    [60 * 60, 'changed 1 hour ago'],
    [2 * 60 * 60, 'changed 2 hours ago'],
    [24 * 60 * 60, 'changed 1 day ago'],
    [3 * 24 * 60 * 60, 'changed 3 days ago'],
  ])('renders %i seconds ago as "%s"', (seconds, expected) => {
    render(<PendingRestartNotice status={secondsAgo(seconds)} onStatusChange={vi.fn()} />);
    expect(screen.getByText(`(${expected})`)).toBeInTheDocument();
  });

  it('never reports a future timestamp as a negative age', () => {
    // Clock skew between the API host and the browser is normal; "changed
    // -3 minutes ago" would read as a bug in the notice itself.
    render(<PendingRestartNotice status={secondsAgo(-3600)} onStatusChange={vi.fn()} />);
    expect(screen.getByText('(changed just now)')).toBeInTheDocument();
  });
});

/**
 * `failedAttemptText` (PendingRestartNotice.tsx:116-124).
 *
 * The comparator on :120 only runs when there are at least TWO failed
 * attempts, so a single-failure test leaves it uncovered -- which is how it
 * reached the release branch untested. Ordering matters here for a plain
 * reason: the notice is read by someone deciding what to fix first, and a set
 * of failures that reorders itself between renders is harder to act on than
 * one that does not.
 */
/**
 * `next.restart_command ?? 'nyxgpt ops restart'` (:207).
 *
 * When a restart fails, the notice tells the user what to run by hand. The
 * sibling case -- a status that names its own command -- is already covered;
 * this is the other side: a status that carries none must still name a command,
 * because "the restart did not happen" with no next step is precisely the
 * dead end this notice exists to avoid. Unwrapped commands stay out of it
 * either way (the 2026-07-15 wrapping rule).
 */
describe('failed restart whose status names no command', () => {
  it('falls back to the generic wrapped restart command', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const noCommand = {
        pending: { api: { keys: ['api.port'], since: Math.floor(Date.now() / 1000) } },
        attempts: { api: { status: 'failed', message: 'unit not loaded', at: Date.now() / 1000 } },
        restart_command: null,
        session_disrupting: [],
      };
      server.use(
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ targets: ['api'], status: 'scheduled' })
        ),
        http.get('/api/v1/infra/restart-status', () => HttpResponse.json(noCommand))
      );

      render(<PendingRestartNotice status={API_PENDING} onStatusChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });

      await waitFor(() => expect(screen.getByText(/did not happen/i)).toBeInTheDocument());
      // The bare wrapper, with no service argument to append.
      expect(document.body.textContent).toMatch(/nyxgpt ops restart[^ ]/);
      expect(document.body.textContent).not.toMatch(/docker compose|brew services|kubectl/);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('failedAttemptText', () => {
  const status = (attempts: Record<string, { status: string; message?: string }>, pending: Record<string, unknown>) =>
    ({ attempts, pending }) as never;

  it('returns null without a status at all', () => {
    expect(failedAttemptText(null)).toBeNull();
  });

  it('returns null when nothing failed', () => {
    expect(failedAttemptText(status({ api: { status: 'ok' } }, { api: true }))).toBeNull();
  });

  it('names a single failure with its own message', () => {
    expect(
      failedAttemptText(status({ api: { status: 'failed', message: 'Refused to act on invalid service name' } }, { api: true }))
    ).toBe('api: Refused to act on invalid service name');
  });

  it('orders several failures by component, whatever order they arrived in', () => {
    const text = failedAttemptText(
      status(
        {
          web: { status: 'failed', message: 'port 3000 busy' },
          api: { status: 'failed', message: 'unit not loaded' },
          ollama: { status: 'failed', message: 'not installed' },
        },
        { web: true, api: true, ollama: true }
      )
    );
    expect(text).toBe('api: unit not loaded\nollama: not installed\nweb: port 3000 busy');
  });

  it('falls back to a plain sentence when a failure carries no message', () => {
    expect(
      failedAttemptText(status({ api: { status: 'failed', message: '' }, web: { status: 'failed' } }, { api: true, web: true }))
    ).toBe('api: the restart did not happen\nweb: the restart did not happen');
  });

  it('ignores a failed attempt for a component that is no longer pending', () => {
    expect(
      failedAttemptText(status({ api: { status: 'failed', message: 'stale' } }, { web: true }))
    ).toBeNull();
  });
});

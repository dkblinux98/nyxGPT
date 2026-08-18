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
  type RestartStatus,
} from '../../src/components/PendingRestartNotice';

const WEB_PENDING: RestartStatus = {
  pending: { web: { keys: ['auth.api_key'], since: Math.floor(Date.now() / 1000) } },
  restart_command: 'nyxgpt ops restart web',
  session_disrupting: ['web'],
};

const API_PENDING: RestartStatus = {
  pending: { api: { keys: ['api.port'], since: Math.floor(Date.now() / 1000) } },
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
        status={{ pending: {}, restart_command: null, session_disrupting: [] }}
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
          HttpResponse.json({ targets: ['web'], status: 'running' })
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
      const post = vi.fn(() => HttpResponse.json({ targets: ['web'], status: 'running' }));
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
          HttpResponse.json({ targets: ['api'], status: 'running' })
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
          HttpResponse.json({ targets: ['api'], status: 'running' })
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

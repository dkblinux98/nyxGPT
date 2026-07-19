import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import ErrorTrackingPanel from '../../src/components/ErrorTrackingPanel';
import { server } from '../mocks/server';

const disabledStatus = {
  enabled: false,
  active: false,
  dsn: '',
  environment: 'development',
  glitchtip_ui_url: 'http://localhost:8080',
};

const activeStatus = {
  enabled: true,
  active: true,
  dsn: 'http://key@glitchtip:8080/1',
  environment: 'production',
  glitchtip_ui_url: 'http://localhost:8080',
};

describe('ErrorTrackingPanel', () => {
  it('shows Python-specific, zero-touch guidance when inactive -- not the GlitchTip Node.js onboarding', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)));

    render(<ErrorTrackingPanel />);

    await waitFor(() => {
      expect(screen.getByText(/error tracking is not yet active/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/sentry_sdk/)).toBeInTheDocument();
    expect(screen.getByText(/ignore them/i)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt ops glitchtip-init/)).toBeInTheDocument();
  });

  it('surfaces an error when the status request fails outright', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.error()));

    render(<ErrorTrackingPanel />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('surfaces an error when the status response is not ok', async () => {
    server.use(http.get('/api/v1/error-tracking', () => new HttpResponse(null, { status: 500 })));

    render(<ErrorTrackingPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('reports a non-Error status failure with a stringified reason', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('plain string failure');

    render(<ErrorTrackingPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });

  it('does not update state after unmounting before the status request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    server.use(
      http.get(
        '/api/v1/error-tracking',
        () =>
          new Promise((resolve) => {
            resolveRequest = () => resolve(HttpResponse.json(activeStatus));
          })
      )
    );

    const { unmount } = render(<ErrorTrackingPanel />);

    await vi.waitFor(() => {
      expect(resolveRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      resolveRequest?.();
    }).not.toThrow();
  });

  it('does not update state after unmounting before the status request rejects', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined;
    server.use(
      http.get(
        '/api/v1/error-tracking',
        () =>
          new Promise((_resolve, reject) => {
            rejectRequest = reject;
          })
      )
    );

    const { unmount } = render(<ErrorTrackingPanel />);

    await vi.waitFor(() => {
      expect(rejectRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      rejectRequest?.(new Error('network error'));
    }).not.toThrow();
  });

  it('links to the GlitchTip UI and shows the DSN when active', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)));

    render(<ErrorTrackingPanel />);

    const link = await screen.findByRole('link', { name: /open glitchtip ui/i });
    expect(link).toHaveAttribute('href', 'http://localhost:8080');
    expect(screen.getByText(activeStatus.dsn)).toBeInTheDocument();
  });

  it('reports a delivered test event when tracking is active', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)),
      http.post('/api/v1/error-tracking/report', () =>
        HttpResponse.json({ status: 'accepted' }, { status: 202 })
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent(/delivered/i);
  });

  it('surfaces an unexpected response status without masking it as delivered', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)),
      http.post('/api/v1/error-tracking/report', () =>
        HttpResponse.json({ status: 'error' }, { status: 500 })
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent(/unexpected response: http 500/i);
  });

  it('surfaces a network error when sending the test event fails outright', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)),
      http.post('/api/v1/error-tracking/report', () => HttpResponse.error())
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toBeInTheDocument();
  });

  it('reports a non-Error test-event failure with a stringified reason', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)));

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });

    const originalFetch = global.fetch;
    global.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/error-tracking/report')) {
        return Promise.reject('plain string failure');
      }
      return originalFetch(input, init);
    });

    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent('Network error sending test event');

    global.fetch = originalFetch;
  });

  it('falls back to the default JSON body when the test-event response is not valid JSON', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)),
      http.post('/api/v1/error-tracking/report', () => new HttpResponse('not json', { status: 500 }))
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent(/unexpected response: http 500/i);
  });

  it('surfaces inactivity instead of masking it as delivered', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.post('/api/v1/error-tracking/report', () =>
        HttpResponse.json(
          { status: 'inactive', detail: 'Error tracking is disabled or has no valid DSN configured; this event was not sent.' },
          { status: 503 }
        )
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent(/not sent/i);
  });

  it('falls back to the default inactivity message when the response has no detail', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.post('/api/v1/error-tracking/report', () => HttpResponse.json({ status: 'inactive' }, { status: 503 }))
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /send test event/i });
    await userEvent.click(button);

    expect(await screen.findByRole('status')).toHaveTextContent(
      /error tracking is inactive -- the test event was not sent/i
    );
  });

  it('shows the GlitchTip container logs inline so the confirmation link is reachable from the dashboard', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.get('/api/v1/self-heal/logs', () =>
        HttpResponse.json({
          service: 'glitchtip',
          tail: 100,
          logs: 'confirm your account: http://localhost:8080/accounts/confirm/abc123',
        })
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });
    await userEvent.click(button);

    expect(await screen.findByText(/confirm your account/i)).toBeInTheDocument();
  });

  it('surfaces an error when the logs endpoint fails', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.get('/api/v1/self-heal/logs', () =>
        HttpResponse.json({ error: { message: 'Failed to fetch logs for glitchtip' } }, { status: 502 })
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });
    await userEvent.click(button);

    expect(await screen.findByRole('alert')).toHaveTextContent(/failed to fetch logs for glitchtip/i);
  });

  it('falls back to the detail field when the logs error has no message', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.get('/api/v1/self-heal/logs', () =>
        HttpResponse.json({ detail: 'glitchtip container is not running' }, { status: 502 })
      )
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });
    await userEvent.click(button);

    expect(await screen.findByRole('alert')).toHaveTextContent(/glitchtip container is not running/i);
  });

  it('falls back to a bare HTTP status when the logs error has neither message nor detail', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.get('/api/v1/self-heal/logs', () => new HttpResponse('not json', { status: 502 }))
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });
    await userEvent.click(button);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 502/);
  });

  it('shows a placeholder when the logs endpoint returns no log output', async () => {
    server.use(
      http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)),
      http.get('/api/v1/self-heal/logs', () => HttpResponse.json({ service: 'glitchtip', tail: 100, logs: '' }))
    );

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });
    await userEvent.click(button);

    expect(await screen.findByText('(no output)')).toBeInTheDocument();
  });

  it('reports a non-Error logs failure with a stringified reason', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)));

    render(<ErrorTrackingPanel />);
    const button = await screen.findByRole('button', { name: /view glitchtip logs/i });

    const originalFetch = global.fetch;
    global.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/self-heal/logs')) {
        return Promise.reject('plain string failure');
      }
      return originalFetch(input, init);
    });

    await userEvent.click(button);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });
});

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
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
  it('shows Python-specific, step-by-step guidance when inactive -- not the GlitchTip Node.js onboarding', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(disabledStatus)));

    render(<ErrorTrackingPanel />);

    await waitFor(() => {
      expect(screen.getByText(/error tracking is disabled/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/sentry_sdk/)).toBeInTheDocument();
    expect(screen.getByText(/ignore them/i)).toBeInTheDocument();
    expect(screen.getByText(/nyxgpt ops logs glitchtip/)).toBeInTheDocument();
  });

  it('links to the GlitchTip UI when active', async () => {
    server.use(http.get('/api/v1/error-tracking', () => HttpResponse.json(activeStatus)));

    render(<ErrorTrackingPanel />);

    const link = await screen.findByRole('link', { name: /open glitchtip ui/i });
    expect(link).toHaveAttribute('href', 'http://localhost:8080');
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
});

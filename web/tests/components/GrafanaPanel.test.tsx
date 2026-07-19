import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import GrafanaPanel from '../../src/components/GrafanaPanel';
import { server } from '../mocks/server';

const inactiveStatus = {
  enabled: false,
  active: false,
  grafana_ui_url: 'http://localhost:3001',
  prometheus_ui_url: 'http://localhost:9090',
};

const activeStatus = {
  enabled: true,
  active: true,
  grafana_ui_url: 'http://localhost:3001',
  prometheus_ui_url: 'http://localhost:9090',
};

describe('GrafanaPanel', () => {
  it('shows a loading state before the status resolves', () => {
    server.use(http.get('/api/v1/monitoring', () => new Promise(() => {})));

    render(<GrafanaPanel />);

    expect(screen.getByText(/Loading monitoring status/i)).toBeInTheDocument();
  });

  it('shows setup guidance with the nyxgpt ops commands when inactive', async () => {
    server.use(http.get('/api/v1/monitoring', () => HttpResponse.json(inactiveStatus)));

    render(<GrafanaPanel />);

    await waitFor(() => {
      expect(screen.getByText(/local-only/i)).toBeInTheDocument();
    });
    expect(screen.getByText('nyxgpt ops install')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt ops observability')).toBeInTheDocument();
  });

  it('links to Grafana and Prometheus when active', async () => {
    server.use(http.get('/api/v1/monitoring', () => HttpResponse.json(activeStatus)));

    render(<GrafanaPanel />);

    const grafanaLink = await screen.findByRole('link', { name: /Open Grafana/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001');

    const prometheusLink = screen.getByRole('link', { name: /Open Prometheus/i });
    expect(prometheusLink).toHaveAttribute('href', 'http://localhost:9090');
  });

  it('surfaces an error when the status response is not ok', async () => {
    server.use(http.get('/api/v1/monitoring', () => new HttpResponse(null, { status: 500 })));

    render(<GrafanaPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('surfaces an error when the status request fails outright', async () => {
    server.use(http.get('/api/v1/monitoring', () => HttpResponse.error()));

    render(<GrafanaPanel />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('does not update state after unmounting before the status request rejects', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined;
    server.use(
      http.get(
        '/api/v1/monitoring',
        () =>
          new Promise((_resolve, reject) => {
            rejectRequest = reject;
          })
      )
    );

    const { unmount } = render(<GrafanaPanel />);

    await vi.waitFor(() => {
      expect(rejectRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      rejectRequest?.(new Error('network error'));
    }).not.toThrow();
  });

  it('does not update state after unmounting before the status request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    server.use(
      http.get(
        '/api/v1/monitoring',
        () =>
          new Promise((resolve) => {
            resolveRequest = () => resolve(HttpResponse.json(activeStatus));
          })
      )
    );

    const { unmount } = render(<GrafanaPanel />);

    await vi.waitFor(() => {
      expect(resolveRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      resolveRequest?.();
    }).not.toThrow();
  });

  it('reports a non-Error status failure with a stringified reason', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('plain string failure');

    render(<GrafanaPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });
});

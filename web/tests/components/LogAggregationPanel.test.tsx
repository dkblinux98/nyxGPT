import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import LogAggregationPanel from '../../src/components/LogAggregationPanel';
import { server } from '../mocks/server';

const inactiveStatus = {
  enabled: false,
  active: false,
  grafana_explore_url: 'http://localhost:3001/explore',
  curated_queries: [],
};

const activeStatusWithQueries = {
  enabled: true,
  active: true,
  grafana_explore_url: 'http://localhost:3001/explore',
  curated_queries: [
    { label: 'Errors only', hint: 'Filters to ERROR level lines', query: '{job="nyxgpt"} |= "ERROR"' },
  ],
};

const activeStatusNoQueries = {
  enabled: true,
  active: true,
  grafana_explore_url: 'http://localhost:3001/explore',
  curated_queries: [],
};

describe('LogAggregationPanel', () => {
  it('shows a loading state before the status resolves', () => {
    server.use(http.get('/api/v1/log-aggregation', () => new Promise(() => {})));

    render(<LogAggregationPanel />);

    expect(screen.getByText(/Loading log aggregation status/i)).toBeInTheDocument();
  });

  it('shows setup guidance with the nyxgpt ops commands when inactive', async () => {
    server.use(http.get('/api/v1/log-aggregation', () => HttpResponse.json(inactiveStatus)));

    render(<LogAggregationPanel />);

    await waitFor(() => {
      expect(screen.getByText(/local-only/i)).toBeInTheDocument();
    });
    expect(screen.getByText('nyxgpt ops install')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt ops observability')).toBeInTheDocument();
  });

  it('links to Grafana Explore and lists curated queries when active', async () => {
    server.use(http.get('/api/v1/log-aggregation', () => HttpResponse.json(activeStatusWithQueries)));

    render(<LogAggregationPanel />);

    const link = await screen.findByRole('link', { name: /Open Grafana Explore/i });
    expect(link).toHaveAttribute('href', 'http://localhost:3001/explore');

    expect(screen.getByText('Curated saved queries')).toBeInTheDocument();
    expect(screen.getByText('Errors only')).toBeInTheDocument();
    expect(screen.getByText('Filters to ERROR level lines')).toBeInTheDocument();
    expect(screen.getByText('{job="nyxgpt"} |= "ERROR"')).toBeInTheDocument();
  });

  it('omits the curated queries section when active but none are configured', async () => {
    server.use(http.get('/api/v1/log-aggregation', () => HttpResponse.json(activeStatusNoQueries)));

    render(<LogAggregationPanel />);

    await screen.findByRole('link', { name: /Open Grafana Explore/i });

    expect(screen.queryByText('Curated saved queries')).not.toBeInTheDocument();
  });

  it('surfaces an error when the status response is not ok', async () => {
    server.use(http.get('/api/v1/log-aggregation', () => new HttpResponse(null, { status: 500 })));

    render(<LogAggregationPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('surfaces an error when the status request fails outright', async () => {
    server.use(http.get('/api/v1/log-aggregation', () => HttpResponse.error()));

    render(<LogAggregationPanel />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('does not update state after unmounting before the status request rejects', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined;
    server.use(
      http.get(
        '/api/v1/log-aggregation',
        () =>
          new Promise((_resolve, reject) => {
            rejectRequest = reject;
          })
      )
    );

    const { unmount } = render(<LogAggregationPanel />);

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
        '/api/v1/log-aggregation',
        () =>
          new Promise((resolve) => {
            resolveRequest = () => resolve(HttpResponse.json(activeStatusWithQueries));
          })
      )
    );

    const { unmount } = render(<LogAggregationPanel />);

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

    render(<LogAggregationPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });
});

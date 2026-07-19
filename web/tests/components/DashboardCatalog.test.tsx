import { describe, it, expect, vi } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import DashboardCatalog from '../../src/components/DashboardCatalog';
import { server } from '../mocks/server';

const CATALOG_SOURCE = readFileSync(
  join(__dirname, '../../src/components/DashboardCatalog.tsx'),
  'utf-8'
);

const DASHBOARDS_DIR = join(__dirname, '../../../docker/grafana/dashboards');

function catalogUids(): string[] {
  return [...CATALOG_SOURCE.matchAll(/uid: '([^']+)'/g)].map((match) => match[1]);
}

function provisionedUids(): string[] {
  return readdirSync(DASHBOARDS_DIR)
    .filter((file) => file.endsWith('.json'))
    .map((file) => JSON.parse(readFileSync(join(DASHBOARDS_DIR, file), 'utf-8')).uid);
}

describe('DashboardCatalog uid/dashboard consistency', () => {
  it('lists every dashboard provisioned under docker/grafana/dashboards', () => {
    const catalog = catalogUids();
    const provisioned = provisionedUids();

    for (const uid of provisioned) {
      expect(catalog).toContain(uid);
    }
  });

  it('does not reference a uid that is not actually provisioned', () => {
    const catalog = catalogUids();
    const provisioned = provisionedUids();

    for (const uid of catalog) {
      expect(provisioned).toContain(uid);
    }
  });
});

describe('DashboardCatalog rendering', () => {
  it('renders nothing while monitoring status is loading', () => {
    server.use(http.get('/api/v1/monitoring', () => new Promise(() => {})));

    const { container } = render(<DashboardCatalog />);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when monitoring is inactive', async () => {
    server.use(
      http.get('/api/v1/monitoring', () =>
        HttpResponse.json({
          enabled: false,
          active: false,
          grafana_ui_url: 'http://localhost:3001',
          prometheus_ui_url: 'http://localhost:9090',
        })
      )
    );

    const { container } = render(<DashboardCatalog />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders nothing when the monitoring status request fails', async () => {
    server.use(http.get('/api/v1/monitoring', () => HttpResponse.error()));

    const { container } = render(<DashboardCatalog />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders nothing when the monitoring status response is not ok', async () => {
    server.use(http.get('/api/v1/monitoring', () => new HttpResponse(null, { status: 500 })));

    const { container } = render(<DashboardCatalog />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders the dashboard catalog grouped by category when monitoring is active', async () => {
    server.use(
      http.get('/api/v1/monitoring', () =>
        HttpResponse.json({
          enabled: true,
          active: true,
          grafana_ui_url: 'http://localhost:3001',
          prometheus_ui_url: 'http://localhost:9090',
        })
      )
    );

    render(<DashboardCatalog />);

    await waitFor(() => {
      expect(screen.getByText('Dashboard Catalog')).toBeInTheDocument();
    });

    expect(screen.getByText('App functionality')).toBeInTheDocument();
    expect(screen.getByText('Self-healing & deployment')).toBeInTheDocument();
    expect(screen.getByText('Logs')).toBeInTheDocument();

    const link = screen.getByRole('link', { name: /System Overview/i });
    expect(link).toHaveAttribute('href', 'http://localhost:3001/d/nyxgpt-system-overview');
  });

  it('does not update state after unmounting before the monitoring status request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    server.use(
      http.get(
        '/api/v1/monitoring',
        () =>
          new Promise((resolve) => {
            resolveRequest = () =>
              resolve(
                HttpResponse.json({
                  enabled: true,
                  active: true,
                  grafana_ui_url: 'http://localhost:3001',
                  prometheus_ui_url: 'http://localhost:9090',
                })
              );
          })
      )
    );

    const { unmount } = render(<DashboardCatalog />);

    await vi.waitFor(() => {
      expect(resolveRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      resolveRequest?.();
    }).not.toThrow();
  });
});

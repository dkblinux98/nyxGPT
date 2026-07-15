import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import LogsPage from '../../../src/app/admin/logs/page';

const mockLogAggregationActive = {
  enabled: true,
  active: true,
  grafana_explore_url: 'http://localhost:3001/explore',
};

const mockLogAggregationDisabled = {
  enabled: false,
  active: false,
  grafana_explore_url: 'http://localhost:3001/explore',
};

describe('LogsPage', () => {
  it('renders the heading and back link', async () => {
    server.use(
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Log Viewer' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('explains that this viewer only covers nyxGPT API logs, not Ollama/other components', async () => {
    server.use(
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText(/does not include logs from Ollama or other components/i)).toBeInTheDocument();
    });
  });

  it('renders the Log Aggregation panel with a Grafana Explore link when active', async () => {
    server.use(
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationActive))
    );
    render(<LogsPage />);

    expect(await screen.findByText('Log Aggregation')).toBeInTheDocument();
    const grafanaLink = await screen.findByRole('link', { name: /Open Grafana Explore/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/explore');
  });

  it('renders the Log Aggregation panel disabled state', async () => {
    server.use(
      http.get('/api/v1/log-aggregation', () => HttpResponse.json(mockLogAggregationDisabled))
    );
    render(<LogsPage />);

    expect(await screen.findByText('Log Aggregation')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/searched centrally in Grafana/i)).toBeInTheDocument();
    });
  });
});

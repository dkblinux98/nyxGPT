import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import ResourceMetrics from '../../src/components/ResourceMetrics';
import { server } from '../mocks/server';

const mockMetricsData = {
  memory: { rss_mb: 256.5, vms_mb: 512.0, percent: 15.2, available_mb: 8192.0 },
  cpu: { process_percent: 5.3, system_percent: 25.8 },
  latency: { avg_ms: 45.2, p50_ms: 42.0, p95_ms: 85.5, p99_ms: 120.3 },
  queue: { depth: 3, total_requests: 1250 },
};

describe('ResourceMetrics (admin dashboard)', () => {
  it('shows a link to the Observability page instead of embedding the dashboards inline', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Observability')).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /Open Observability/i });
    expect(link).toHaveAttribute('href', '/admin/observability');

    // The Grafana/Prometheus/Jaeger/GlitchTip panels were promoted out of
    // this component onto the Observability page -- they should not render
    // (and their status endpoints should not be fetched) here anymore.
    expect(screen.queryByText('Monitoring Dashboards')).not.toBeInTheDocument();
    expect(screen.queryByText('Distributed Tracing')).not.toBeInTheDocument();
    expect(screen.queryByText('Error Tracking')).not.toBeInTheDocument();
    expect(screen.queryByText('Log Aggregation')).not.toBeInTheDocument();
    expect(screen.queryByText('Prometheus Endpoint')).not.toBeInTheDocument();
  });
});

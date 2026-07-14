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

const mockTracingDisabled = {
  enabled: false,
  active: false,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318/v1/traces',
  jaeger_ui_url: 'http://localhost:16686',
};

const mockTracingActive = {
  enabled: true,
  active: true,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://otel-collector:4318/v1/traces',
  jaeger_ui_url: 'http://localhost:16686',
};

const mockErrorTrackingDisabled = {
  enabled: false,
  active: false,
  dsn: '',
  environment: 'development',
  glitchtip_ui_url: 'http://localhost:8080',
};

const mockErrorTrackingActive = {
  enabled: true,
  active: true,
  dsn: 'http://key@glitchtip:8080/1',
  environment: 'production',
  glitchtip_ui_url: 'http://localhost:8080',
};

describe('ResourceMetrics (admin dashboard)', () => {
  it('shows a Prometheus scrape endpoint card with a link to view current metrics', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled))
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Prometheus Endpoint')).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /View current metrics/i });
    expect(link).toHaveAttribute('href', '/api/prometheus-metrics');
    expect(screen.getByText('<nyxgpt-api-host>/metrics')).toBeInTheDocument();
  });

  it('shows a Distributed Tracing card', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled))
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Distributed Tracing')).toBeInTheDocument();
    });
  });

  it('shows an active Distributed Tracing card with a link to the local Jaeger UI', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingActive)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled))
    );

    render(<ResourceMetrics />);

    const link = await screen.findByRole('link', { name: /Open Jaeger UI/i });
    expect(link).toHaveAttribute('href', 'http://localhost:16686');
    expect(screen.getByText('http://otel-collector:4318/v1/traces')).toBeInTheDocument();
  });

  it('shows an error on the Distributed Tracing card when the status fetch fails', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => new HttpResponse(null, { status: 500 })),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled))
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Failed to fetch tracing status: HTTP 500/i);
    });
  });

  it('shows an Error Tracking card', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingDisabled))
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Error Tracking')).toBeInTheDocument();
    });
  });

  it('shows an active Error Tracking card with a link to the local GlitchTip UI', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => HttpResponse.json(mockErrorTrackingActive))
    );

    render(<ResourceMetrics />);

    const link = await screen.findByRole('link', { name: /Open GlitchTip UI/i });
    expect(link).toHaveAttribute('href', 'http://localhost:8080');
    expect(screen.getByText('production')).toBeInTheDocument();
  });

  it('shows an error on the Error Tracking card when the status fetch fails', async () => {
    server.use(
      http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)),
      http.get('/api/v1/tracing', () => HttpResponse.json(mockTracingDisabled)),
      http.get('/api/v1/error-tracking', () => new HttpResponse(null, { status: 500 }))
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        /Failed to fetch error tracking status: HTTP 500/i
      );
    });
  });
});

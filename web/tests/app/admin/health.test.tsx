import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import AdminHealthPage from '../../../src/app/admin/health/page';

const emptyUsageSummary = {
  total_requests: 0,
  total_prompt_tokens: 0,
  total_completion_tokens: 0,
  total_tokens: 0,
  session_count: 0,
  by_model: [],
  by_day: [],
};

const emptyMetricsHistory = {
  range: '1h',
  points: [],
  sample_interval_seconds: 60,
  requested_window_seconds: 3600,
  earliest_available_ts: null,
  history_available_seconds: 0,
};

const zeroMetrics = {
  memory: { rss_mb: 0, vms_mb: 0, percent: 0, available_mb: 0 },
  cpu: { process_percent: 0, system_percent: 0 },
  latency: { avg_ms: 0, p50_ms: 0, p95_ms: 0, p99_ms: 0 },
  queue: { depth: 0, total_requests: 0 },
};

describe('AdminHealthPage', () => {
  // The consolidated screen (#3413) also mounts the Usage Analytics and
  // Resource Metrics sections on every render, so every test needs default
  // handlers for their endpoints -- individual tests below override these
  // via their own `server.use()` where they care about the response.
  beforeEach(() => {
    server.use(
      http.get('/api/v1/analytics/usage', () => HttpResponse.json(emptyUsageSummary)),
      http.get('/api/metrics', () => HttpResponse.json(zeroMetrics)),
      http.get('/api/v1/metrics/history', () => HttpResponse.json(emptyMetricsHistory))
    );
  });

  it('renders the heading and back link', async () => {
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'System Health' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('renders service status with uptime', async () => {
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/Service: ok/)).toBeInTheDocument();
    });
    expect(screen.getByText(/1h 2m 5s/)).toBeInTheDocument();
  });

  it('renders dependency checks', async () => {
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/ollama: healthy/)).toBeInTheDocument();
    });
    expect(screen.getByText(/cassandra: not applicable/)).toBeInTheDocument();
  });

  it('renders resource utilization', async () => {
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/128 MB/)).toBeInTheDocument();
    });
    expect(screen.getByText(/2.5%/)).toBeInTheDocument();
  });

  it('shows no active alerts when none are present', async () => {
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText('No active alerts.')).toBeInTheDocument();
    });
  });

  it('renders alert indicators when present', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 60 },
          dependencies: [
            { name: 'ollama', ok: false, detail: 'Connection refused', applicable: true },
          ],
          resource_metrics: null,
          alerts_source: 'local',
          alerts: [
            {
              severity: 'critical',
              message: "Dependency 'ollama' is unreachable: Connection refused",
              source: 'local',
            },
          ],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(screen.getByText(/unreachable: Connection refused/)).toBeInTheDocument();
  });

  it('labels alerts as live from Grafana when alerts_source is grafana', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 60 },
          dependencies: [],
          resource_metrics: null,
          alerts_source: 'grafana',
          alerts: [
            { severity: 'critical', message: 'CPU usage above 95%', source: 'grafana' },
          ],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/Live from Grafana alerting/)).toBeInTheDocument();
    });
  });

  it('labels alerts as a local estimate when alerts_source is local', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 60 },
          dependencies: [],
          resource_metrics: null,
          alerts_source: 'local',
          alerts: [],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/Local estimate/)).toBeInTheDocument();
    });
  });

  it('shows an error message when the request fails', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return new HttpResponse(null, { status: 500 });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/Failed to load system health/).length).toBeGreaterThan(0);
    });
  });

  it('shows a string error message when a non-Error value is thrown', async () => {
    // Routed by URL rather than mockRejectedValueOnce: the page now mounts
    // sibling sections (Usage Analytics, Resource Metrics) that also fetch
    // on mount, and a call-order-based one-time rejection could land on any
    // of them instead of the health endpoint this test targets.
    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/admin/health')) {
        return Promise.reject('boom');
      }
      return realFetch(input, init);
    });
    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/boom/).length).toBeGreaterThan(0);
    });
    fetchSpy.mockRestore();
  });

  it('formats uptime including days for long-running services', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 100000 },
          dependencies: [],
          resource_metrics: null,
          alerts: [],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText(/1d 3h 46m 40s/)).toBeInTheDocument();
    });
  });

  it('formats uptime under a minute with only seconds', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 5 },
          dependencies: [],
          resource_metrics: null,
          alerts: [],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText('5s')).toBeInTheDocument();
    });
    expect(screen.queryByText(/0m/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0h/)).not.toBeInTheDocument();
  });

  it('shows resource metrics unavailable when metrics are null', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 60 },
          dependencies: [],
          resource_metrics: null,
          alerts: [],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByText('Resource metrics unavailable.')).toBeInTheDocument();
    });
  });

  it('renders a warning-severity alert distinctly from critical', async () => {
    server.use(
      http.get('/api/v1/admin/health', () => {
        return HttpResponse.json({
          service: { status: 'ok', uptime_s: 60 },
          dependencies: [],
          resource_metrics: null,
          alerts_source: 'local',
          alerts: [{ severity: 'warning', message: 'Queue depth elevated', source: 'local' }],
        });
      })
    );

    render(<AdminHealthPage />);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(screen.getByText(/Queue depth elevated/)).toBeInTheDocument();
    expect(screen.getByText('warning')).toBeInTheDocument();
  });

  describe('consolidated screen sections (#3413)', () => {
    it('renders section anchor jump links for all three sections', async () => {
      render(<AdminHealthPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'System Health' })).toBeInTheDocument();
      });

      const nav = screen.getByRole('navigation', { name: 'System Health sections' });
      expect(within(nav).getByRole('link', { name: 'Service Health' })).toHaveAttribute(
        'href',
        '#service-health'
      );
      expect(within(nav).getByRole('link', { name: 'Usage Analytics' })).toHaveAttribute(
        'href',
        '#usage-analytics'
      );
      expect(within(nav).getByRole('link', { name: 'Resource Metrics' })).toHaveAttribute(
        'href',
        '#resource-metrics'
      );
    });

    it('renders the Usage Analytics section with data from the analytics endpoint', async () => {
      server.use(
        http.get('/api/v1/analytics/usage', () =>
          HttpResponse.json({
            total_requests: 42,
            total_prompt_tokens: 100,
            total_completion_tokens: 50,
            total_tokens: 150,
            session_count: 3,
            by_model: [{ model: 'llama3.1:8b', requests: 42, prompt_tokens: 100, completion_tokens: 50 }],
            by_day: [{ date: '2026-07-28', requests: 42, prompt_tokens: 100, completion_tokens: 50 }],
          })
        )
      );

      render(<AdminHealthPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Usage Analytics' })).toBeInTheDocument();
      });
      await waitFor(() => {
        expect(screen.getByText('150')).toBeInTheDocument(); // total_tokens stat tile
      });
      expect(screen.getByText('llama3.1:8b')).toBeInTheDocument();
    });

    it('renders the Resource Metrics section with data from the metrics endpoint', async () => {
      server.use(
        http.get('/api/metrics', () =>
          HttpResponse.json({
            memory: { rss_mb: 256, vms_mb: 512, percent: 12.5, available_mb: 4096 },
            cpu: { process_percent: 3.2, system_percent: 15.5 },
            latency: { avg_ms: 10, p50_ms: 9, p95_ms: 20, p99_ms: 30 },
            queue: { depth: 1, total_requests: 5 },
          })
        )
      );

      render(<AdminHealthPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Resource Metrics' })).toBeInTheDocument();
      });
      await waitFor(() => {
        expect(screen.getByText('256.0 MB')).toBeInTheDocument();
      });
    });

    it('the Resource Utilization card\'s "Full metrics" link jumps to the Resource Metrics section on the same page', async () => {
      server.use(
        http.get('/api/v1/admin/health', () =>
          HttpResponse.json({
            service: { status: 'ok', uptime_s: 60 },
            dependencies: [],
            resource_metrics: {
              memory: { rss_mb: 128, percent: 2.5 },
              cpu: { process_percent: 1.1 },
              disk: { percent: 10 },
              queue: { depth: 0 },
              errors: { rate_percent: 0 },
            },
            alerts_source: 'local',
            alerts: [],
          })
        )
      );

      render(<AdminHealthPage />);
      await waitFor(() => {
        expect(screen.getByText(/128 MB/)).toBeInTheDocument();
      });
      expect(screen.getByRole('link', { name: /full metrics/i })).toHaveAttribute('href', '#resource-metrics');
    });
  });
});

import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import AdminHealthPage from '../../../src/app/admin/health/page';

describe('AdminHealthPage', () => {
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
          alerts: [
            { severity: 'critical', message: "Dependency 'ollama' is unreachable: Connection refused" },
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
});

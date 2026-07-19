import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import AdminDashboardPage from '../../../src/app/admin/dashboard/page';

describe('AdminDashboardPage', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('renders the dashboard heading and back link', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Admin Dashboard' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to chat/i });
    expect(link).toHaveAttribute('href', '/');
  });

  it('renders system status overview after loading', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Deploy: blue/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    expect(screen.getByText(/Auth: disabled/)).toBeInTheDocument();
  });

  it('renders configuration summary with a link to the wizard', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText('llama3.1:8b').length).toBeGreaterThan(0);
    });
    const wizardLink = screen.getByRole('link', { name: /open configuration wizard/i });
    expect(wizardLink).toHaveAttribute('href', '/admin');
  });

  it('renders the activity log with recent events', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('config.updated')).toBeInTheDocument();
    });
    expect(screen.getByText('deploy.switch')).toBeInTheDocument();
  });

  it('renders access management with masked key state', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/API key authentication disabled/)).toBeInTheDocument();
    });
    expect(screen.getByText('not set')).toBeInTheDocument();
  });

  it('wraps a long masked key instead of overflowing its pane', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('not set')).toBeInTheDocument();
    });
    const maskedKey = screen.getByText('not set');
    expect(maskedKey.tagName).toBe('CODE');
    expect(maskedKey).toHaveStyle({ wordBreak: 'break-all', overflowWrap: 'anywhere' });
  });

  it('renders the quick-nav links with button-style affordance', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Deploy: blue/)).toBeInTheDocument();
    });

    const deploymentLink = screen.getByRole('link', { name: /Deployment →/ });
    expect(deploymentLink).toHaveStyle({
      border: '1px solid var(--border)',
      whiteSpace: 'nowrap',
    });
    expect(deploymentLink).toHaveAttribute('href', '/admin/deploy');
  });

  it('toggles auth enabled when the checkbox is clicked', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/API key authentication disabled/)).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(screen.getByText(/API key authentication enabled/)).toBeInTheDocument();
    });
  });

  it('reveals the new API key once after rotation', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rotate api key/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /rotate api key/i }));

    await waitFor(() => {
      expect(screen.getByText('newly-generated-key-value')).toBeInTheDocument();
    });
    expect(global.confirm).toHaveBeenCalled();
  });

  it('does not rotate the key when the confirmation is declined', async () => {
    global.confirm = vi.fn().mockReturnValue(false);
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rotate api key/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /rotate api key/i }));

    expect(screen.queryByText('newly-generated-key-value')).not.toBeInTheDocument();
  });

  it('shows an error message when loading system status fails', async () => {
    server.use(
      http.get('/api/v1/admin/overview', () => new HttpResponse(null, { status: 500 }))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/Failed to load system status/).length).toBeGreaterThan(0);
    });
  });

  it('shows an error message when loading the activity log fails', async () => {
    server.use(
      http.get('/api/v1/admin/activity', () => new HttpResponse(null, { status: 500 }))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/Failed to load activity log/).length).toBeGreaterThan(0);
    });
  });

  it('shows an error message when loading access settings fails', async () => {
    server.use(
      http.get('/api/v1/admin/access', () => new HttpResponse(null, { status: 500 }))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/Failed to load access settings/).length).toBeGreaterThan(0);
    });
  });

  it('shows an empty state when there is no recorded activity', async () => {
    server.use(
      http.get('/api/v1/admin/activity', () => HttpResponse.json({ events: [] }))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('No admin activity recorded yet.')).toBeInTheDocument();
    });
  });

  it('defaults to an empty activity list when the events field is missing', async () => {
    server.use(
      http.get('/api/v1/admin/activity', () => HttpResponse.json({}))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('No admin activity recorded yet.')).toBeInTheDocument();
    });
  });

  it('falls back to the default header name when the loaded access header is empty', async () => {
    server.use(
      http.get('/api/v1/admin/access', () =>
        HttpResponse.json({ enabled: false, header: '', api_key_set: false, api_key_masked: null })
      )
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect((screen.getByLabelText('Header:') as HTMLInputElement).value).toBe('X-API-Key');
    });
  });

  it('shows an error message when saving access settings fails', async () => {
    server.use(
      http.post('/api/v1/admin/access', () =>
        HttpResponse.json({ detail: 'Cannot rotate key right now' }, { status: 400 })
      )
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('checkbox')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('checkbox'));

    await waitFor(() => {
      expect(screen.getByText(/Cannot rotate key right now/)).toBeInTheDocument();
    });
  });

  it('falls back to the default header name after a save response with an empty header', async () => {
    server.use(
      http.post('/api/v1/admin/access', () =>
        HttpResponse.json({ enabled: true, header: '', api_key_set: false, api_key_masked: null })
      )
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('checkbox')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('checkbox'));

    await waitFor(() => {
      expect((screen.getByLabelText('Header:') as HTMLInputElement).value).toBe('X-API-Key');
    });
  });

  it('saves a new access header when Save is clicked', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText('Header:')).toBeInTheDocument();
    });

    const input = screen.getByLabelText('Header:') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'X-Custom-Header' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect((screen.getByLabelText('Header:') as HTMLInputElement).value).toBe('X-Custom-Header');
    });
  });

  it('does not save the header when it is only whitespace', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText('Header:')).toBeInTheDocument();
    });

    const seenPosts: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (request.url.includes('/api/v1/admin/access') && request.method === 'POST') {
        seenPosts.push(request.url);
      }
    };
    server.events.on('request:start', listener);

    const input = screen.getByLabelText('Header:') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(seenPosts).toHaveLength(0);
    server.events.removeListener('request:start', listener);
  });

  it('renders active status badges and defaults when overview features are enabled', async () => {
    server.use(
      http.get('/api/v1/admin/overview', () =>
        HttpResponse.json({
          info: { ollama_base_url: 'http://127.0.0.1:11434', default_model: '', rag_enabled: true },
          resource_metrics: null,
          deploy: { namespace: 'nyxgpt' },
          canary: { active: true },
          self_heal: { enabled: true, unhealthy_count: 2 },
          observability: { monitoring: true, tracing: true, error_tracking: true, log_aggregation: true },
          auth_enabled: true,
        })
      )
    );

    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Deploy: unknown/)).toBeInTheDocument();
    });
    expect(screen.getByText('Canary: active')).toBeInTheDocument();
    expect(screen.getByText('Self-heal: on (2 unhealthy)')).toBeInTheDocument();
    expect(screen.getByText('Auth: enabled')).toBeInTheDocument();
    expect(screen.getAllByText('Not set').length).toBe(2);
    expect(screen.getAllByText('enabled').length).toBeGreaterThan(0);
  });

  it('shows a string error message when a non-Error value is thrown while loading', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch').mockRejectedValue('boom');
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText(/boom/).length).toBeGreaterThan(0);
    });
    fetchSpy.mockRestore();
  });

  it('shows a string error message when a non-Error value is thrown while saving access settings', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('checkbox')).toBeInTheDocument();
    });

    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/admin/access') && init?.method === 'POST') {
        return Promise.reject('nope');
      }
      return realFetch(input, init);
    });

    fireEvent.click(screen.getByRole('checkbox'));

    await waitFor(() => {
      expect(screen.getByText(/nope/)).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('falls back to an HTTP status message when the access save error has no detail', async () => {
    server.use(
      http.post('/api/v1/admin/access', () => HttpResponse.json({}, { status: 500 }))
    );
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('checkbox')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('checkbox'));

    await waitFor(() => {
      expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
    });
  });

  it('renders self-heal as on without an unhealthy count when there are no unhealthy components', async () => {
    server.use(
      http.get('/api/v1/admin/overview', () =>
        HttpResponse.json({
          info: { ollama_base_url: 'http://127.0.0.1:11434', default_model: 'llama3.1:8b', rag_enabled: false },
          resource_metrics: null,
          deploy: { active: 'blue', inactive: 'green' },
          canary: { active: false },
          self_heal: { enabled: true, unhealthy_count: 0 },
          observability: { monitoring: false, tracing: false, error_tracking: false, log_aggregation: false },
          auth_enabled: false,
        })
      )
    );

    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('Self-heal: on')).toBeInTheDocument();
    });
  });
});

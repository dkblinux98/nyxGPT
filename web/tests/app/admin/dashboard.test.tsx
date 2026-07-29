import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, screen, within, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import AdminDashboardPage from '../../../src/app/admin/dashboard/page';
import { ADMIN_NAV } from '../../../src/app/admin/dashboard/nav';

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
      expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Auth: disabled/)).toBeInTheDocument();
  });

  it('renders configuration summary with a link to the wizard', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText('llama3.1:8b').length).toBeGreaterThan(0);
    });
    const wizardLink = screen.getByRole('link', { name: /Configuration Wizard/ });
    expect(wizardLink).toHaveAttribute('href', '/admin');
  });

  it('renders the activity log with recent events', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('config.updated')).toBeInTheDocument();
    });
    expect(screen.getByText('canary.deploy')).toBeInTheDocument();
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

  it('renders the quick-nav tiles with descriptions, tooltips, and same-tab links', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    });

    for (const dest of ADMIN_NAV) {
      const tile = screen.getByRole('link', { name: new RegExp(dest.label) });
      expect(tile).toHaveAttribute('href', dest.href);
      // Hover tooltip explains what the screen is for; the same text is
      // visible in the tile so the grid is self-explanatory without hovering.
      expect(tile).toHaveAttribute('title', dest.description);
      expect(screen.getByText(dest.description)).toBeInTheDocument();
      // Same tab: no new-window target, no arrow decoration.
      expect(tile).not.toHaveAttribute('target');
      expect(tile.textContent).not.toContain('→');
    }
  });

  it('groups observation tiles under System Status and operation tiles under Configuration', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    });

    const systemStatus = screen.getByRole('region', { name: 'System status overview' });
    const configuration = screen.getByRole('region', { name: 'Configuration management' });

    const observationLabels = [
      'System Health',
      'Infrastructure Status',
      'SRE Overview',
      'Usage Analytics',
      'Full Metrics',
    ];
    const operationLabels = ['Canary Operations', 'Self-heal Operations'];

    for (const label of observationLabels) {
      expect(within(systemStatus).getByRole('link', { name: new RegExp(label) })).toBeInTheDocument();
      expect(within(configuration).queryByRole('link', { name: new RegExp(label) })).not.toBeInTheDocument();
    }

    for (const label of operationLabels) {
      expect(within(configuration).getByRole('link', { name: new RegExp(label) })).toBeInTheDocument();
      expect(within(systemStatus).queryByRole('link', { name: new RegExp(label) })).not.toBeInTheDocument();
    }

    // The Configuration Wizard renders in the same tile grid as the moved
    // operation tiles, not as a separately styled button.
    expect(within(configuration).getByRole('link', { name: /Configuration Wizard/ })).toBeInTheDocument();

    const observationCount = ADMIN_NAV.filter((dest) => dest.group === 'observation').length;
    const operationCount = ADMIN_NAV.filter((dest) => dest.group === 'operation').length;
    expect(within(systemStatus).getAllByRole('link')).toHaveLength(observationCount);
    // +1 for the Configuration Wizard tile alongside the moved operation tiles.
    expect(within(configuration).getAllByRole('link')).toHaveLength(operationCount + 1);
  });

  it('highlights a quick-nav tile on hover and restores it on leave', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    });

    const tile = screen.getByRole('link', { name: /System Health/ });
    fireEvent.mouseEnter(tile);
    expect(tile.style.borderColor).toBe('var(--link)');
    expect(tile.style.background).toBe('var(--muted)');
    fireEvent.mouseLeave(tile);
    expect(tile.style.borderColor).toBe('var(--border)');
    expect(tile.style.background).toBe('var(--background)');
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
          canary: { active: true },
          self_heal: { enabled: true, unhealthy_count: 2 },
          observability: { monitoring: true, tracing: true, error_tracking: true, log_aggregation: true },
          auth_enabled: true,
        })
      )
    );

    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('Canary: active')).toBeInTheDocument();
    });
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

  describe('restart-required banner (#3407)', () => {
    it('does not render when nothing is pending', async () => {
      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Admin Dashboard' })).toBeInTheDocument();
      });
      expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
    });

    it('renders the components and keys a wizard save flagged as pending', async () => {
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } })
        )
      );

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      });
      expect(screen.getByText(/api \(api\.port\)/)).toBeInTheDocument();
    });

    it('clicking Restart now triggers the mode-aware restart and clears the banner once it succeeds', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        let getCount = 0;
        server.use(
          http.get('/api/v1/infra/restart-status', () => {
            getCount += 1;
            // First load: pending. After the restart is triggered, the next
            // poll finds it cleared (the mode-aware restart succeeded).
            return HttpResponse.json({
              pending: getCount === 1 ? { api: { keys: ['api.port'], since: 1 } } : {},
            });
          }),
          http.post('/api/v1/infra/restart-required', () =>
            HttpResponse.json({ targets: ['api'], status: 'running' })
          )
        );

        render(<AdminDashboardPage />);
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
        });

        fireEvent.click(screen.getByRole('button', { name: /restart now/i }));
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restarting/i })).toBeInTheDocument();
        });

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000);
        });

        await waitFor(() => {
          expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it('reports failure if the restart has not cleared after the poll budget', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        server.use(
          http.get('/api/v1/infra/restart-status', () =>
            HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } })
          ),
          http.post('/api/v1/infra/restart-required', () =>
            HttpResponse.json({ targets: ['api'], status: 'running' })
          )
        );

        render(<AdminDashboardPage />);
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
        });

        fireEvent.click(screen.getByRole('button', { name: /restart now/i }));
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restarting/i })).toBeInTheDocument();
        });

        // 10 poll attempts at 2s each -- still pending every time.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(20000);
        });

        await waitFor(() => {
          expect(screen.getByText(/did not complete in time/i)).toBeInTheDocument();
        });
        // Still shown -- a restart that never clears must not silently disappear.
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
    });

    it('shows the error and re-enables Restart now when POST /infra/restart-required itself fails', async () => {
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } })
        ),
        http.post('/api/v1/infra/restart-required', () =>
          HttpResponse.json({ detail: 'no restart is currently pending' }, { status: 400 })
        )
      );

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
      });

      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await waitFor(() => {
        expect(screen.getByText('no restart is currently pending')).toBeInTheDocument();
      });
      // The failed POST never entered the poll loop -- the button reverts to
      // its idle label/state instead of getting stuck on "Restarting...".
      const button = screen.getByRole('button', { name: /restart now/i });
      expect(button).not.toBeDisabled();
      // A restart request that never started still leaves the underlying
      // condition pending, so the banner stays up.
      expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
    });

    it('falls back to an HTTP status message when the restart-required error body has no detail field', async () => {
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } })
        ),
        // A non-JSON body forces `res.json().catch(() => ({}))` to fall
        // back to `{}`, so `data?.detail` is undefined and the handler
        // must fall back to the raw HTTP status instead.
        http.post('/api/v1/infra/restart-required', () => new HttpResponse('not json', { status: 500 }))
      );

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
      });

      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await waitFor(() => {
        expect(screen.getByText('HTTP 500')).toBeInTheDocument();
      });
    });

    it('shows a plain-text error when the restart-required request itself rejects with a non-Error value', async () => {
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } })
        )
      );

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
      });

      const realFetch = global.fetch;
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
        const url = typeof input === 'string' ? input : (input as Request).url;
        if (url.includes('/api/v1/infra/restart-required') && init?.method === 'POST') {
          return Promise.reject('restart-boom');
        }
        return realFetch(input, init);
      });

      fireEvent.click(screen.getByRole('button', { name: /restart now/i }));

      await waitFor(() => {
        expect(screen.getByText('restart-boom')).toBeInTheDocument();
      });
      fetchSpy.mockRestore();
    });

    it('does not crash and leaves the banner state unchanged when the initial restart-status GET fails', async () => {
      server.use(http.get('/api/v1/infra/restart-status', () => new HttpResponse(null, { status: 500 })));

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Admin Dashboard' })).toBeInTheDocument();
      });
      // The failed GET is best-effort -- pending state stays at its
      // (empty) default rather than throwing, so no banner appears.
      expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
    });

    it('treats a restart-status response without a pending field as nothing pending', async () => {
      server.use(http.get('/api/v1/infra/restart-status', () => HttpResponse.json({})));

      render(<AdminDashboardPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Admin Dashboard' })).toBeInTheDocument();
      });
      expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
    });

    it('keeps polling through a mid-poll GET failure and clears the banner once a later poll omits pending', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        let getCount = 0;
        server.use(
          http.get('/api/v1/infra/restart-status', () => {
            getCount += 1;
            if (getCount === 1) {
              return HttpResponse.json({ pending: { api: { keys: ['api.port'], since: 1 } } });
            }
            if (getCount === 2) {
              // A transient failure mid-poll (e.g. the api component
              // itself restarting) -- polling must continue past it.
              return new HttpResponse(null, { status: 500 });
            }
            // Recovered, and the response omits `pending` entirely --
            // exercises the `data.pending || {}` fallback, which clears
            // the banner just like an explicit empty object would.
            return HttpResponse.json({});
          }),
          http.post('/api/v1/infra/restart-required', () =>
            HttpResponse.json({ targets: ['api'], status: 'running' })
          )
        );

        render(<AdminDashboardPage />);
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restart now/i })).toBeInTheDocument();
        });

        fireEvent.click(screen.getByRole('button', { name: /restart now/i }));
        await waitFor(() => {
          expect(screen.getByRole('button', { name: /restarting/i })).toBeInTheDocument();
        });

        // First poll attempt (2s) hits the 500; second poll attempt (2s)
        // succeeds via the missing-pending fallback.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(4000);
        });

        await waitFor(() => {
          expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
        });
      } finally {
        vi.useRealTimers();
      }
    });
  });
});

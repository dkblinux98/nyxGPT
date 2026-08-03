import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SecretsSetupPage from '../../../src/app/admin/secrets/page';

const mockSecrets = [
  {
    section: 'auth',
    key: 'api_key',
    full_key: 'auth.api_key',
    label: 'API authentication key',
    description: 'Shared secret clients must send to call the nyxGPT API.',
    obtain: 'nyxGPT can generate a strong one for you -- no external service involved.',
    can_generate: true,
    set: false,
    masked: null,
  },
  {
    section: 'openai',
    key: 'api_key',
    full_key: 'openai.api_key',
    label: 'OpenAI API key',
    description: 'Lets nyxGPT call OpenAI models.',
    obtain: 'https://platform.openai.com/api-keys -- create a new secret key.',
    can_generate: false,
    set: true,
    masked: 'sk-1********cdef',
  },
  {
    section: 'github',
    key: 'pat',
    full_key: 'github.pat',
    label: 'GitHub Personal Access Token',
    description: 'Authenticates GitHub agent automation.',
    obtain: 'https://github.com/settings/tokens -- generate a token with `repo` scope.',
    can_generate: false,
    set: false,
    masked: null,
  },
];

describe('SecretsSetupPage', () => {
  it('renders every guided secret with its label, description, and where-to-obtain link', async () => {
    server.use(http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })));
    render(<SecretsSetupPage />);

    expect(await screen.findByText('API authentication key')).toBeInTheDocument();
    expect(screen.getByText('OpenAI API key')).toBeInTheDocument();
    expect(screen.getByText('GitHub Personal Access Token')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'https://platform.openai.com/api-keys -- create a new secret key.' })).toHaveAttribute(
      'href',
      'https://platform.openai.com/api-keys -- create a new secret key.'
    );
  });

  it('shows Set (masked) for a configured secret and Not set for an unconfigured one', async () => {
    server.use(http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })));
    render(<SecretsSetupPage />);

    await screen.findByText('API authentication key');
    expect(screen.getByText('Set (sk-1********cdef)')).toBeInTheDocument();
    expect(screen.getAllByText('Not set')).toHaveLength(2);
  });

  it('only shows a Generate button for the secret with a generator', async () => {
    server.use(http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })));
    render(<SecretsSetupPage />);

    await screen.findByText('API authentication key');
    expect(screen.getAllByRole('button', { name: 'Generate for me' })).toHaveLength(1);
  });

  it('saves a typed value and refreshes the list from the response', async () => {
    server.use(
      http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })),
      http.post('/api/v1/config/secrets', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({ section: 'github', key: 'pat', value: 'ghp_abcdefghijklmnopqrst' });
        return HttpResponse.json({
          set: 'github.pat',
          masked: 'ghp_****...rst',
          secrets: mockSecrets.map((s) =>
            s.full_key === 'github.pat' ? { ...s, set: true, masked: 'ghp_****...rst' } : s
          ),
        });
      })
    );
    render(<SecretsSetupPage />);

    await screen.findByText('GitHub Personal Access Token');
    const inputs = screen.getAllByPlaceholderText('Paste value here');
    const githubInput = inputs[inputs.length - 1];
    await userEvent.type(githubInput, 'ghp_abcdefghijklmnopqrst');
    const saveButtons = screen.getAllByRole('button', { name: 'Save' });
    await userEvent.click(saveButtons[saveButtons.length - 1]);

    await waitFor(() => expect(screen.getByText('Saved (ghp_****...rst).')).toBeInTheDocument());
  });

  it('shows a validation error inline without clearing the rest of the page', async () => {
    server.use(
      http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })),
      http.post('/api/v1/config/secrets', () =>
        HttpResponse.json({ detail: 'github.pat: must be at least 20 characters' }, { status: 422 })
      )
    );
    render(<SecretsSetupPage />);

    await screen.findByText('GitHub Personal Access Token');
    const inputs = screen.getAllByPlaceholderText('Paste value here');
    await userEvent.type(inputs[inputs.length - 1], 'short');
    const saveButtons = screen.getAllByRole('button', { name: 'Save' });
    await userEvent.click(saveButtons[saveButtons.length - 1]);

    expect(await screen.findByText('github.pat: must be at least 20 characters')).toBeInTheDocument();
  });

  it('generates a value for the secret with a generator without requiring typed input', async () => {
    server.use(
      http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })),
      http.post('/api/v1/config/secrets', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({ section: 'auth', key: 'api_key', generate: true });
        return HttpResponse.json({
          set: 'auth.api_key',
          masked: 'abcd****wxyz',
          secrets: mockSecrets.map((s) =>
            s.full_key === 'auth.api_key' ? { ...s, set: true, masked: 'abcd****wxyz' } : s
          ),
        });
      })
    );
    render(<SecretsSetupPage />);

    await screen.findByText('API authentication key');
    await userEvent.click(screen.getByRole('button', { name: 'Generate for me' }));

    await waitFor(() => expect(screen.getByText('Saved (abcd****wxyz).')).toBeInTheDocument());
  });

  it('runs a dry-run sync and renders the results without values', async () => {
    server.use(
      http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })),
      http.post('/api/v1/config/secrets/sync', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({ dry_run: true });
        return HttpResponse.json({
          ok: true,
          dry_run: true,
          results: [
            { ok: true, message: '[dry-run] would sync monitoring.slack_bot_token -> Actions secret SLACK_BOT_TOKEN', details: '' },
          ],
        });
      })
    );
    render(<SecretsSetupPage />);

    await screen.findByText('API authentication key');
    await userEvent.click(screen.getByRole('button', { name: 'Preview (dry run)' }));

    expect(
      await screen.findByText(/would sync monitoring.slack_bot_token -> Actions secret SLACK_BOT_TOKEN/)
    ).toBeInTheDocument();
    expect(screen.getByText('Dry run -- nothing was pushed:')).toBeInTheDocument();
  });

  it('links back to the admin dashboard like the other admin pages', async () => {
    server.use(http.get('/api/v1/config/secrets', () => HttpResponse.json({ secrets: mockSecrets })));
    render(<SecretsSetupPage />);

    await screen.findByText('API authentication key');
    expect(screen.getByRole('link', { name: /Back to Admin Dashboard/ })).toHaveAttribute(
      'href',
      '/admin/dashboard'
    );
  });
});

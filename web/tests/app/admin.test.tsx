import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import AdminPage from '../../src/app/admin/page';

/** Clicks Next `times` times to advance through the wizard from wherever it currently is. */
async function clickNext(times: number) {
  for (let i = 0; i < times; i++) {
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /next/i }));
    });
  }
}

/** Selects a model, then clicks Next `times` times to advance through the wizard. */
async function selectModelAndClickNext(times: number) {
  await waitFor(() => {
    const select = screen.getByLabelText('Default Model');
    fireEvent.change(select, { target: { value: 'llama3.1:8b' } });
  });
  await clickNext(times);
}

/**
 * Configuration Wizard Tests (#3354, #3384)
 *
 * The wizard now covers every config.ini section the issue lists (core,
 * RAG, API & auth, observability), grouped into five steps: Core & Model,
 * RAG Configuration, API & Auth, Observability, Summary. The read-only
 * Resource Usage step was removed (#3384) -- live metrics live in
 * Settings -> Resource Usage and the admin dashboard instead.
 * Saving posts to /api/v1/config/sections and offers a restart/observability
 * reconciliation banner driven by the response.
 */

describe('AdminPage Component', () => {
  it('renders loading state initially', () => {
    render(<AdminPage />);
    expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
  });

  it('renders the configuration wizard after loading', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
    });
  });

  it('renders back to chat link', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const link = screen.getByRole('link', { name: /back to chat/i });
      expect(link).toBeInTheDocument();
      expect(link).toHaveAttribute('href', '/');
    });
  });

  it('starts at the Core & Model step with pre-loaded values', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
      expect(screen.getByLabelText('Ollama Base URL')).toHaveValue('http://127.0.0.1:11434');
      expect(screen.getByLabelText('Log Level')).toHaveValue('INFO');
    });
  });

  it('disables Next on the Core step until a model is selected', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
    });
  });

  it('enables Next once a model is selected', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      fireEvent.change(screen.getByLabelText('Default Model'), {
        target: { value: 'mistral:7b' },
      });
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /next/i })).not.toBeDisabled();
    });
  });

  it('edits core fields: timeout, sessions dir, vectorstore dir, log dir', async () => {
    render(<AdminPage />);
    await waitFor(() => screen.getByLabelText('Default Model'));

    fireEvent.change(screen.getByLabelText('Chat Timeout (seconds)'), {
      target: { value: '90' },
    });
    fireEvent.change(screen.getByLabelText('Sessions Directory'), {
      target: { value: '/tmp/sessions' },
    });
    fireEvent.change(screen.getByLabelText('Vectorstore Directory'), {
      target: { value: '/tmp/vectorstore' },
    });
    fireEvent.change(screen.getByLabelText('Log Directory'), {
      target: { value: '/tmp/logs' },
    });
    fireEvent.change(screen.getByLabelText('Log Level'), { target: { value: 'DEBUG' } });
    fireEvent.change(screen.getByLabelText('Ollama Base URL'), {
      target: { value: 'http://ollama.local:11434' },
    });

    expect(screen.getByLabelText('Chat Timeout (seconds)')).toHaveValue(90);
    expect(screen.getByLabelText('Sessions Directory')).toHaveValue('/tmp/sessions');
    expect(screen.getByLabelText('Vectorstore Directory')).toHaveValue('/tmp/vectorstore');
    expect(screen.getByLabelText('Log Directory')).toHaveValue('/tmp/logs');
    expect(screen.getByLabelText('Log Level')).toHaveValue('DEBUG');
    expect(screen.getByLabelText('Ollama Base URL')).toHaveValue('http://ollama.local:11434');
  });

  it('shows the models loading indicator, then the error banner if models fail', async () => {
    server.use(http.get('/api/models', () => new HttpResponse(null, { status: 500 })));
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load models: HTTP 500/)).toBeInTheDocument();
    });
    expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
  });

  it('navigates to the RAG Configuration step', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(1);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });
  });

  it('toggles RAG enabled and edits Cassandra fields', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(1);

    const checkbox = screen.getByRole('checkbox', { name: /enable rag/i });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox).toBeChecked());
    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox).not.toBeChecked());
    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox).toBeChecked());

    fireEvent.change(screen.getByLabelText('Cassandra Hosts'), {
      target: { value: '10.0.0.1, 10.0.0.2' },
    });
    fireEvent.change(screen.getByLabelText('Cassandra Port'), { target: { value: '9142' } });
    fireEvent.change(screen.getByLabelText('Cassandra Keyspace'), { target: { value: 'ks' } });
    fireEvent.change(screen.getByLabelText('Cassandra Table'), { target: { value: 'tbl' } });
    fireEvent.change(screen.getByLabelText('Embedding Model'), {
      target: { value: 'nomic-embed-text' },
    });

    expect(screen.getByLabelText('Cassandra Hosts')).toHaveValue('10.0.0.1, 10.0.0.2');
    expect(screen.getByLabelText('Cassandra Port')).toHaveValue(9142);
    expect(screen.getByLabelText('Cassandra Keyspace')).toHaveValue('ks');
    expect(screen.getByLabelText('Cassandra Table')).toHaveValue('tbl');
    expect(screen.getByLabelText('Embedding Model')).toHaveValue('nomic-embed-text');
  });

  it('navigates to the API & Auth step and edits fields', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(2);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'API & Auth' })).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('API Host'), { target: { value: '0.0.0.0' } });
    fireEvent.change(screen.getByLabelText('API Port'), { target: { value: '9000' } });

    const authCheckbox = screen.getByRole('checkbox', { name: /require api key/i });
    expect(authCheckbox).not.toBeChecked();
    fireEvent.click(authCheckbox);
    await waitFor(() => expect(authCheckbox).toBeChecked());
    fireEvent.click(authCheckbox);
    await waitFor(() => expect(authCheckbox).not.toBeChecked());
    fireEvent.click(authCheckbox);
    await waitFor(() => expect(authCheckbox).toBeChecked());

    fireEvent.change(screen.getByLabelText('Auth Header Name'), {
      target: { value: 'X-Custom-Key' },
    });

    const rateLimitCheckbox = screen.getByRole('checkbox', { name: /enable rate limiting/i });
    fireEvent.click(rateLimitCheckbox);
    await waitFor(() => expect(rateLimitCheckbox).toBeChecked());
    fireEvent.click(rateLimitCheckbox);
    await waitFor(() => expect(rateLimitCheckbox).not.toBeChecked());
    fireEvent.click(rateLimitCheckbox);
    await waitFor(() => expect(rateLimitCheckbox).toBeChecked());

    expect(screen.getByLabelText('API Host')).toHaveValue('0.0.0.0');
    expect(screen.getByLabelText('API Port')).toHaveValue(9000);
  });

  it('shows the API key as not set with an empty input by default', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(2);

    const apiKeyInput = screen.getByLabelText('API Key') as HTMLInputElement;
    expect(apiKeyInput.value).toBe('');
    expect(apiKeyInput).toHaveAttribute('placeholder', 'Not set');
  });

  it('shows a masked placeholder for an API key that is already set', async () => {
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: {
            nyxgpt: {
              default_model: 'llama3.1:8b',
              chat_timeout_seconds: '120',
              sessions_dir: '~/.nyxGPT/sessions',
              vectorstore_dir: '~/.nyxGPT/vectorstore',
            },
            logging: { level: 'INFO', dir: '~/.nyxGPT/logs' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: {
              enabled: 'true',
              header: 'X-API-Key',
              api_key: { set: true, masked: 'abcd****wxyz' },
            },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: 'nyxgpt-api', otlp_endpoint: 'http://localhost:4318/v1/traces' },
            error_tracking: {
              enabled: 'false',
              dsn: { set: true, masked: 'sent****ryio' },
              environment: 'development',
            },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          schema: [],
        })
      )
    );

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByLabelText('Default Model')).toHaveValue('llama3.1:8b');
    });
    await clickNext(2);
    expect(screen.getByLabelText('API Key')).toHaveAttribute(
      'placeholder',
      'Set (abcd****wxyz) -- leave blank to keep'
    );

    await clickNext(1);
    expect(screen.getByLabelText('Error Tracking DSN')).toHaveAttribute(
      'placeholder',
      'Set (sent****ryio) -- leave blank to keep'
    );
  });

  it('navigates to the Observability step and toggles every stack', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(3);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Observability' })).toBeInTheDocument();
    });

    for (const name of [
      /enable tracing/i,
      /enable error tracking/i,
      /enable monitoring/i,
      /enable log aggregation/i,
    ]) {
      const checkbox = screen.getByRole('checkbox', { name });
      expect(checkbox).not.toBeChecked();
      fireEvent.click(checkbox);
    }

    await waitFor(() => {
      for (const name of [
        /enable tracing/i,
        /enable error tracking/i,
        /enable monitoring/i,
        /enable log aggregation/i,
      ]) {
        expect(screen.getByRole('checkbox', { name })).toBeChecked();
      }
    });

    // Uncheck then re-check tracing/error-tracking/monitoring so both sides
    // of their `checked ? 'true' : 'false'` handlers are exercised.
    for (const name of [/enable tracing/i, /enable error tracking/i, /enable monitoring/i]) {
      const checkbox = screen.getByRole('checkbox', { name });
      fireEvent.click(checkbox);
      await waitFor(() => expect(checkbox).not.toBeChecked());
      fireEvent.click(checkbox);
      await waitFor(() => expect(checkbox).toBeChecked());
    }

    fireEvent.change(screen.getByLabelText('Tracing Service Name'), {
      target: { value: 'my-service' },
    });
    fireEvent.change(screen.getByLabelText('OTLP Endpoint'), {
      target: { value: 'http://otel:4318/v1/traces' },
    });
    fireEvent.change(screen.getByLabelText('Error Tracking DSN'), {
      target: { value: 'http://key@glitchtip/1' },
    });
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'staging' } });

    expect(screen.getByLabelText('Tracing Service Name')).toHaveValue('my-service');
    expect(screen.getByLabelText('Environment')).toHaveValue('staging');
  });

  it('navigates to the Summary step and reviews every section', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(4);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    expect(screen.getByText('Core & Model')).toBeInTheDocument();
    expect(screen.getByText('RAG Configuration')).toBeInTheDocument();
    expect(screen.getByText('API & Auth')).toBeInTheDocument();
    expect(screen.getByText('Observability')).toBeInTheDocument();
  });

  it('shows Enabled/Required/On for every toggle on the summary step once switched on', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(1);
    fireEvent.click(screen.getByRole('checkbox', { name: /enable rag/i }));

    await clickNext(1);
    fireEvent.click(screen.getByRole('checkbox', { name: /require api key/i }));

    await clickNext(1);
    fireEvent.click(screen.getByRole('checkbox', { name: /enable tracing/i }));
    fireEvent.click(screen.getByRole('checkbox', { name: /enable error tracking/i }));
    fireEvent.click(screen.getByRole('checkbox', { name: /enable monitoring/i }));
    fireEvent.click(screen.getByRole('checkbox', { name: /enable log aggregation/i }));

    await clickNext(1);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    expect(screen.getByText('Enabled')).toBeInTheDocument();
    expect(screen.getByText('Required')).toBeInTheDocument();
    expect(screen.getAllByText('On')).toHaveLength(4);
  });

  it('renders save configuration button on summary step', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      expect(saveButton).toBeInTheDocument();
      expect(saveButton).not.toBeDisabled();
    });
  });

  it('shows success message and posts every section after saving', async () => {
    let capturedBody: Record<string, Record<string, unknown>> | undefined;
    server.use(
      http.post('/api/v1/config/sections', async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({
          applied: capturedBody,
          sections: {
            nyxgpt: {
              default_model: 'llama3.1:8b',
              chat_timeout_seconds: '120',
              sessions_dir: '~/.nyxGPT/sessions',
              vectorstore_dir: '~/.nyxGPT/vectorstore',
            },
            logging: { level: 'INFO', dir: '~/.nyxGPT/logs' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: 'nyxgpt-api', otlp_endpoint: 'http://localhost:4318/v1/traces' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: 'development' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: [],
          observability_reconciled: false,
          observability_result: null,
        });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/configuration saved and applied/i)).toBeInTheDocument();
    });

    expect(capturedBody?.nyxgpt.default_model).toBe('llama3.1:8b');
    expect(capturedBody?.api.port).toBe(8000);
    expect(capturedBody?.auth).not.toHaveProperty('api_key');
    expect(capturedBody?.error_tracking).not.toHaveProperty('dsn');
    expect(screen.getByText('Return to chat')).toHaveAttribute('href', '/');
  });

  it('includes the api_key and dsn in the payload when the user types new values', async () => {
    let capturedBody: Record<string, Record<string, unknown>> | undefined;
    server.use(
      http.post('/api/v1/config/sections', async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({
          applied: capturedBody,
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: true, masked: 'new-****-key' } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: true, masked: 'newd****snxx' }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: [],
          observability_reconciled: false,
          observability_result: null,
        });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(2);
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'brand-new-key' } });
    await clickNext(1);
    fireEvent.change(screen.getByLabelText('Error Tracking DSN'), {
      target: { value: 'http://new@dsn/1' },
    });
    await clickNext(1);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(capturedBody?.auth.api_key).toBe('brand-new-key');
    });
    expect(capturedBody?.error_tracking.dsn).toBe('http://new@dsn/1');
  });

  it('shows a restart banner and schedules a restart when required', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json({
          applied: {},
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '9000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: ['api'],
          observability_reconciled: true,
          observability_result: { ok: true, messages: ['Observability stack up'] },
        })
      )
    );
    let restartRequestBody: unknown;
    server.use(
      http.post('/api/v1/config/restart', async ({ request }) => {
        restartRequestBody = await request.json();
        return HttpResponse.json({ target: 'api', status: 'scheduled' });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/observability stack reconciled/i)).toBeInTheDocument();
      expect(screen.getByText(/observability stack up/i)).toBeInTheDocument();
      expect(screen.getByText(/need a restart to fully apply/i)).toBeInTheDocument();
    });

    const restartButton = screen.getByRole('button', { name: 'Restart api' });
    fireEvent.click(restartButton);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /restart scheduled/i })).toBeInTheDocument();
    });
    expect(restartRequestBody).toEqual({ target: 'api' });
  });

  it('shows an observability reconciled message with no detail messages', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json({
          applied: {},
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: [],
          observability_reconciled: true,
          observability_result: null,
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText('Observability stack reconciled.')).toBeInTheDocument();
    });
  });

  it('shows an error message when scheduling a restart fails', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json({
          applied: {},
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '9000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: ['api'],
          observability_reconciled: false,
          observability_result: null,
        })
      )
    );
    server.use(
      http.post('/api/v1/config/restart', () => new HttpResponse(null, { status: 500 }))
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Restart api' }));
    });

    await waitFor(() => {
      expect(screen.getByText(/Failed to schedule restart: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('shows a string error message when scheduling a restart throws a non-Error value', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json({
          applied: {},
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '9000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: ['api'],
          observability_reconciled: false,
          observability_result: null,
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });
    await waitFor(() => screen.getByRole('button', { name: 'Restart api' }));

    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/config/restart')) {
        return Promise.reject('restart-boom');
      }
      return realFetch(input, init);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Restart api' }));

    await waitFor(() => {
      expect(screen.getByText(/Failed to schedule restart: restart-boom/)).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('shows a validation error message from the 422 error envelope', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json(
          {
            error: {
              code: 'http_error',
              message: 'Request failed',
              details: { errors: ['api.port: must be an integer'] },
              request_id: null,
            },
          },
          { status: 422 }
        )
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/api\.port: must be an integer/)).toBeInTheDocument();
    });
  });

  it('shows a plain message from the error envelope when there are no field errors', async () => {
    server.use(
      http.post('/api/v1/config/sections', () =>
        HttpResponse.json(
          {
            error: {
              code: 'http_error',
              message: 'No valid fields to update',
              details: null,
              request_id: null,
            },
          },
          { status: 400 }
        )
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText('No valid fields to update')).toBeInTheDocument();
    });
  });

  it('falls back to an HTTP status message when the error body has no error envelope', async () => {
    server.use(http.post('/api/v1/config/sections', () => HttpResponse.json({}, { status: 503 })));

    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    });
  });

  it('shows a string error message when saving throws a non-Error value', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(4);

    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/config/sections') && init?.method === 'POST') {
        return Promise.reject('save-boom');
      }
      return realFetch(input, init);
    });

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText('save-boom')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('allows navigation back to previous steps', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(1);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /previous/i }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
    });
  });

  it('disables Previous button on first step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled();
    });
  });

  it('disables Next button on the last step', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
    });
  });

  it('re-fetches models when the window regains focus', async () => {
    let modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b'];
    server.use(
      http.get('/api/models', () => HttpResponse.json({ models: modelsResponse }))
    );

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'llama3.1:8b' })).toBeInTheDocument();
    });

    modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b', 'phi3:mini'];
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'phi3:mini' })).toBeInTheDocument();
    });
  });

  it('re-fetches models when the tab becomes visible again', async () => {
    let modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b'];
    server.use(
      http.get('/api/models', () => HttpResponse.json({ models: modelsResponse }))
    );

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'llama3.1:8b' })).toBeInTheDocument();
    });

    modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b', 'phi3:mini'];
    const visibilityStateSpy = vi
      .spyOn(document, 'visibilityState', 'get')
      .mockReturnValue('visible');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'phi3:mini' })).toBeInTheDocument();
    });
    visibilityStateSpy.mockRestore();
  });

  it('does not re-fetch models when tab visibility changes to something other than visible', async () => {
    let modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b'];
    server.use(
      http.get('/api/models', () => HttpResponse.json({ models: modelsResponse }))
    );

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'llama3.1:8b' })).toBeInTheDocument();
    });

    modelsResponse = ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b', 'phi3:mini'];
    const visibilityStateSpy = vi
      .spyOn(document, 'visibilityState', 'get')
      .mockReturnValue('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('option', { name: 'phi3:mini' })).not.toBeInTheDocument();
    visibilityStateSpy.mockRestore();
  });

  it('defaults to an empty models list when the models response has no models field', async () => {
    server.use(http.get('/api/models', () => HttpResponse.json({})));

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
    });
    expect(screen.queryByRole('option', { name: 'llama3.1:8b' })).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Select a model...' })).toBeInTheDocument();
  });

  it('renders error state when config loading fails', async () => {
    server.use(http.get('/api/v1/config/sections', () => new HttpResponse(null, { status: 500 })));

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
    });
  });

  it('shows a string error message when config loading throws a non-Error value', async () => {
    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/config/sections')) {
        return Promise.reject('config-boom');
      }
      return realFetch(input, init);
    });

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('config-boom')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('shows a string error message when models loading throws a non-Error value', async () => {
    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/models')) {
        return Promise.reject('models-boom');
      }
      return realFetch(input, init);
    });

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText(/models-boom/)).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('tests connection successfully from the Core step', async () => {
    render(<AdminPage />);
    await waitFor(() => screen.getByLabelText('Default Model'));
    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));
    await waitFor(() => {
      expect(screen.getByText(/connection successful/i)).toBeInTheDocument();
    });
  });

  it('shows a failure message when the connection test fails', async () => {
    server.use(http.get('/api/info', () => new HttpResponse(null, { status: 500 })));
    render(<AdminPage />);
    await waitFor(() => screen.getByLabelText('Default Model'));
    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Connection failed: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('shows a string connection failure message when a non-Error value is thrown', async () => {
    render(<AdminPage />);
    await waitFor(() => screen.getByLabelText('Default Model'));

    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/info')) {
        return Promise.reject('nope');
      }
      return realFetch(input, init);
    });

    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));

    await waitFor(() => {
      expect(screen.getByText(/Connection failed: nope/)).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('advances to the next step on a real ArrowRight keydown', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(0);
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });
  });

  it('does not advance on a real ArrowRight keydown when no model is selected', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
  });

  it('goes back a step on a real ArrowLeft keydown', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(0);
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
    });
  });

  it('does not go before the first step on a real ArrowLeft keydown', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
  });

  it('advances to the next step on a real Enter keydown', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(0);
    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });
  });

  it('triggers save on a real Enter keydown while on the summary step', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(4);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => {
      expect(screen.getByText(/configuration saved and applied/i)).toBeInTheDocument();
    });
  });

  it('ignores a real keydown event when the target is a select element', async () => {
    render(<AdminPage />);
    await waitFor(() => screen.getByLabelText('Default Model'));
    fireEvent.change(screen.getByLabelText('Default Model'), { target: { value: 'llama3.1:8b' } });

    fireEvent.keyDown(screen.getByLabelText('Default Model'), { key: 'ArrowRight' });

    expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
  });

  /**
   * Default-value labelling and safe-save tests (#3385).
   *
   * Backed by `GET|POST /api/v1/config/sections` returning `field_defaults`
   * (`{section: {key: is_default}}`) alongside `sections`. A field flagged
   * `is_default: true` shows its effective value plus a "default" badge
   * instead of an ambiguous blank box, and saving without touching it must
   * not freeze that inherited default into config.ini as an explicit value.
   */
  const SECTIONS_WITH_DEFAULTS = {
    nyxgpt: {
      default_model: 'llama3.1:8b',
      chat_timeout_seconds: '180',
      sessions_dir: '~/.nyxGPT/sessions',
      vectorstore_dir: '~/.nyxGPT/vectorstore',
    },
    logging: { level: 'INFO', dir: '~/.nyxGPT/logs' },
    ollama: { base_url: 'http://127.0.0.1:11434' },
    api: { host: '127.0.0.1', port: '8000' },
    auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
    rate_limit: { enabled: 'false' },
    rag: {
      enable_chat_context: 'false',
      cassandra_hosts: '127.0.0.1',
      cassandra_port: '9042',
      cassandra_keyspace: 'nyxgpt',
      cassandra_table: 'rag_chunks',
      embedding_model: '',
    },
    tracing: {
      enabled: 'false',
      service_name: 'nyxgpt-api',
      otlp_endpoint: 'http://localhost:4318/v1/traces',
    },
    error_tracking: {
      enabled: 'false',
      dsn: { set: false, masked: null },
      environment: 'development',
    },
    monitoring: { enabled: 'false' },
    log_aggregation: { enabled: 'false' },
  };

  const FIELD_DEFAULTS_ALL_UNSET = {
    nyxgpt: {
      default_model: true,
      chat_timeout_seconds: true,
      sessions_dir: true,
      vectorstore_dir: true,
    },
    logging: { level: true, dir: true },
    ollama: { base_url: true },
    api: { host: true, port: true },
    auth: { enabled: true, header: true },
    rate_limit: { enabled: true },
    rag: {
      enable_chat_context: true,
      cassandra_hosts: true,
      cassandra_port: true,
      cassandra_keyspace: true,
      cassandra_table: true,
      embedding_model: true,
    },
    tracing: { enabled: true, service_name: true, otlp_endpoint: true },
    error_tracking: { enabled: true, environment: true },
    monitoring: { enabled: true },
    log_aggregation: { enabled: true },
  };

  it('labels an inherited default value instead of leaving it blank or unmarked', async () => {
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: SECTIONS_WITH_DEFAULTS,
          schema: [],
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(1);

    await waitFor(() => {
      // rag.embedding_model has no fixed default and is genuinely empty here;
      // an empty inherited-default field must not show the "default" badge.
      expect(screen.getByLabelText('Embedding Model')).toHaveValue('');
    });

    await clickNext(2);

    await waitFor(() => {
      expect(screen.getByLabelText('Tracing Service Name')).toHaveValue('nyxgpt-api');
      expect(screen.getByLabelText('OTLP Endpoint')).toHaveValue(
        'http://localhost:4318/v1/traces'
      );
    });

    // Every populated inherited-default field is labelled -- not just tracing.
    expect(screen.getAllByText('default').length).toBeGreaterThan(1);
  });

  it('does not label a field the user explicitly configured', async () => {
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: SECTIONS_WITH_DEFAULTS,
          schema: [],
          field_defaults: {
            ...FIELD_DEFAULTS_ALL_UNSET,
            tracing: { enabled: true, service_name: false, otlp_endpoint: true },
          },
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(3);

    await waitFor(() => {
      expect(screen.getByLabelText('Tracing Service Name')).toHaveValue('nyxgpt-api');
    });

    const serviceNameRow = screen.getByText('Tracing Service Name').parentElement;
    expect(serviceNameRow).not.toHaveTextContent('default');

    const otlpRow = screen.getByText('OTLP Endpoint').parentElement;
    expect(otlpRow).toHaveTextContent('default');
  });

  it('omits untouched inherited defaults from the save payload but includes edited fields', async () => {
    let capturedBody: Record<string, Record<string, unknown>> | undefined;
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: SECTIONS_WITH_DEFAULTS,
          schema: [],
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
        })
      ),
      http.post('/api/v1/config/sections', async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({
          applied: capturedBody,
          sections: SECTIONS_WITH_DEFAULTS,
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
          restart_required: [],
          observability_reconciled: false,
          observability_result: null,
        });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(3);

    fireEvent.change(screen.getByLabelText('Tracing Service Name'), {
      target: { value: 'my-custom-service' },
    });

    await clickNext(1);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/configuration saved and applied/i)).toBeInTheDocument();
    });

    // Edited away from its default -> included.
    expect(capturedBody?.tracing.service_name).toBe('my-custom-service');
    // Never touched, still showing its inherited default -> omitted entirely,
    // so saving can't freeze today's fallback into config.ini as explicit.
    expect(capturedBody?.tracing).not.toHaveProperty('otlp_endpoint');
    // Every field in these sections was an untouched default -> the whole
    // section is left out of the payload rather than re-writing today's
    // fallback values as explicit settings.
    expect(capturedBody).not.toHaveProperty('error_tracking');
    expect(capturedBody).not.toHaveProperty('logging');
  });

  it('saves without a network round trip when nothing was changed from its default', async () => {
    let postCalled = false;
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: SECTIONS_WITH_DEFAULTS,
          schema: [],
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
        })
      ),
      http.post('/api/v1/config/sections', () => {
        postCalled = true;
        return HttpResponse.json({ applied: {}, sections: SECTIONS_WITH_DEFAULTS });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/configuration saved and applied/i)).toBeInTheDocument();
    });
    expect(postCalled).toBe(false);
  });

  it('initializes the auth/error_tracking payload sections when only the secret field changes', async () => {
    // auth.enabled/header and error_tracking.enabled/environment are all
    // untouched inherited defaults here, so buildSavePayload's `include()`
    // skips them -- payload.auth/error_tracking must not exist yet by the
    // time api_key/dsn are handled, exercising their own initialization
    // (`if (!payload.auth) payload.auth = {}`) rather than relying on
    // another field in the section having already created it.
    let capturedBody: Record<string, Record<string, unknown>> | undefined;
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: SECTIONS_WITH_DEFAULTS,
          schema: [],
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
        })
      ),
      http.post('/api/v1/config/sections', async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({
          applied: capturedBody,
          sections: SECTIONS_WITH_DEFAULTS,
          field_defaults: FIELD_DEFAULTS_ALL_UNSET,
          restart_required: [],
          observability_reconciled: false,
          observability_result: null,
        });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(2);
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'brand-new-key' } });
    await clickNext(1);
    fireEvent.change(screen.getByLabelText('Error Tracking DSN'), {
      target: { value: 'http://new@dsn/1' },
    });
    await clickNext(1);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(capturedBody?.auth).toEqual({ api_key: 'brand-new-key' });
    });
    expect(capturedBody?.error_tracking).toEqual({ dsn: 'http://new@dsn/1' });
  });

  it('does not navigate or re-trigger save via keyboard shortcuts while a save is in progress', async () => {
    server.use(
      http.post('/api/v1/config/sections', async () => {
        await new Promise((resolve) => setTimeout(resolve, 200));
        return HttpResponse.json({
          applied: {},
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: false, masked: null } },
            rate_limit: { enabled: 'false' },
            rag: {
              enable_chat_context: 'false',
              cassandra_hosts: '127.0.0.1',
              cassandra_port: '9042',
              cassandra_keyspace: 'nyxgpt',
              cassandra_table: 'rag_chunks',
              embedding_model: 'nomic-embed-text',
            },
            tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
            error_tracking: { enabled: 'false', dsn: { set: false, masked: null }, environment: '' },
            monitoring: { enabled: 'false' },
            log_aggregation: { enabled: 'false' },
          },
          restart_required: [],
          observability_reconciled: false,
          observability_result: null,
        });
      })
    );

    render(<AdminPage />);
    await selectModelAndClickNext(4);

    fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /saving/i })).toBeInTheDocument();
    });

    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();

    await waitFor(
      () => {
        expect(screen.getByText(/configuration saved and applied/i)).toBeInTheDocument();
      },
      { timeout: 3000 }
    );
  });
});

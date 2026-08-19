import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, render, screen, within, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { FULL_WIZARD_SCHEMA } from '../mocks/handlers';
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
 * Stubs `window.location.href`'s setter so a test can assert the wizard's
 * final-page-Save redirect (#3407) without letting happy-dom attempt a real
 * navigation -- replacing `window.location` wholesale breaks relative
 * `fetch()` URL resolution for the rest of the suite, so this patches only
 * the `href` accessor and restores the original descriptor afterward.
 */
function stubLocationHref() {
  const setter = vi.fn();
  const original = Object.getOwnPropertyDescriptor(window.location, 'href');
  Object.defineProperty(window.location, 'href', {
    configurable: true,
    set: setter,
    get: () => 'http://localhost:3000/admin',
  });
  return {
    setter,
    restore: () => {
      if (original) Object.defineProperty(window.location, 'href', original);
    },
  };
}

/**
 * Configuration Wizard Tests (#3354, #3384)
 *
 * The wizard now covers every config.ini section the issue lists (core,
 * RAG, API & auth, observability), grouped into five steps: Core & Model,
 * RAG Configuration, API & Auth, Observability, Summary. The read-only
 * Resource Usage step was removed (#3384) -- live metrics live in the
 * System Health screen (#3413) and the admin dashboard instead.
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

  it('renders back to admin dashboard link (#3407)', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const link = screen.getByRole('link', { name: /back to admin dashboard/i });
      expect(link).toBeInTheDocument();
      expect(link).toHaveAttribute('href', '/admin/dashboard');
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

  it('navigates to the Summary step and reviews every schema section (#3407)', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(5);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    // Every section in the derived schema (FULL_WIZARD_SCHEMA) renders, not just
    // the four legacy hand-built groups -- the bug this issue exists to fix.
    for (const label of [
      'Core & model',
      'Logging',
      'Model backend',
      'API server',
      'Authentication',
      'Rate limiting',
      'RAG / retrieval',
      'Tracing',
      'Error tracking',
      'Monitoring',
      'Log aggregation',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('marks changed fields on the Summary step, distinct from unchanged defaults (#3407)', async () => {
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

    await clickNext(2);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    // Six values were switched on above, so at least six "changed" badges show.
    expect(screen.getAllByText('changed').length).toBeGreaterThanOrEqual(6);
    expect(screen.getAllByText('On').length).toBeGreaterThanOrEqual(4);
  });

  it('never renders a secret in cleartext on the Summary step, only its masked preview (#3407)', async () => {
    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: {
            nyxgpt: { default_model: 'llama3.1:8b', chat_timeout_seconds: '120', sessions_dir: '', vectorstore_dir: '' },
            logging: { level: 'INFO', dir: '' },
            ollama: { base_url: 'http://127.0.0.1:11434' },
            api: { host: '127.0.0.1', port: '8000' },
            auth: { enabled: 'false', header: 'X-API-Key', api_key: { set: true, masked: 'abcd****wxyz' } },
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
          schema: FULL_WIZARD_SCHEMA,
          field_defaults: {},
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(2);
    // Rotate the already-set API key, and set the never-before-set DSN, so
    // both the "already masked" and "newly typed" secret paths render.
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'brand-new-secret-key' } });
    await clickNext(1);
    fireEvent.change(screen.getByLabelText('Error Tracking DSN'), {
      target: { value: 'http://newly-typed-dsn-value@glitchtip/1' },
    });
    await clickNext(2);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });

    expect(screen.getByText('abcd****wxyz')).toBeInTheDocument();
    expect(screen.getByText('Not set')).toBeInTheDocument();
    expect(screen.queryByText(/brand-new-secret-key/)).not.toBeInTheDocument();
    expect(screen.queryByText(/newly-typed-dsn-value/)).not.toBeInTheDocument();
    expect(screen.getAllByText('will be updated')).toHaveLength(2);
  });

  it('renders save configuration button on summary step', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(5);
    await waitFor(() => {
      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      expect(saveButton).toBeInTheDocument();
      expect(saveButton).not.toBeDisabled();
    });
  });

  it('posts every section then redirects to the Admin Dashboard on the final Save (#3407)', async () => {
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

    const location = stubLocationHref();
    render(<AdminPage />);
    await selectModelAndClickNext(5);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(location.setter).toHaveBeenCalledWith('/admin/dashboard');
    });

    expect(capturedBody?.nyxgpt.default_model).toBe('llama3.1:8b');
    expect(capturedBody?.api.port).toBe(8000);
    expect(capturedBody?.auth).not.toHaveProperty('api_key');
    expect(capturedBody?.error_tracking).not.toHaveProperty('dsn');

    location.restore();
  });

  it('Save changes on a non-final step persists and advances to the next step, without redirecting (#3407)', async () => {
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

    const location = stubLocationHref();
    render(<AdminPage />);
    await waitFor(() => {
      fireEvent.change(screen.getByLabelText('Default Model'), { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });

    expect(capturedBody?.nyxgpt.default_model).toBe('llama3.1:8b');
    // A non-final Save advances in place -- it must not also fire the
    // last-page redirect to the Admin Dashboard.
    expect(location.setter).not.toHaveBeenCalled();

    location.restore();
  });

  it('Summary falls back gracefully when a field secret flag disagrees with the hand-built form state', async () => {
    // The Summary step trusts the schema's `secret` flag per field, not the
    // hand-built step components' own assumptions -- if the backend ever
    // marks a normally-secret field as non-secret (or vice versa) the
    // Summary must still render something sane instead of crashing.
    const weirdSchema = FULL_WIZARD_SCHEMA.map((spec) => {
      if (spec.section === 'auth') {
        return {
          ...spec,
          fields: spec.fields.map((f) => (f.key === 'api_key' ? { ...f, secret: false } : f)),
        };
      }
      if (spec.section === 'rate_limit') {
        return {
          ...spec,
          fields: spec.fields.map((f) => (f.key === 'enabled' ? { ...f, secret: true } : f)),
        };
      }
      return spec;
    });

    server.use(
      http.get('/api/v1/config/sections', () =>
        HttpResponse.json({
          sections: {
            nyxgpt: { default_model: '', chat_timeout_seconds: '120', sessions_dir: '~/.nyxGPT/sessions', vectorstore_dir: '~/.nyxGPT/vectorstore' },
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
          schema: weirdSchema,
        })
      )
    );

    render(<AdminPage />);
    await selectModelAndClickNext(5);

    // auth.api_key is schema-flagged non-secret here, but the hand-built
    // form state never tracks its value outside the dedicated secret field
    // -- the merged Summary value is missing, and formatSummaryValue must
    // show "(empty)" rather than "undefined".
    const authSection = screen.getByTestId('summary-section-auth');
    expect(within(authSection).getByText('Api Key:')).toBeInTheDocument();
    expect(within(authSection).getByText('(empty)')).toBeInTheDocument();

    // rate_limit.enabled is schema-flagged secret here even though it's a
    // hand-built (non-"Additional Settings") field with no entry in
    // extraValues -- pendingSecretValue must fall back to '' instead of
    // reading `undefined`.
    const rateLimitSection = screen.getByTestId('summary-section-rate_limit');
    expect(within(rateLimitSection).getByText('Not set')).toBeInTheDocument();
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
    await clickNext(2);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(capturedBody?.auth.api_key).toBe('brand-new-key');
    });
    expect(capturedBody?.error_tracking.dsn).toBe('http://new@dsn/1');
  });

  // The wizard no longer offers a per-target Restart button inline (#3407)
  // -- a save that needs a restart is tracked server-side and surfaced by
  // the Admin Dashboard's restart-required button instead (see
  // dashboard.test.tsx). This test only checks the wizard still redirects
  // cleanly on success when the response reports `restart_required`.
  it('redirects to the Admin Dashboard even when the save response reports restart_required', async () => {
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

    const location = stubLocationHref();
    render(<AdminPage />);
    await selectModelAndClickNext(5);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(location.setter).toHaveBeenCalledWith('/admin/dashboard');
    });
    expect(screen.queryByRole('button', { name: /restart api/i })).not.toBeInTheDocument();

    location.restore();
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
    await selectModelAndClickNext(5);
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
    await selectModelAndClickNext(5);
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
    await selectModelAndClickNext(5);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    });
  });

  it('shows a string error message when saving throws a non-Error value', async () => {
    render(<AdminPage />);
    await selectModelAndClickNext(5);

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
    await selectModelAndClickNext(5);
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
    await selectModelAndClickNext(5);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => {
      expect(capturedBody?.nyxgpt.default_model).toBe('llama3.1:8b');
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

    await clickNext(2);
    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(capturedBody).toBeDefined();
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

    const location = stubLocationHref();
    render(<AdminPage />);
    await selectModelAndClickNext(5);

    await waitFor(() => {
      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
    });

    await waitFor(() => {
      expect(location.setter).toHaveBeenCalledWith('/admin/dashboard');
    });
    expect(postCalled).toBe(false);

    location.restore();
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
    await clickNext(2);

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

    const location = stubLocationHref();
    render(<AdminPage />);
    await selectModelAndClickNext(5);

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
        expect(location.setter).toHaveBeenCalledWith('/admin/dashboard');
      },
      { timeout: 3000 }
    );

    location.restore();
  });

  describe('Cancel action (#3387)', () => {
    beforeEach(() => {
      global.confirm = vi.fn();
    });

    afterEach(() => {
      // Cancel is a real <a href> so an un-prevented click actually
      // navigates in happy-dom; restore the URL so it can't leak into
      // later tests in this file.
      window.history.replaceState(null, '', '/');
    });

    it('links to the Admin Dashboard and exits without prompting when there are no unsaved changes', async () => {
      render(<AdminPage />);
      await waitFor(() => screen.getByLabelText('Default Model'));

      const cancelLink = screen.getByRole('link', { name: /cancel/i });
      expect(cancelLink).toHaveAttribute('href', '/admin/dashboard');

      const notPrevented = fireEvent.click(cancelLink);

      expect(global.confirm).not.toHaveBeenCalled();
      expect(notPrevented).toBe(true);
    });

    it('is reachable from every step of the wizard', async () => {
      render(<AdminPage />);
      await waitFor(() => screen.getByLabelText('Default Model'));
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();

      await selectModelAndClickNext(1);
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();

      await clickNext(1);
      expect(screen.getByRole('heading', { name: 'API & Auth' })).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();

      await clickNext(1);
      expect(screen.getByRole('heading', { name: 'Observability' })).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();

      await clickNext(1);
      expect(screen.getByRole('heading', { name: 'Additional Settings' })).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();

      await clickNext(1);
      expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /cancel/i })).toBeInTheDocument();
    });

    it('prompts before discarding unsaved changes and writes nothing on confirm', async () => {
      let saveCalled = false;
      server.use(
        http.post('/api/v1/config/sections', async () => {
          saveCalled = true;
          return HttpResponse.json({});
        })
      );
      global.confirm = vi.fn().mockReturnValue(true);

      render(<AdminPage />);
      await waitFor(() => screen.getByLabelText('Default Model'));
      fireEvent.change(screen.getByLabelText('Default Model'), {
        target: { value: 'llama3.1:8b' },
      });
      await waitFor(() => {
        expect(screen.getByLabelText('Default Model')).toHaveValue('llama3.1:8b');
      });

      const cancelLink = screen.getByRole('link', { name: /cancel/i });
      const notPrevented = fireEvent.click(cancelLink);

      expect(global.confirm).toHaveBeenCalledWith(
        expect.stringContaining('unsaved changes')
      );
      expect(notPrevented).toBe(true);
      expect(saveCalled).toBe(false);
    });

    it('stays on the wizard with edits intact when the confirmation is dismissed', async () => {
      global.confirm = vi.fn().mockReturnValue(false);

      render(<AdminPage />);
      await waitFor(() => screen.getByLabelText('Default Model'));
      fireEvent.change(screen.getByLabelText('Default Model'), {
        target: { value: 'llama3.1:8b' },
      });
      await waitFor(() => {
        expect(screen.getByLabelText('Default Model')).toHaveValue('llama3.1:8b');
      });

      const cancelLink = screen.getByRole('link', { name: /cancel/i });
      const notPrevented = fireEvent.click(cancelLink);

      expect(global.confirm).toHaveBeenCalled();
      expect(notPrevented).toBe(false);
      expect(screen.getByRole('heading', { name: 'Core & Model' })).toBeInTheDocument();
      expect(screen.getByLabelText('Default Model')).toHaveValue('llama3.1:8b');
    });

    it('disables Cancel while a save is in progress', async () => {
      server.use(
        http.post('/api/v1/config/sections', async () => {
          await new Promise((resolve) => setTimeout(resolve, 200));
          return HttpResponse.json({
            applied: {},
            sections: {
              nyxgpt: {
                default_model: 'llama3.1:8b',
                chat_timeout_seconds: '120',
                sessions_dir: '',
                vectorstore_dir: '',
              },
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

      const location = stubLocationHref();
      render(<AdminPage />);
      await selectModelAndClickNext(5);

      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /saving/i })).toBeInTheDocument();
      });

      const cancelLink = screen.getByRole('link', { name: /cancel/i });
      expect(cancelLink).toHaveAttribute('aria-disabled', 'true');
      const notPrevented = fireEvent.click(cancelLink);

      expect(global.confirm).not.toHaveBeenCalled();
      expect(notPrevented).toBe(false);

      await waitFor(
        () => {
          expect(location.setter).toHaveBeenCalledWith('/admin/dashboard');
        },
        { timeout: 3000 }
      );

      location.restore();
    });
  });

  /**
   * Additional Settings step (#3388): every config.ini field the backend's
   * derived schema declares but this file doesn't hand-code a widget for
   * (see `KNOWN_FIELDS` in page.tsx) renders generically here, grouped by
   * topic. Also covers the drift-reconciliation stale-key banner.
   */
  describe('pending-restart notice (#3806)', () => {
    it('shows nothing on entry when every saved value is already running', async () => {
      render(<AdminPage />);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: /configuration wizard/i })).toBeInTheDocument();
      });
      expect(screen.queryByRole('alert', { name: /restart required/i })).not.toBeInTheDocument();
    });

    it('shows a restart owed from an earlier visit or from `nyxgpt secrets setup`', async () => {
      // The state is the backend's, shared with the CLI -- so a key rotated
      // outside the browser must announce itself the moment the wizard opens.
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({
            pending: { web: { keys: ['auth.api_key'], since: 1 } },
            restart_command: 'nyxgpt ops restart web',
            session_disrupting: ['web'],
          })
        )
      );

      render(<AdminPage />);
      await waitFor(() => {
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      });
      expect(screen.getByText(/not yet in effect/i)).toBeInTheDocument();
      expect(screen.getByText(/auth\.api_key/)).toBeInTheDocument();
    });

    it('raises the notice when a save reports a restart-required key as pending', async () => {
      // The wizard reconciles with GET /infra/restart-status after every save,
      // so the notice reflects the backend's own view rather than a
      // client-side guess about what the payload implied.
      server.use(
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({
            pending: { web: { keys: ['auth.api_key'], since: 1 } },
            restart_command: 'nyxgpt ops restart web',
            session_disrupting: ['web'],
          })
        )
      );

      render(<AdminPage />);
      await selectModelAndClickNext(1);
      await waitFor(() => {
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
      });

      await waitFor(() => {
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      });
      // ...and it names the tier that is still running the old value, rather
      // than leaving the user to discover a 401 wall (#3806).
      expect(screen.getByText(/drop this browser session/i)).toBeInTheDocument();
    });

    it('shows the save response\'s own pending set when the reconcile fetch fails', async () => {
      // Two things at once, both of which the earlier tests left untested:
      // the notice is derived from the save response *before* the reconcile
      // round trip lands (so it never flickers absent), and a reconcile that
      // fails leaves that derived state standing rather than blanking the
      // notice on a transient error. `session_disrupting` is computed
      // client-side here, so this is also what proves the wizard names the
      // session-dropping tier without waiting for the backend to say so.
      server.use(
        http.post('/api/v1/config/sections', async () =>
          HttpResponse.json({
            applied: {},
            sections: {
              nyxgpt: {
                default_model: 'llama3.1:8b',
                chat_timeout_seconds: '120',
                sessions_dir: '',
                vectorstore_dir: '',
              },
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
            restart_required: ['api', 'web'],
            restart_pending: {
              web: { keys: ['auth.api_key'], since: Math.floor(Date.now() / 1000) },
              api: { keys: ['api.port'], since: Math.floor(Date.now() / 1000) },
            },
            observability_reconciled: false,
            observability_result: null,
          })
        ),
        http.get('/api/v1/infra/restart-status', () =>
          HttpResponse.json({}, { status: 502 })
        )
      );

      render(<AdminPage />);
      await selectModelAndClickNext(1);
      await waitFor(() => {
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
      });

      await waitFor(() => {
        expect(screen.getByRole('alert', { name: /restart required/i })).toBeInTheDocument();
      });
      expect(screen.getByText(/auth\.api_key/)).toBeInTheDocument();
      expect(screen.getByText(/api\.port/)).toBeInTheDocument();
      // Only `web` drops the session -- `api` is in the same pending set and
      // must not be described that way.
      expect(screen.getByText(/drop this browser session/i)).toBeInTheDocument();
    });

    it('renders a per-field activation hint before the save, not only after', async () => {
      render(<AdminPage />);
      await selectModelAndClickNext(2);
      await waitFor(() => {
        expect(screen.getByLabelText('API Host')).toBeInTheDocument();
      });
      expect(
        screen.getAllByText(/not in effect until you restart: api/i).length
      ).toBeGreaterThan(0);
    });
  });

  describe('Additional Settings step (#3388)', () => {
    const SCHEMA_WITH_EXTRAS = [
      {
        section: 'cache',
        label: 'Caching',
        fields: [
          { key: 'embedding_cache_enabled', secret: false, restart_components: ['api'], observability: false },
          { key: 'embedding_cache_dir', secret: false, restart_components: ['api'], observability: false },
        ],
      },
      {
        section: 'monitoring',
        label: 'Monitoring',
        fields: [
          { key: 'enabled', secret: false, restart_components: [], observability: true },
          { key: 'grafana_admin_password', secret: true, restart_components: [], observability: false },
        ],
      },
    ];

    const SECTIONS_WITH_EXTRAS = {
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
      monitoring: {
        enabled: 'false',
        grafana_admin_password: { set: true, masked: 'sekr****xxxx' },
      },
      log_aggregation: { enabled: 'false' },
      cache: { embedding_cache_enabled: 'false', embedding_cache_dir: '~/.nyxGPT/cache/embeddings' },
    };

    async function goToMoreStep() {
      render(<AdminPage />);
      await selectModelAndClickNext(4);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Additional Settings' })).toBeInTheDocument();
      });
    }

    it('renders a generic field for every schema field not hand-coded elsewhere', async () => {
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      const cacheToggle = screen.getByRole('checkbox', { name: /Embedding Cache Enabled/ });
      expect(cacheToggle).not.toBeChecked();
      expect(screen.getByLabelText('Embedding Cache Dir')).toHaveValue('~/.nyxGPT/cache/embeddings');

      const secretInput = screen.getByLabelText('Grafana Admin Password') as HTMLInputElement;
      expect(secretInput).toHaveAttribute('type', 'password');
      expect(secretInput).toHaveAttribute(
        'placeholder',
        'Set (sekr****xxxx) -- leave blank to keep'
      );
    });

    it('renders the Slack bot token as a masked secret and leaves it alone when not retyped (#3947)', async () => {
      // The backend now declares `[monitoring] slack_bot_token` secret=True,
      // so it arrives as {set, masked} instead of the cleartext string it
      // used to be. Nothing in this file is field-specific -- the generic
      // extras path drives it off `f.secret` -- but that genericity is
      // exactly what made the defect invisible, so the field the leak
      // happened through is pinned here rather than assumed covered.
      let capturedBody: Record<string, Record<string, unknown>> | undefined;
      const schemaWithBotToken = [
        SCHEMA_WITH_EXTRAS[0],
        {
          ...SCHEMA_WITH_EXTRAS[1],
          fields: [
            ...SCHEMA_WITH_EXTRAS[1].fields,
            { key: 'slack_bot_token', secret: true, restart_components: [], observability: false },
          ],
        },
      ];
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: {
              ...SECTIONS_WITH_EXTRAS,
              monitoring: {
                ...SECTIONS_WITH_EXTRAS.monitoring,
                slack_bot_token: { set: true, masked: 'xoxb****9876' },
              },
            },
            schema: schemaWithBotToken,
            field_defaults: {},
            stale_keys: {},
          })
        ),
        http.post('/api/v1/config/sections', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
          return HttpResponse.json({
            applied: capturedBody,
            sections: SECTIONS_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
            restart_required: [],
            observability_reconciled: false,
            observability_result: null,
          });
        })
      );

      await goToMoreStep();

      const tokenInput = screen.getByLabelText('Slack Bot Token') as HTMLInputElement;
      expect(tokenInput).toHaveAttribute('type', 'password');
      expect(tokenInput).toHaveValue('');
      expect(tokenInput).toHaveAttribute(
        'placeholder',
        'Set (xoxb****9876) -- leave blank to keep'
      );

      // Change something else, then save without touching the token.
      fireEvent.change(screen.getByLabelText('Embedding Cache Dir'), {
        target: { value: '/somewhere/else' },
      });

      await clickNext(1);
      await waitFor(() => {
        expect(screen.getByRole('heading', { name: 'Review Configuration' })).toBeInTheDocument();
      });
      // Summary shows the masked preview, never a value.
      expect(screen.getByText('xoxb****9876')).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));

      await waitFor(() => {
        expect(capturedBody?.cache).toBeDefined();
      });
      // Untouched secret is omitted entirely, so the on-disk token survives.
      expect(capturedBody?.monitoring?.slack_bot_token).toBeUndefined();
    });

    it('names every service a field goes stale on, not just the first (#3806)', async () => {
      // Nothing in the shipped classification is restart-required for two
      // tiers at once today, but the classification is data the backend owns
      // and the mechanism is general by design -- the hint has to read
      // correctly the first time a key is classified for both, rather than
      // silently dropping one.
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: [
              {
                ...SCHEMA_WITH_EXTRAS[0],
                fields: [
                  {
                    key: 'embedding_cache_dir',
                    secret: false,
                    restart_components: ['api', 'web'],
                    observability: false,
                  },
                ],
              },
            ],
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      expect(
        screen.getByText(/not in effect until you restart: api and web\./i)
      ).toBeInTheDocument();
    });

    it('does not re-render fields the hand-typed sections already cover', async () => {
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      // "enabled" for monitoring is already a hand-coded checkbox in the
      // Observability step -- the generic renderer must not duplicate it.
      expect(screen.queryAllByRole('checkbox', { name: /^Enabled$/ })).toHaveLength(0);
    });

    it('includes an edited generic field in the save payload, type-coerced', async () => {
      let capturedBody: Record<string, Record<string, unknown>> | undefined;
      // embedding_cache_dir is flagged as an inherited default here so the
      // "skip untouched defaults" rule (mirrors buildSavePayload's `include()`,
      // #3385) omits it -- only the field the user actually toggled should
      // appear in the payload.
      const fieldDefaults = { cache: { embedding_cache_enabled: false, embedding_cache_dir: true } };
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: fieldDefaults,
            stale_keys: {},
          })
        ),
        http.post('/api/v1/config/sections', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
          return HttpResponse.json({
            applied: capturedBody,
            sections: SECTIONS_WITH_EXTRAS,
            field_defaults: fieldDefaults,
            stale_keys: {},
            restart_required: [],
            observability_reconciled: false,
            observability_result: null,
          });
        })
      );

      await goToMoreStep();
      fireEvent.click(screen.getByRole('checkbox', { name: /Embedding Cache Enabled/ }));

      await clickNext(1);
      await waitFor(() => {
        fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
      });

      await waitFor(() => {
        expect(capturedBody?.cache).toEqual({ embedding_cache_enabled: true });
      });
    });

    it('shows a stale-keys banner and removes a key on confirmation', async () => {
      let removeRequestBody: unknown;
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: { nyxgpt: ['retired_option'] },
          })
        ),
        http.post('/api/v1/config/sections/stale-keys/remove', async ({ request }) => {
          removeRequestBody = await request.json();
          return HttpResponse.json({
            removed: { nyxgpt: ['retired_option'] },
            sections: SECTIONS_WITH_EXTRAS,
            stale_keys: {},
          });
        })
      );

      await goToMoreStep();

      expect(screen.getByText('Config.ini has options no longer recognized')).toBeInTheDocument();
      expect(screen.getByText('[nyxgpt] retired_option')).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

      await waitFor(() => {
        expect(screen.queryByText('Config.ini has options no longer recognized')).not.toBeInTheDocument();
      });
      expect(removeRequestBody).toEqual({ remove: { nyxgpt: ['retired_option'] } });
    });

    it('leaves the stale-keys banner in place when removal fails', async () => {
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: { nyxgpt: ['retired_option'] },
          })
        ),
        http.post('/api/v1/config/sections/stale-keys/remove', () =>
          HttpResponse.json({ error: 'removal failed' }, { status: 500 })
        )
      );

      await goToMoreStep();
      fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Remove' })).not.toBeDisabled();
      });
      expect(screen.getByText('Config.ini has options no longer recognized')).toBeInTheDocument();
      expect(screen.getByText('[nyxgpt] retired_option')).toBeInTheDocument();
    });

    it('resets raw sections and stale keys safely when a removal response omits them', async () => {
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: SCHEMA_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: { nyxgpt: ['retired_option'] },
          })
        ),
        http.post('/api/v1/config/sections/stale-keys/remove', () => HttpResponse.json({}))
      );

      await goToMoreStep();
      fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

      await waitFor(() => {
        expect(
          screen.queryByText('Config.ini has options no longer recognized')
        ).not.toBeInTheDocument();
      });
      expect(screen.getByLabelText('Embedding Cache Dir')).toHaveValue('');
    });

    it('falls back to no extra fields when the schema is omitted from the response', async () => {
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      expect(screen.queryByLabelText('Embedding Cache Enabled')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Grafana Admin Password')).not.toBeInTheDocument();
    });

    it('treats a section fully covered by hand-built fields as having no extras', async () => {
      const schemaWithFullyCoveredSection = [
        ...SCHEMA_WITH_EXTRAS,
        {
          section: 'auth',
          label: 'Auth',
          fields: [
            { key: 'enabled', secret: false, restart_components: ['api'], observability: false },
            { key: 'header', secret: false, restart_components: ['api'], observability: false },
            { key: 'api_key', secret: true, restart_components: ['api'], observability: false },
          ],
        },
      ];
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: SECTIONS_WITH_EXTRAS,
            schema: schemaWithFullyCoveredSection,
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      // "auth" only declares fields already hand-coded in the API & Auth step
      // -- the generic renderer must produce zero extra fields for it, not a
      // duplicate "Enabled"/"Header"/"Api Key" group under Additional Settings.
      expect(screen.queryByText('Auth')).not.toBeInTheDocument();
    });

    it('renders a non-string raw value as free text and humanizes an unusual key', async () => {
      const schemaWithOddField = [
        {
          section: 'cache',
          label: 'Caching',
          fields: [
            ...SCHEMA_WITH_EXTRAS[0].fields,
            { key: 'embedding_cache_max_size', secret: false, restart_components: ['api'], observability: false },
            { key: 'example__setting', secret: false, restart_components: [], observability: false },
          ],
        },
        SCHEMA_WITH_EXTRAS[1],
      ];
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: {
              ...SECTIONS_WITH_EXTRAS,
              cache: {
                ...SECTIONS_WITH_EXTRAS.cache,
                embedding_cache_max_size: 1000,
                example__setting: 'plain text',
              },
            },
            schema: schemaWithOddField,
            field_defaults: {},
            stale_keys: {},
          })
        )
      );

      await goToMoreStep();

      // A JSON number (not the usual config.ini string) must still render as
      // plain text rather than crashing or being coerced into a number input.
      const maxSizeInput = screen.getByLabelText('Embedding Cache Max Size') as HTMLInputElement;
      expect(maxSizeInput).toHaveAttribute('type', 'text');
      expect(maxSizeInput).toHaveValue('1000');

      // Testing Library's default text matcher collapses whitespace, so the
      // literal double space from splitting "example__setting" on "_" reads
      // as a single space here even though humanizeKey emits two.
      expect(screen.getByLabelText('Example Setting')).toHaveValue('plain text');
    });

    it('edits secret, numeric, and boolean generic fields, coercing each type in the save payload', async () => {
      let capturedBody: Record<string, Record<string, unknown>> | undefined;
      const schemaWithNumericField = [
        {
          section: 'cache',
          label: 'Caching',
          fields: [
            ...SCHEMA_WITH_EXTRAS[0].fields,
            { key: 'embedding_cache_ttl_seconds', secret: false, restart_components: ['api'], observability: false },
          ],
        },
        SCHEMA_WITH_EXTRAS[1],
      ];
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: {
              ...SECTIONS_WITH_EXTRAS,
              cache: { ...SECTIONS_WITH_EXTRAS.cache, embedding_cache_ttl_seconds: '3600' },
            },
            schema: schemaWithNumericField,
            field_defaults: {},
            stale_keys: {},
          })
        ),
        http.post('/api/v1/config/sections', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
          return HttpResponse.json({
            applied: capturedBody,
            sections: SECTIONS_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
            restart_required: [],
            observability_reconciled: false,
            observability_result: null,
          });
        })
      );

      await goToMoreStep();

      // Boolean field: toggle on, then back off, to exercise both outcomes.
      const cacheToggle = screen.getByRole('checkbox', { name: /Embedding Cache Enabled/ });
      fireEvent.click(cacheToggle);
      fireEvent.click(cacheToggle);

      // Text field.
      fireEvent.change(screen.getByLabelText('Embedding Cache Dir'), {
        target: { value: '/custom/cache/dir' },
      });

      // Numeric field (numeric string -> coerced to a JS number).
      fireEvent.change(screen.getByLabelText('Embedding Cache Ttl Seconds'), {
        target: { value: '7200' },
      });

      // Secret field.
      fireEvent.change(screen.getByLabelText('Grafana Admin Password'), {
        target: { value: 'new-secret' },
      });

      await clickNext(1);
      await waitFor(() => {
        fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
      });

      await waitFor(() => {
        expect(capturedBody?.cache).toEqual({
          embedding_cache_enabled: false,
          embedding_cache_dir: '/custom/cache/dir',
          embedding_cache_ttl_seconds: 7200,
        });
      });
      expect(capturedBody?.monitoring?.grafana_admin_password).toBe('new-secret');
    });

    it('renders a drift field missing from config.ini and an observability hint, and saves multiple secrets in one section', async () => {
      let capturedBody: Record<string, Record<string, unknown>> | undefined;
      const schemaEdgeCases = [
        {
          section: 'cache',
          label: 'Caching',
          fields: [
            { key: 'embedding_cache_enabled', secret: false, restart_components: [], observability: false },
            { key: 'missing_from_config', secret: false, restart_components: [], observability: true },
          ],
        },
        {
          section: 'monitoring',
          label: 'Monitoring',
          fields: [
            { key: 'enabled', secret: false, restart_components: [], observability: true },
            { key: 'grafana_admin_password', secret: true, restart_components: [], observability: false },
            { key: 'admin_email_secret', secret: true, restart_components: [], observability: false },
          ],
        },
      ];
      server.use(
        http.get('/api/v1/config/sections', () =>
          HttpResponse.json({
            sections: {
              ...SECTIONS_WITH_EXTRAS,
              // "missing_from_config" is declared by the schema but absent
              // here -- config.ini drift: the key hasn't been added yet.
              cache: { embedding_cache_enabled: 'false' },
              monitoring: {
                enabled: 'false',
                grafana_admin_password: { set: false, masked: null },
                admin_email_secret: { set: false, masked: null },
              },
            },
            schema: schemaEdgeCases,
            field_defaults: {},
            stale_keys: {},
          })
        ),
        http.post('/api/v1/config/sections', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, Record<string, unknown>>;
          return HttpResponse.json({
            applied: capturedBody,
            sections: SECTIONS_WITH_EXTRAS,
            field_defaults: {},
            stale_keys: {},
            restart_required: [],
            observability_reconciled: false,
            observability_result: null,
          });
        })
      );

      await goToMoreStep();

      expect(screen.getByLabelText('Missing From Config')).toHaveValue('');
      expect(
        screen.getByText('Reconciles the observability stack on save.')
      ).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText('Grafana Admin Password'), {
        target: { value: 'secret-one' },
      });
      fireEvent.change(screen.getByLabelText('Admin Email Secret'), {
        target: { value: 'secret-two' },
      });

      await clickNext(1);
      await waitFor(() => {
        fireEvent.click(screen.getByRole('button', { name: /save configuration/i }));
      });

      await waitFor(() => {
        expect(capturedBody?.monitoring).toMatchObject({
          grafana_admin_password: 'secret-one',
          admin_email_secret: 'secret-two',
        });
      });
    });
  });
});

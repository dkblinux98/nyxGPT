import { http, HttpResponse } from 'msw';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';

/**
 * Mirrors `config_wizard.schema_summary()`'s derived schema (#3388) for the
 * 11 sections the wizard hand-builds fields for (#3354) -- section labels
 * match `config_wizard._SECTION_LABELS`, and secret/restart_components/
 * observability flags match `config_wizard._FIELD_OVERRIDES`, so tests that
 * rely on the default `GET /api/v1/config/sections` mock exercise the same
 * schema-driven Summary step (#3407) real traffic would.
 */
export const FULL_WIZARD_SCHEMA = [
  {
    section: 'nyxgpt',
    label: 'Core & model',
    fields: [
      { key: 'default_model', secret: false, restart_components: [], observability: false },
      { key: 'chat_timeout_seconds', secret: false, restart_components: [], observability: false },
      { key: 'sessions_dir', secret: false, restart_components: [], observability: false },
      { key: 'vectorstore_dir', secret: false, restart_components: [], observability: false },
    ],
  },
  {
    section: 'logging',
    label: 'Logging',
    fields: [
      { key: 'level', secret: false, restart_components: [], observability: false },
      { key: 'dir', secret: false, restart_components: [], observability: false },
    ],
  },
  {
    section: 'ollama',
    label: 'Model backend',
    fields: [{ key: 'base_url', secret: false, restart_components: [], observability: false }],
  },
  {
    section: 'api',
    label: 'API server',
    fields: [
      { key: 'host', secret: false, restart_components: ['api'], observability: false },
      { key: 'port', secret: false, restart_components: ['api'], observability: false },
    ],
  },
  {
    section: 'auth',
    label: 'Authentication',
    fields: [
      // Mirrors the real classification (#3806): the api tier re-reads [auth]
      // per request, the web tier freezes it into its environment at start.
      { key: 'enabled', secret: false, restart_components: ['web'], observability: false },
      { key: 'header', secret: false, restart_components: [], observability: false },
      { key: 'api_key', secret: true, restart_components: ['web'], observability: false },
    ],
  },
  {
    section: 'rate_limit',
    label: 'Rate limiting',
    fields: [{ key: 'enabled', secret: false, restart_components: ['api'], observability: false }],
  },
  {
    section: 'rag',
    label: 'RAG / retrieval',
    fields: [
      { key: 'enable_chat_context', secret: false, restart_components: [], observability: false },
      { key: 'cassandra_hosts', secret: false, restart_components: ['api'], observability: false },
      { key: 'cassandra_port', secret: false, restart_components: ['api'], observability: false },
      { key: 'cassandra_keyspace', secret: false, restart_components: ['api'], observability: false },
      { key: 'cassandra_table', secret: false, restart_components: ['api'], observability: false },
      { key: 'embedding_model', secret: false, restart_components: ['api'], observability: false },
    ],
  },
  {
    section: 'tracing',
    label: 'Tracing',
    fields: [
      { key: 'enabled', secret: false, restart_components: ['api'], observability: true },
      { key: 'service_name', secret: false, restart_components: [], observability: false },
      { key: 'otlp_endpoint', secret: false, restart_components: [], observability: false },
    ],
  },
  {
    section: 'error_tracking',
    label: 'Error tracking',
    fields: [
      { key: 'enabled', secret: false, restart_components: ['api'], observability: true },
      { key: 'dsn', secret: true, restart_components: [], observability: false },
      { key: 'environment', secret: false, restart_components: [], observability: false },
    ],
  },
  {
    section: 'monitoring',
    label: 'Monitoring',
    fields: [{ key: 'enabled', secret: false, restart_components: [], observability: true }],
  },
  {
    section: 'log_aggregation',
    label: 'Log aggregation',
    fields: [{ key: 'enabled', secret: false, restart_components: [], observability: true }],
  },
];

export const handlers = [
  // GET /api/models
  http.get(`${API_BASE_URL}/api/v1/models`, () => {
    return HttpResponse.json({
      models: ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b'],
    });
  }),

  // POST /api/chat/stream
  http.post('/api/chat/stream', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('Hello! '));
        controller.enqueue(encoder.encode('How can I help you?'));
        controller.close();
      },
    });

    return new HttpResponse(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
      },
    });
  }),

  // GET /api/sessions/:name/metadata
  http.get('/api/sessions/:name/metadata', ({ params }) => {
    return HttpResponse.json({
      name: params.name,
      rag_enabled: false,
      title: `Session ${params.name}`,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    });
  }),

  // POST /api/sessions/:name/rag/enable
  http.post('/api/sessions/:name/rag/enable', () => {
    return HttpResponse.json({ success: true });
  }),

  // POST /api/sessions/:name/rag/disable
  http.post('/api/sessions/:name/rag/disable', () => {
    return HttpResponse.json({ success: true });
  }),

  // POST /api/rag/upload
  http.post('/api/rag/upload', () => {
    return HttpResponse.json({
      doc_id: 'mock-doc-id-123',
      filename: 'test.txt',
    });
  }),

  // GET /api/v1/sessions/:name/export
  http.get(`${API_BASE_URL}/api/v1/sessions/:name/export`, ({ params, request }) => {
    const url = new URL(request.url);
    const format = url.searchParams.get('format') || 'markdown';
    const sessionName = params.name as string;

    let content = '';
    let contentType = '';
    let filename = '';

    if (format === 'markdown') {
      content = `# ${sessionName}\n\n## User\nTest message\n\n## Assistant\nTest response`;
      contentType = 'text/markdown';
      filename = `${sessionName}.md`;
    } else if (format === 'json') {
      content = JSON.stringify({
        name: sessionName,
        messages: [
          { role: 'user', content: 'Test message' },
          { role: 'assistant', content: 'Test response' },
        ],
      });
      contentType = 'application/json';
      filename = `${sessionName}.json`;
    } else if (format === 'html') {
      content = `<!DOCTYPE html><html><head><title>${sessionName}</title></head><body><h1>${sessionName}</h1></body></html>`;
      contentType = 'text/html';
      filename = `${sessionName}.html`;
    }

    return new HttpResponse(content, {
      headers: {
        'Content-Type': contentType,
        'Content-Disposition': `attachment; filename="${filename}"`,
      },
    });
  }),

  // GET /api/config
  http.get('/api/config', () => {
    return HttpResponse.json({
      ollama_base_url: 'http://127.0.0.1:11434',
      default_model: '',
      rag_enabled: false,
      log_level: 'INFO',
    });
  }),

  // POST /api/config
  http.post('/api/config', async ({ request }) => {
    const body = await request.json();
    return HttpResponse.json(body);
  }),

  // GET /api/v1/config/sections
  http.get('/api/v1/config/sections', () => {
    return HttpResponse.json({
      sections: {
        nyxgpt: {
          default_model: '',
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
      schema: FULL_WIZARD_SCHEMA,
    });
  }),

  // POST /api/v1/config/sections
  http.post('/api/v1/config/sections', async ({ request }) => {
    const body = (await request.json()) as Record<string, Record<string, unknown>>;
    return HttpResponse.json({
      applied: body,
      sections: {
        nyxgpt: {
          default_model: body.nyxgpt?.default_model ?? '',
          chat_timeout_seconds: String(body.nyxgpt?.chat_timeout_seconds ?? '120'),
          sessions_dir: body.nyxgpt?.sessions_dir ?? '~/.nyxGPT/sessions',
          vectorstore_dir: body.nyxgpt?.vectorstore_dir ?? '~/.nyxGPT/vectorstore',
        },
        logging: {
          level: body.logging?.level ?? 'INFO',
          dir: body.logging?.dir ?? '~/.nyxGPT/logs',
        },
        ollama: { base_url: body.ollama?.base_url ?? 'http://127.0.0.1:11434' },
        api: {
          host: body.api?.host ?? '127.0.0.1',
          port: String(body.api?.port ?? '8000'),
        },
        auth: {
          enabled: String(body.auth?.enabled ?? false),
          header: body.auth?.header ?? 'X-API-Key',
          api_key: { set: false, masked: null },
        },
        rate_limit: { enabled: String(body.rate_limit?.enabled ?? false) },
        rag: {
          enable_chat_context: String(body.rag?.enable_chat_context ?? false),
          cassandra_hosts: body.rag?.cassandra_hosts ?? '127.0.0.1',
          cassandra_port: String(body.rag?.cassandra_port ?? '9042'),
          cassandra_keyspace: body.rag?.cassandra_keyspace ?? 'nyxgpt',
          cassandra_table: body.rag?.cassandra_table ?? 'rag_chunks',
          embedding_model: body.rag?.embedding_model ?? 'nomic-embed-text',
        },
        tracing: {
          enabled: String(body.tracing?.enabled ?? false),
          service_name: body.tracing?.service_name ?? 'nyxgpt-api',
          otlp_endpoint: body.tracing?.otlp_endpoint ?? 'http://localhost:4318/v1/traces',
        },
        error_tracking: {
          enabled: String(body.error_tracking?.enabled ?? false),
          dsn: { set: false, masked: null },
          environment: body.error_tracking?.environment ?? 'development',
        },
        monitoring: { enabled: String(body.monitoring?.enabled ?? false) },
        log_aggregation: { enabled: String(body.log_aggregation?.enabled ?? false) },
      },
      restart_required: [],
      observability_reconciled: false,
      observability_result: null,
    });
  }),

  // POST /api/v1/config/restart
  http.post('/api/v1/config/restart', async ({ request }) => {
    const body = (await request.json()) as { target?: string };
    return HttpResponse.json({ target: body.target || 'all', status: 'scheduled' });
  }),

  // GET /api/info
  http.get('/api/info', () => {
    return HttpResponse.json({
      version: '1.0.0',
      status: 'ok',
    });
  }),

  // GET /api/models (relative URL for admin page)
  http.get('/api/models', () => {
    return HttpResponse.json({
      models: ['llama3.1:8b', 'llama3.1:70b', 'mistral:7b'],
    });
  }),

  // GET /api/v1/sessions/:name
  http.get(/\/api\/v1\/sessions\/[^/]+$/, () => {
    return HttpResponse.json({
      messages: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    });
  }),

  // GET /api/v1/admin/overview
  http.get('/api/v1/admin/overview', () => {
    return HttpResponse.json({
      info: {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: false,
      },
      resource_metrics: {
        memory: { rss_mb: 128, vms_mb: 256, percent: 5, available_mb: 8000 },
        cpu: { process_percent: 2.5, system_percent: 10 },
        latency: { avg_ms: 10, p50_ms: 8, p95_ms: 20, p99_ms: 30 },
        queue: { depth: 0, total_requests: 42 },
      },
      canary: { namespace: 'nyxgpt', active: false },
      self_heal: { enabled: false, components: [], unhealthy_count: 0, events: [] },
      observability: {
        monitoring: false,
        tracing: false,
        error_tracking: false,
        log_aggregation: false,
      },
      auth_enabled: false,
    });
  }),

  // GET /api/v1/monitoring
  http.get('/api/v1/monitoring', () => {
    return HttpResponse.json({
      enabled: true,
      active: true,
      grafana_ui_url: 'http://localhost:3001',
      prometheus_ui_url: 'http://localhost:9090',
    });
  }),

  // GET /api/v1/infra/restart-status
  http.get('/api/v1/infra/restart-status', () => {
    return HttpResponse.json({ pending: {}, restart_command: null, session_disrupting: [] });
  }),

  // POST /api/v1/infra/restart-required
  http.post('/api/v1/infra/restart-required', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { target?: string };
    return HttpResponse.json({ targets: body.target ? [body.target] : ['api'], status: 'running' });
  }),

  // GET /api/v1/admin/health
  http.get('/api/v1/admin/health', () => {
    return HttpResponse.json({
      service: { status: 'ok', uptime_s: 3725 },
      dependencies: [
        { name: 'ollama', ok: true, detail: 'Reachable at http://127.0.0.1:11434', applicable: true },
        { name: 'cassandra', ok: true, detail: 'RAG disabled; Cassandra is not required', applicable: false },
      ],
      resource_metrics: {
        memory: { rss_mb: 128, vms_mb: 256, percent: 5, available_mb: 8000 },
        cpu: { process_percent: 2.5, system_percent: 10 },
        disk: { percent: 12 },
        latency: { avg_ms: 10, p50_ms: 8, p95_ms: 20, p99_ms: 30 },
        queue: { depth: 0, total_requests: 42 },
        errors: { count: 0, rate_percent: 0 },
      },
      alerts_source: 'local',
      alerts: [],
    });
  }),

  // GET /api/v1/admin/activity
  http.get('/api/v1/admin/activity', () => {
    return HttpResponse.json({
      events: [
        { ts: 1768300800, action: 'config.updated', detail: 'log_level=DEBUG' },
        { ts: 1768300900, action: 'canary.deploy', detail: 'Deployed nyxgpt-api:1.2.3-abcd123 to nyxgpt-api-canary' },
      ],
    });
  }),

  // GET /api/v1/admin/access
  http.get('/api/v1/admin/access', () => {
    return HttpResponse.json({
      enabled: false,
      header: 'X-API-Key',
      api_key_set: false,
      api_key_masked: null,
    });
  }),

  // POST /api/v1/admin/access
  http.post('/api/v1/admin/access', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    const response: Record<string, unknown> = {
      enabled: body.enabled ?? false,
      header: body.header ?? 'X-API-Key',
      api_key_set: true,
      api_key_masked: 'newk********-key',
    };
    if (body.rotate) {
      response.api_key = 'newly-generated-key-value';
    }
    return HttpResponse.json(response);
  }),

  // GET /api/v1/rag/cache/stats
  http.get('/api/v1/rag/cache/stats', () => {
    return HttpResponse.json({
      hits: 8,
      misses: 2,
      hit_rate: 0.8,
      size: 3,
      enabled: true,
      backend: 'memory',
      max_size: 500,
      ttl_seconds: 300,
    });
  }),

  // POST /api/v1/rag/cache/clear
  http.post('/api/v1/rag/cache/clear', () => {
    return HttpResponse.json({ status: 'Query result cache cleared' });
  }),

  // GET /api/v1/ops/release-candidate -- shaped like
  // nyxgpt.release_candidate.plan() (#3727).
  http.get('/api/v1/ops/release-candidate', () => {
    return HttpResponse.json({
      branch: 'v3.0.0',
      channel: 'rc',
      channels: ['dev', 'rc', 'stable'],
      is_release_branch: true,
      branch_version: '3.0.0',
      declared_version: '3.0.0',
      version_matches_branch: true,
      release: '3.0.0',
      published_releases: ['2.1.0'],
      published_rcs: ['3.0.0rc1'],
      next_rc_number: 2,
      next_rc_version: '3.0.0rc2',
      rc_formulas: ['nyxgpt-api@3.0.0rc', 'nyxgpt-web@3.0.0rc'],
      version: '3.0.0rc2',
      is_prerelease: true,
      workflow: 'release-publish-pypi.yml',
      pypi_lookup_error: '',
      publishable: true,
      blockers: [],
      commands: {
        plan: 'nyxgpt release publish --channel rc',
        publish: 'nyxgpt release publish --channel rc --publish',
        brew:
          'brew tap dkblinux98/nyxgpt && (brew tap-trust dkblinux98/nyxgpt || ' +
          'brew trust dkblinux98/nyxgpt || true) && ' +
          'brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc',
        install: 'pip install nyxgpt==3.0.0rc2',
        user_data: 'nyxgpt cloud user-data --os linux --version 3.0.0rc2',
        deploy: 'nyxgpt cloud deploy --version 3.0.0rc2',
      },
      guardrails: [
        'Dispatch trigger only: the workflow has no schedule, push, tag or release trigger.',
      ],
      docs: 'docs/cloud.md#pypi-publishing-rc-and-stable',
    });
  }),
];

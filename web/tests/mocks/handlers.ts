import { http, HttpResponse } from 'msw';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';

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
      schema: [],
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

  // GET /api/v1/logs/files
  http.get('/api/v1/logs/files', () => {
    return HttpResponse.json({
      files: [
        {
          name: 'nyxgpt.log',
          path: '/home/user/.nyxGPT/logs/nyxgpt.log',
          size: 1024,
          modified: Date.now() / 1000,
        },
        {
          name: 'api.log',
          path: '/home/user/.nyxGPT/logs/api.log',
          size: 2048,
          modified: Date.now() / 1000 - 3600,
        },
      ],
      log_dir: '/home/user/.nyxGPT/logs',
    });
  }),

  // GET /api/v1/logs/view/:filename
  http.get('/api/v1/logs/view/:filename', ({ params, request }) => {
    const url = new URL(request.url);
    const tail = url.searchParams.get('tail');
    const level = url.searchParams.get('level');
    const search = url.searchParams.get('search');

    let lines = [
      '2026-01-13 12:00:00,000 - INFO - Server started',
      '2026-01-13 12:00:01,000 - DEBUG - Debug message',
      '2026-01-13 12:00:02,000 - WARNING - Warning message',
      '2026-01-13 12:00:03,000 - ERROR - Error message',
    ];

    // Apply filters
    if (level) {
      lines = lines.filter(line => line.includes(level));
    }
    if (search) {
      lines = lines.filter(line => line.toLowerCase().includes(search.toLowerCase()));
    }
    if (tail) {
      lines = lines.slice(-parseInt(tail));
    }

    return HttpResponse.json({
      filename: params.filename,
      lines,
      total_lines: 4,
      filtered_lines: lines.length,
    });
  }),

  // GET /api/v1/logs/stream/:filename
  http.get('/api/v1/logs/stream/:filename', ({ params }) => {
    const content = `2026-01-13 12:00:00,000 - INFO - Server started
2026-01-13 12:00:01,000 - DEBUG - Debug message
2026-01-13 12:00:02,000 - WARNING - Warning message
2026-01-13 12:00:03,000 - ERROR - Error message
`;
    return new HttpResponse(content, {
      headers: {
        'Content-Type': 'text/plain',
        'Content-Disposition': `inline; filename="${params.filename}"`,
        'X-Content-Type-Options': 'nosniff',
      },
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
      deploy: { namespace: 'nyxgpt', active: 'blue', inactive: 'green' },
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
        latency: { avg_ms: 10, p50_ms: 8, p95_ms: 20, p99_ms: 30 },
        queue: { depth: 0, total_requests: 42 },
        errors: { count: 0, rate_percent: 0 },
      },
      alerts: [],
    });
  }),

  // GET /api/v1/admin/activity
  http.get('/api/v1/admin/activity', () => {
    return HttpResponse.json({
      events: [
        { ts: 1768300800, action: 'config.updated', detail: 'log_level=DEBUG' },
        { ts: 1768300900, action: 'deploy.switch', detail: 'blue -> green' },
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
];

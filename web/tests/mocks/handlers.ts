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
];

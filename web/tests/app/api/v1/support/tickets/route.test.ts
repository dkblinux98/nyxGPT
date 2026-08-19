/**
 * Tests for the /api/v1/support/tickets Next.js proxy route (#3811).
 *
 * The one write on the Support surface, and the reason the filer never has
 * to visit github.com. What these pin is that it stays a *pass-through*: the
 * backend's status codes are the UI's branch points -- 503 means "this
 * install has no credential, here is the prefilled form", 502 means "GitHub
 * said no", 400 means "fix your input" -- so collapsing them into a generic
 * failure here would leave the dialog unable to say anything useful to
 * someone who is already reporting a problem.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/support/tickets/route';

const TICKET = {
  ticket_type: 'Bug Found',
  summary: 'Docs are a mess',
  description: 'I cannot find the install steps.',
};

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req(body: unknown = TICKET) {
  return new Request('http://localhost/api/v1/support/tickets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/support/tickets proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the ticket to the backend and returns the created issue', async () => {
    mockFetch({
      status: 201,
      data: {
        status: 'filed',
        number: 4300,
        url: 'https://github.com/dkblinux98/nyxGPT/issues/4300',
        title: 'support: Docs are a mess',
        labeled: true,
      },
    });

    const { POST } = await import(ROUTE);
    const response = (await POST(req())) as Response;

    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/support/tickets');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual(TICKET);
    // Filing is never served from a cache: the answer is a brand new ticket.
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(201);
    expect((await response.json()).number).toBe(4300);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 201 });

    const { POST } = await import(ROUTE);
    await POST(req());

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/support/tickets');
  });

  it('passes the no-credential answer through with its fallback link intact', async () => {
    // The dialog renders that link. Rewriting this into a generic 502 would
    // take away the only filing path a tokenless install has.
    mockFetch({
      status: 503,
      data: {
        status: 'no_credential',
        detail: 'This nyxGPT install has no GitHub token configured.',
        issue_form_url: 'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml',
      },
    });

    const { POST } = await import(ROUTE);
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(503);
    expect((await response.json()).issue_form_url).toContain('template=support.yml');
  });

  it('passes a rejected ticket through as a 400 with its reason', async () => {
    mockFetch({ status: 400, data: { detail: "Unknown ticket type: 'Production Defect'" } });

    const { POST } = await import(ROUTE);
    const response = (await POST(req({ ...TICKET, ticket_type: 'Production Defect' }))) as Response;

    expect(response.status).toBe(400);
    expect((await response.json()).detail).toContain('Unknown ticket type');
  });

  it('says the API is unreachable rather than blaming GitHub', async () => {
    // The distinction matters to the person waiting: their own nyxGPT is
    // down, and no ticket was created anywhere.
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(ROUTE);
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(502);
    expect((await response.json()).error).toContain('nyxgpt ops status');
  });

  it('tags the response with a correlation id', async () => {
    mockFetch({ status: 201 });

    const { POST } = await import(ROUTE);
    const response = (await POST(req())) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes filing and nothing else -- no read, no delete', async () => {
    const route = await import(ROUTE);

    expect(route.POST).toBeDefined();
    expect(route.GET).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});

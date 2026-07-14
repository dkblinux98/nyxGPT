import { render } from '@testing-library/react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import ClientErrorReporter from '../../src/components/ClientErrorReporter';
import { server } from '../mocks/server';

describe('ClientErrorReporter', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('reports uncaught window errors to the backend', async () => {
    let reportedBody: unknown = null;
    server.use(
      http.post('/api/v1/error-tracking/report', async ({ request }) => {
        reportedBody = await request.json();
        return HttpResponse.json({ status: 'accepted' }, { status: 202 });
      })
    );

    render(<ClientErrorReporter />);

    const error = new Error('boom');
    window.dispatchEvent(new ErrorEvent('error', { message: 'boom', error }));

    await vi.waitFor(() => {
      expect(reportedBody).not.toBeNull();
    });
    expect(reportedBody).toMatchObject({ message: 'boom' });
  });

  it('reports unhandled promise rejections to the backend', async () => {
    let reportedBody: unknown = null;
    server.use(
      http.post('/api/v1/error-tracking/report', async ({ request }) => {
        reportedBody = await request.json();
        return HttpResponse.json({ status: 'accepted' }, { status: 202 });
      })
    );

    render(<ClientErrorReporter />);

    const event = new Event('unhandledrejection') as PromiseRejectionEvent & {
      reason: unknown;
    };
    Object.defineProperty(event, 'reason', { value: new Error('rejected') });
    window.dispatchEvent(event);

    await vi.waitFor(() => {
      expect(reportedBody).not.toBeNull();
    });
    expect(reportedBody).toMatchObject({ message: 'rejected' });
  });

  it('never throws when the report request fails', async () => {
    server.use(
      http.post('/api/v1/error-tracking/report', () => HttpResponse.error())
    );

    render(<ClientErrorReporter />);

    expect(() => {
      window.dispatchEvent(new ErrorEvent('error', { message: 'boom' }));
    }).not.toThrow();
  });
});

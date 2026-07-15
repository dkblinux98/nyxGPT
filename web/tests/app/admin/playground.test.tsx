import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import PlaygroundPage from '../../../src/app/admin/playground/page';

describe('PlaygroundPage', () => {
  it('renders the standardized back-nav link and keeps the Show Comparison action', async () => {
    server.use(http.get('/api/v1/rag/collections', () => HttpResponse.json({ collections: [] })));

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Playground' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
    expect(screen.queryByRole('button', { name: /back to chat/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /show comparison/i })).toBeInTheDocument();
  });
});

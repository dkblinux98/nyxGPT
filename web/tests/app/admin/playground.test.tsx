import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import PlaygroundPage from '../../../src/app/admin/playground/page';

const sampleCollections = [
  { name: 'default', doc_count: 5, chunk_count: 40, embedding_models: [] },
];

function mockCollections(collections = sampleCollections) {
  server.use(http.get('/api/v1/rag/collections', () => HttpResponse.json({ collections })));
}

const fullResponse = {
  results: [
    {
      doc_id: 'doc-1',
      chunk_id: 0,
      text: 'green result',
      score: 0.91,
      similarity_score: 0.91,
      chunk_number: 1,
      total_chunks: 3,
      collection: 'alt-collection',
    },
    {
      doc_id: 'doc-2',
      chunk_id: 1,
      text: 'yellow result',
      score: 0.5,
      similarity_score: 0.5,
      chunk_number: 2,
      total_chunks: null,
      collection: 'default',
    },
    { doc_id: 'doc-3', chunk_id: 2, text: 'red result', score: 0.1, similarity_score: 0.1 },
  ],
  debug_info: {
    total_time_ms: 123.456,
    embedding_time_ms: 10.1,
    vector_search_time_ms: 20.2,
    keyword_search_time_ms: 30.3,
    fusion_time_ms: 5.5,
    query_expansion_time_ms: 2.2,
    reranking_time_ms: 15.15,
    original_query: 'full query',
    query_variants: ['full query', 'full query variant'],
    num_queries: 2,
    embedding_model: 'nomic-embed-text',
    embedding_dim: 768,
    raw_results_count: 10,
    vector_results_count: 6,
    keyword_results_count: 4,
    score_min: 0.1,
    score_max: 0.91,
    score_mean: 0.5,
    after_min_score_filter: 8,
    after_dedupe_filter: 5,
    after_max_chunks_filter: 3,
  },
  evaluation_metrics: {
    retrieval_accuracy: {
      results_returned: 3,
      unique_docs: 3,
      score_distribution: { min: 0.1, max: 0.91, mean: 0.5, median: 0.5 },
    },
    latency: {
      total_time_ms: 123.456,
      embedding_ms: 10.1,
      vector_search_ms: 20.2,
      keyword_search_ms: 30.3,
      fusion_ms: 5.5,
      reranking_ms: 15.15,
    },
    hit_rate: {
      success_rate: 0.75,
      threshold_performance: { '0.5': 0.75 },
    },
  },
};

const minimalResponse = {
  results: [],
  debug_info: null,
  evaluation_metrics: null,
};

const sparseResponse = {
  results: [{ doc_id: 'doc-9', chunk_id: 0, text: 'sparse result', score: 0.6, similarity_score: null }],
  debug_info: {
    total_time_ms: 50,
    embedding_time_ms: null,
    vector_search_time_ms: null,
    keyword_search_time_ms: null,
    fusion_time_ms: null,
    query_expansion_time_ms: null,
    reranking_time_ms: null,
    original_query: 'sparse query',
    query_variants: null,
    num_queries: 1,
    embedding_model: null,
    embedding_dim: null,
    raw_results_count: 1,
    vector_results_count: null,
    keyword_results_count: null,
    score_min: null,
    score_max: null,
    score_mean: null,
    after_min_score_filter: 1,
    after_dedupe_filter: 1,
    after_max_chunks_filter: 1,
  },
  evaluation_metrics: {
    retrieval_accuracy: {
      results_returned: 1,
      unique_docs: 1,
      score_distribution: { min: null, max: null, mean: null, median: null },
    },
    latency: {
      total_time_ms: 50,
      embedding_ms: null,
      vector_search_ms: null,
      keyword_search_ms: null,
      fusion_ms: null,
      reranking_ms: null,
    },
    hit_rate: {
      success_rate: null,
      threshold_performance: {},
    },
  },
};

describe('PlaygroundPage', () => {
  beforeEach(() => {
    localStorage.clear();
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('renders the back to chat link and keeps the Show Comparison action', async () => {
    mockCollections([]);

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Playground' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to chat/i });
    expect(link).toHaveAttribute('href', '/');
    const compareButton = screen.getByRole('button', { name: /show comparison/i });
    expect(compareButton).toBeInTheDocument();
    expect(compareButton).toBeDisabled();
  });

  it('shows a load-collections error, including the non-Error fallback', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('collections gremlin'));

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByText('collections gremlin')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /retry/i }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    fetchSpy.mockRestore();
  });

  it('shows an HTTP-status error when loading collections fails without throwing', async () => {
    server.use(http.get('/api/v1/rag/collections', () => new HttpResponse(null, { status: 500 })));

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load collections: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('defaults collections to an empty array when the field is omitted from the response', async () => {
    server.use(http.get('/api/v1/rag/collections', () => HttpResponse.json({})));

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Playground' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });

  it('loads valid query history from localStorage and silently ignores invalid JSON', async () => {
    const stored = [
      {
        id: 'query-stored-1',
        timestamp: 1768300800000,
        query: 'stored query',
        params: { top_k: 5, min_score: 0, collection: 'default', debug_mode: true, collect_metrics: true },
        results: [{ doc_id: 'doc-s', chunk_id: 0, text: 'stored text', score: 0.8, similarity_score: 0.8 }],
        debug_info: null,
        evaluation_metrics: null,
      },
    ];
    localStorage.setItem('rag-playground-history', JSON.stringify(stored));
    mockCollections();

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByText(/Query History \(1\)/)).toBeInTheDocument();
    });
    expect(screen.getByText('stored query')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByText('stored query'));
    await waitFor(() => {
      expect(screen.getByText('1 Results')).toBeInTheDocument();
    });
  });

  it('ignores unparsable localStorage history without crashing', async () => {
    localStorage.setItem('rag-playground-history', '{not-json');
    mockCollections();

    render(<PlaygroundPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Playground' })).toBeInTheDocument();
    });
    expect(screen.getByText('No queries yet')).toBeInTheDocument();
  });

  it('disables Run Query until a non-whitespace query is entered', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /run query/i })).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/enter your search query/i), '   ');
    expect(screen.getByRole('button', { name: /run query/i })).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'real query');
    expect(screen.getByRole('button', { name: /run query/i })).not.toBeDisabled();
  });

  it('runs a full query with metrics + debug enabled and renders every populated field', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/metrics/query', () => HttpResponse.json(fullResponse)));

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'full query');
    await user.click(screen.getByRole('button', { name: /run query/i }));

    await waitFor(() => {
      expect(screen.getByText('3 Results')).toBeInTheDocument();
    });
    expect(screen.getByText('green result')).toBeInTheDocument();
    expect(screen.getByText('0.9100')).toBeInTheDocument();
    expect(screen.getByText('0.5000')).toBeInTheDocument();
    expect(screen.getByText('0.1000')).toBeInTheDocument();

    // chunk_number + total_chunks -> "1 of 3", plus the non-default collection suffix.
    expect(screen.getByText(/doc-1 • Chunk 1 of 3 • alt-collection/)).toBeInTheDocument();
    // chunk_number without total_chunks -> bare chunk_number; collection "default" is never suffixed.
    expect(screen.getByText(/doc-2 • Chunk 2$/)).toBeInTheDocument();
    // Neither chunk_number nor collection -> falls back to chunk_id + 1, no collection suffix.
    expect(screen.getByText(/doc-3 • Chunk 3$/)).toBeInTheDocument();

    // Metrics tab
    await user.click(screen.getByRole('button', { name: 'metrics' }));
    expect(screen.getByText('Min:')).toBeInTheDocument();
    expect(screen.getByText('75.0%')).toBeInTheDocument();
    expect(screen.getByText(/Embedding: 10.10 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Vector Search: 20.20 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Keyword Search: 30.30 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Fusion: 5.50 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Reranking: 15.15 ms/)).toBeInTheDocument();

    // Debug tab
    await user.click(screen.getByRole('button', { name: 'debug' }));
    expect(screen.getByText(/Original Query:/)).toBeInTheDocument();
    expect(screen.getByText('full query variant')).toBeInTheDocument();
    expect(screen.getByText(/Embedding: 10.10 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Vector Search: 20.20 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Keyword Search: 30.30 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Fusion: 5.50 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Query Expansion: 2.20 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Reranking: 15.15 ms/)).toBeInTheDocument();
    expect(screen.getByText(/Score Range:/)).toBeInTheDocument();
    expect(screen.getByText(/Mean Score:/)).toBeInTheDocument();

    await user.click(screen.getByText('Full Debug JSON'));
    expect(screen.getByText(/"total_time_ms": 123.456/)).toBeInTheDocument();
  });

  it('runs a query with metrics + debug disabled and renders the empty-result / no-metrics / no-debug states', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText(/enable debug mode/i));
    await user.click(screen.getByLabelText(/collect evaluation metrics/i));

    server.use(http.post('/api/v1/rag/query', () => HttpResponse.json(minimalResponse)));

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'minimal query');
    await user.click(screen.getByRole('button', { name: /run query/i }));

    await waitFor(() => {
      expect(screen.getByText('No results found')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'metrics' }));
    expect(screen.getByText(/No metrics available/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'debug' }));
    expect(screen.getByText(/No debug info available/)).toBeInTheDocument();
  });

  it('renders sparse metrics/debug data (null sub-fields) and adjusts Top K / Min Score / collection', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    const sliders = screen.getAllByRole('slider');
    fireEvent.change(sliders[0], { target: { value: '10' } });
    fireEvent.change(sliders[1], { target: { value: '0.25' } });
    await user.selectOptions(screen.getByRole('combobox'), 'default');

    server.use(http.post('/api/v1/rag/metrics/query', () => HttpResponse.json(sparseResponse)));

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'sparse query');
    await user.click(screen.getByRole('button', { name: /run query/i }));

    await waitFor(() => {
      expect(screen.getByText('1 Results')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'metrics' }));
    expect(screen.queryByText(/Score Distribution/)).not.toBeInTheDocument();
    expect(screen.getByText('0.0%')).toBeInTheDocument();
    expect(screen.queryByText(/Embedding:/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'debug' }));
    expect(screen.queryByText(/Query Variants:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Score Range:/)).not.toBeInTheDocument();
  });

  it('links the collection help text to the Collections admin page', async () => {
    mockCollections();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    const collectionsLink = screen.getByRole('link', { name: 'Collections admin page' });
    expect(collectionsLink).toHaveAttribute('href', '/admin/collections');
    expect(collectionsLink).toHaveAttribute('target', '_blank');
  });

  it('forwards the selected collection in the outgoing query request body', async () => {
    mockCollections([
      { name: 'default', doc_count: 5, chunk_count: 40, embedding_models: [] },
      { name: 'alt-collection', doc_count: 2, chunk_count: 10, embedding_models: [] },
    ]);
    const user = userEvent.setup();
    let capturedBody: any = null;

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox'), 'alt-collection');

    server.use(
      http.post('/api/v1/rag/metrics/query', async ({ request }) => {
        capturedBody = await request.json();
        return HttpResponse.json(minimalResponse);
      })
    );

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'scoped query');
    await user.click(screen.getByRole('button', { name: /run query/i }));

    await waitFor(() => expect(capturedBody?.collection).toBe('alt-collection'));
  });

  it('defaults results to an empty array when the field is omitted from the response', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    server.use(
      http.post('/api/v1/rag/metrics/query', () =>
        HttpResponse.json({ debug_info: null, evaluation_metrics: null })
      )
    );

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'no results field query');
    await user.click(screen.getByRole('button', { name: /run query/i }));

    await waitFor(() => {
      expect(screen.getByText('No results found')).toBeInTheDocument();
    });
  });

  it('walks every executeQuery error branch', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'error query');

    // errorData.detail branch
    server.use(
      http.post('/api/v1/rag/metrics/query', () =>
        HttpResponse.json({ detail: 'bad request' }, { status: 400 })
      )
    );
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query failed: bad request/)).toBeInTheDocument();
    });

    // errorData.error branch (detail absent)
    server.use(
      http.post('/api/v1/rag/metrics/query', () =>
        HttpResponse.json({ error: 'index unavailable' }, { status: 500 })
      )
    );
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query failed: index unavailable/)).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.post('/api/v1/rag/metrics/query', () => HttpResponse.json({}, { status: 503 })));
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query failed: HTTP 503/)).toBeInTheDocument();
    });

    // Unparseable body branch
    server.use(
      http.post('/api/v1/rag/metrics/query', () => new HttpResponse(null, { status: 500 }))
    );
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query failed: Unknown error/)).toBeInTheDocument();
    });

    // Non-Error rejection branch
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('query gremlin'));
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query failed: query gremlin/)).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('builds a comparison view across multiple history entries and clears history on confirm', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/metrics/query', () => HttpResponse.json(fullResponse)));
    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'first query');
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText('3 Results')).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText(/collect evaluation metrics/i));
    server.use(http.post('/api/v1/rag/query', () => HttpResponse.json(sparseResponse)));
    await user.clear(screen.getByPlaceholderText(/enter your search query/i));
    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'second query');
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query History \(2\)/)).toBeInTheDocument();
    });

    const compareButton = screen.getByRole('button', { name: /show comparison/i });
    expect(compareButton).not.toBeDisabled();
    await user.click(compareButton);
    expect(screen.getByText(/Select queries from history to compare/)).toBeInTheDocument();

    const historySection = screen.getByText(/Query History \(2\)/).closest('div')!.parentElement!;
    const checkboxes = within(historySection).getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(2);
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    await waitFor(() => {
      expect(screen.getByText(/Comparison \(2 selected\)/)).toBeInTheDocument();
    });
    expect(screen.getAllByText('first query').length).toBeGreaterThan(0);
    expect(screen.getAllByText('second query').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Avg Score:/).length).toBe(2);
    expect(screen.getByText('N/A')).toBeInTheDocument();

    // Toggle one back off
    await user.click(checkboxes[0]);
    await waitFor(() => {
      expect(screen.getByText(/Comparison \(1 selected\)/)).toBeInTheDocument();
    });

    // Hide comparison
    await user.click(screen.getByRole('button', { name: /hide comparison/i }));
    expect(screen.queryByText(/Comparison \(/)).not.toBeInTheDocument();

    // Clear history (confirmed)
    await user.click(screen.getByRole('button', { name: /^clear$/i }));
    await waitFor(() => {
      expect(screen.getByText('No queries yet')).toBeInTheDocument();
    });
    expect(localStorage.getItem('rag-playground-history')).toBeNull();
  });

  it('does not clear history when the confirm dialog is dismissed', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<PlaygroundPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run query/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/metrics/query', () => HttpResponse.json(fullResponse)));
    await user.type(screen.getByPlaceholderText(/enter your search query/i), 'keep me');
    await user.click(screen.getByRole('button', { name: /run query/i }));
    await waitFor(() => {
      expect(screen.getByText(/Query History \(1\)/)).toBeInTheDocument();
    });

    global.confirm = vi.fn().mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: /^clear$/i }));
    expect(screen.getByText(/Query History \(1\)/)).toBeInTheDocument();
  });
});

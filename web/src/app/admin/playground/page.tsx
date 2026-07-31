'use client';

import { useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type RAGResult = {
  doc_id: string;
  chunk_id: number;
  text: string;
  score: number;
  similarity_score: number | null;
  collection?: string | null;
  chunk_number?: number | null;
  total_chunks?: number | null;
};

type RAGDebugInfo = {
  total_time_ms: number;
  embedding_time_ms: number | null;
  vector_search_time_ms: number | null;
  keyword_search_time_ms: number | null;
  fusion_time_ms: number | null;
  query_expansion_time_ms: number | null;
  reranking_time_ms: number | null;
  original_query: string;
  query_variants: string[] | null;
  num_queries: number;
  embedding_model: string | null;
  embedding_dim: number | null;
  raw_results_count: number;
  vector_results_count: number | null;
  keyword_results_count: number | null;
  score_min: number | null;
  score_max: number | null;
  score_mean: number | null;
  after_min_score_filter: number;
  after_dedupe_filter: number;
  after_max_chunks_filter: number;
};

type RAGEvaluationMetrics = {
  retrieval_accuracy: {
    results_returned: number;
    unique_docs: number;
    score_distribution: { min: number; max: number; mean: number; median: number };
  };
  latency: {
    total_time_ms: number;
    embedding_ms: number | null;
    vector_search_ms: number | null;
    keyword_search_ms: number | null;
    fusion_ms: number | null;
    reranking_ms: number | null;
  };
  hit_rate: {
    success_rate: number;
    threshold_performance: Record<string, number>;
  };
};

type QueryResult = {
  id: string;
  timestamp: number;
  query: string;
  params: QueryParams;
  results: RAGResult[];
  debug_info: RAGDebugInfo | null;
  evaluation_metrics: RAGEvaluationMetrics | null;
};

type QueryParams = {
  top_k: number;
  min_score: number;
  collection: string;
  debug_mode: boolean;
  collect_metrics: boolean;
};

type Collection = {
  name: string;
  doc_count: number;
  chunk_count: number;
  embedding_models: string[];
};

export default function PlaygroundPage() {

  // Query parameters
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(5);
  const [minScore, setMinScore] = useState(0.0);
  const [collection, setCollection] = useState('default');
  const [debugMode, setDebugMode] = useState(true);
  const [collectMetrics, setCollectMetrics] = useState(true);

  // Collections data
  const [collections, setCollections] = useState<Collection[]>([]);
  const [loadingCollections, setLoadingCollections] = useState(true);

  // Query execution
  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Results
  const [currentResult, setCurrentResult] = useState<QueryResult | null>(null);
  const [queryHistory, setQueryHistory] = useState<QueryResult[]>([]);
  const [selectedHistoryIds, setSelectedHistoryIds] = useState<string[]>([]);

  // UI state
  const [activeTab, setActiveTab] = useState<'results' | 'metrics' | 'debug'>('results');
  const [showComparison, setShowComparison] = useState(false);

  // Load collections on mount
  useEffect(() => {
    async function loadCollections() {
      setLoadingCollections(true);
      try {
        const res = await fetch('/api/v1/rag/collections', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to load collections: HTTP ${res.status}`);
        const data = await res.json();
        setCollections(data.collections || []);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
      } finally {
        setLoadingCollections(false);
      }
    }

    void loadCollections();

    // Load query history from localStorage
    const stored = localStorage.getItem('rag-playground-history');
    if (stored) {
      try {
        const parsed = JSON.parse(stored);
        setQueryHistory(parsed);
      } catch {
        // Ignore parse errors
      }
    }
  }, []);

  // Save query history to localStorage
  useEffect(() => {
    if (queryHistory.length > 0) {
      localStorage.setItem('rag-playground-history', JSON.stringify(queryHistory.slice(-20))); // Keep last 20
    }
  }, [queryHistory]);

  async function handleExecuteQuery() {
    setExecuting(true);
    setError(null);
    setCurrentResult(null);

    try {
      const endpoint = collectMetrics ? '/api/v1/rag/metrics/query' : '/api/v1/rag/query';

      const payload: any = {
        query: query.trim(),
        top_k: topK,
        debug_mode: debugMode,
        collection: collection,
      };

      if (collectMetrics) {
        payload.collect_metrics = true;
      }

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(errorData.detail || errorData.error || `HTTP ${res.status}`);
      }

      const data = await res.json();

      const result: QueryResult = {
        id: `query-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
        timestamp: Date.now(),
        query: query.trim(),
        params: { top_k: topK, min_score: minScore, collection, debug_mode: debugMode, collect_metrics: collectMetrics },
        results: data.results || [],
        debug_info: data.debug_info || null,
        evaluation_metrics: data.evaluation_metrics || null,
      };

      setCurrentResult(result);
      setQueryHistory((prev) => [...prev, result]);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Query failed: ${msg}`);
    } finally {
      setExecuting(false);
    }
  }

  function handleClearHistory() {
    if (confirm('Clear all query history?')) {
      setQueryHistory([]);
      setSelectedHistoryIds([]);
      localStorage.removeItem('rag-playground-history');
    }
  }

  function handleToggleCompare(id: string) {
    setSelectedHistoryIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function getScoreColor(score: number): string {
    if (score >= 0.7) return '#22c55e'; // green
    if (score >= 0.4) return '#eab308'; // yellow
    return '#ef4444'; // red
  }

  if (loadingCollections) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading playground...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '1600px', margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: '2rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            RAG Playground
          </h1>
          <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
            Test queries, adjust parameters, and compare results
          </p>
          <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to Chat
          </a>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            onClick={() => setShowComparison(!showComparison)}
            disabled={queryHistory.length === 0}
            style={{
              padding: '0.5rem 1rem',
              backgroundColor: showComparison ? '#3b82f6' : 'var(--background-secondary)',
              color: showComparison ? 'white' : 'var(--foreground)',
              border: '1px solid var(--border-color)',
              borderRadius: '0.375rem',
              cursor: queryHistory.length === 0 ? 'not-allowed' : 'pointer',
              fontSize: '0.875rem',
              fontWeight: showComparison ? '600' : 'normal',
              opacity: queryHistory.length === 0 ? 0.5 : 1,
            }}
          >
            {showComparison ? 'Hide' : 'Show'} Comparison
          </button>
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={() => setError(null)} />
        </div>
      )}

      {/* Main Layout */}
      <div style={{ display: 'grid', gridTemplateColumns: showComparison ? '300px 1fr 350px' : '300px 1fr', gap: '1rem' }}>
        {/* Left Panel - Query Builder */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div
            style={{
              padding: '1.5rem',
              backgroundColor: 'var(--background-secondary)',
              borderRadius: '0.5rem',
              border: '1px solid var(--border-color)',
            }}
          >
            <h3 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '1rem' }}>
              Query Parameters
            </h3>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', fontSize: '0.875rem', fontWeight: '600', marginBottom: '0.5rem' }}>
                Query
              </label>
              <textarea
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Enter your search query..."
                rows={4}
                style={{
                  width: '100%',
                  padding: '0.5rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  backgroundColor: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: '0.875rem',
                  resize: 'vertical',
                }}
              />
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', fontSize: '0.875rem', fontWeight: '600', marginBottom: '0.5rem' }}>
                Top K: {topK}
              </label>
              <input
                type="range"
                min="1"
                max="50"
                value={topK}
                onChange={(e) => setTopK(parseInt(e.target.value))}
                style={{ width: '100%' }}
              />
              <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                Number of results to retrieve
              </p>
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', fontSize: '0.875rem', fontWeight: '600', marginBottom: '0.5rem' }}>
                Min Score: {minScore.toFixed(2)}
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={minScore}
                onChange={(e) => setMinScore(parseFloat(e.target.value))}
                style={{ width: '100%' }}
              />
              <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                Minimum relevance threshold
              </p>
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', fontSize: '0.875rem', fontWeight: '600', marginBottom: '0.5rem' }}>
                Collection
              </label>
              <select
                value={collection}
                onChange={(e) => setCollection(e.target.value)}
                style={{
                  width: '100%',
                  padding: '0.5rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  backgroundColor: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: '0.875rem',
                }}
              >
                {collections.map((coll) => (
                  <option key={coll.name} value={coll.name}>
                    {coll.name} ({coll.doc_count} docs, {coll.chunk_count} chunks)
                  </option>
                ))}
              </select>
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={debugMode}
                  onChange={(e) => setDebugMode(e.target.checked)}
                />
                <span style={{ fontSize: '0.875rem', fontWeight: '600' }}>Enable Debug Mode</span>
              </label>
              <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem', marginLeft: '1.5rem' }}>
                Collect detailed timing and performance metrics
              </p>
            </div>

            <div style={{ marginBottom: '1.5rem' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={collectMetrics}
                  onChange={(e) => setCollectMetrics(e.target.checked)}
                />
                <span style={{ fontSize: '0.875rem', fontWeight: '600' }}>Collect Evaluation Metrics</span>
              </label>
              <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem', marginLeft: '1.5rem' }}>
                Gather comprehensive retrieval accuracy and latency metrics
              </p>
            </div>

            <button
              onClick={handleExecuteQuery}
              disabled={executing || !query.trim()}
              style={{
                width: '100%',
                padding: '0.75rem',
                backgroundColor: executing || !query.trim() ? 'var(--background-secondary)' : '#3b82f6',
                color: executing || !query.trim() ? 'var(--foreground-muted)' : 'white',
                border: 'none',
                borderRadius: '0.375rem',
                cursor: executing || !query.trim() ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {executing ? 'Executing...' : 'Run Query'}
            </button>
          </div>

          {/* Query History */}
          <div
            style={{
              padding: '1.5rem',
              backgroundColor: 'var(--background-secondary)',
              borderRadius: '0.5rem',
              border: '1px solid var(--border-color)',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h3 style={{ fontSize: '1rem', fontWeight: 'bold' }}>
                Query History ({queryHistory.length})
              </h3>
              {queryHistory.length > 0 && (
                <button
                  onClick={handleClearHistory}
                  style={{
                    padding: '0.25rem 0.5rem',
                    fontSize: '0.75rem',
                    backgroundColor: 'transparent',
                    color: '#ef4444',
                    border: '1px solid #ef4444',
                    borderRadius: '0.25rem',
                    cursor: 'pointer',
                  }}
                >
                  Clear
                </button>
              )}
            </div>

            <div style={{ maxHeight: '400px', overflowY: 'auto' }}>
              {queryHistory.length === 0 ? (
                <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', textAlign: 'center' }}>
                  No queries yet
                </p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  {queryHistory.slice().reverse().map((item) => (
                    <div
                      key={item.id}
                      style={{
                        padding: '0.75rem',
                        backgroundColor: currentResult?.id === item.id ? 'var(--background)' : 'transparent',
                        border: `1px solid ${currentResult?.id === item.id ? '#3b82f6' : 'var(--border-color)'}`,
                        borderRadius: '0.375rem',
                        cursor: 'pointer',
                      }}
                      onClick={() => setCurrentResult(item)}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start', marginBottom: '0.25rem' }}>
                        <span style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                          {new Date(item.timestamp).toLocaleTimeString()}
                        </span>
                        {showComparison && (
                          <input
                            type="checkbox"
                            checked={selectedHistoryIds.includes(item.id)}
                            onChange={(e) => {
                              e.stopPropagation();
                              handleToggleCompare(item.id);
                            }}
                            style={{ cursor: 'pointer' }}
                          />
                        )}
                      </div>
                      <p style={{ fontSize: '0.875rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {item.query}
                      </p>
                      <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                        {item.results.length} results • top_k={item.params.top_k}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Center Panel - Results */}
        <div
          style={{
            padding: '1.5rem',
            backgroundColor: 'var(--background-secondary)',
            borderRadius: '0.5rem',
            border: '1px solid var(--border-color)',
            minHeight: '600px',
          }}
        >
          {!currentResult ? (
            <div style={{ textAlign: 'center', paddingTop: '3rem' }}>
              <p style={{ fontSize: '1.125rem', color: 'var(--foreground-muted)' }}>
                Enter a query and click "Run Query" to see results
              </p>
            </div>
          ) : (
            <>
              {/* Tabs */}
              <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1.5rem', borderBottom: '1px solid var(--border-color)' }}>
                {(['results', 'metrics', 'debug'] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: 'transparent',
                      color: activeTab === tab ? '#3b82f6' : 'var(--foreground-muted)',
                      border: 'none',
                      borderBottom: activeTab === tab ? '2px solid #3b82f6' : '2px solid transparent',
                      cursor: 'pointer',
                      fontSize: '0.875rem',
                      fontWeight: activeTab === tab ? '600' : 'normal',
                      textTransform: 'capitalize',
                    }}
                  >
                    {tab}
                  </button>
                ))}
              </div>

              {/* Results Tab */}
              {activeTab === 'results' && (
                <div>
                  <h3 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '1rem' }}>
                    {currentResult.results.length} Results
                  </h3>

                  {currentResult.results.length === 0 ? (
                    <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', textAlign: 'center', padding: '2rem' }}>
                      No results found
                    </p>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                      {currentResult.results.map((result, idx) => (
                        <div
                          key={`${result.doc_id}-${result.chunk_id}`}
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                            <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                              <strong>#{idx + 1}</strong> • {result.doc_id} • Chunk{' '}
                              {result.chunk_number != null
                                ? (result.total_chunks ? `${result.chunk_number} of ${result.total_chunks}` : result.chunk_number)
                                : result.chunk_id + 1}
                              {result.collection && result.collection !== 'default' && ` • ${result.collection}`}
                            </div>
                            <div
                              style={{
                                padding: '0.25rem 0.5rem',
                                backgroundColor: getScoreColor(result.score),
                                color: 'white',
                                borderRadius: '0.25rem',
                                fontSize: '0.75rem',
                                fontWeight: 'bold',
                              }}
                            >
                              {result.score.toFixed(4)}
                            </div>
                          </div>

                          <div
                            style={{
                              fontSize: '0.875rem',
                              lineHeight: '1.5',
                              whiteSpace: 'pre-wrap',
                              wordBreak: 'break-word',
                            }}
                          >
                            {result.text}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Metrics Tab */}
              {activeTab === 'metrics' && (
                <div>
                  {!currentResult.evaluation_metrics ? (
                    <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', textAlign: 'center', padding: '2rem' }}>
                      No metrics available. Enable "Collect Evaluation Metrics" to see this data.
                    </p>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                      {/* Retrieval Accuracy */}
                      <div>
                        <h4 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
                          Retrieval Accuracy
                        </h4>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '1rem' }}>
                          <div
                            style={{
                              padding: '1rem',
                              backgroundColor: 'var(--background)',
                              borderRadius: '0.375rem',
                              border: '1px solid var(--border-color)',
                            }}
                          >
                            <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase' }}>
                              Results Returned
                            </div>
                            <div style={{ fontSize: '1.5rem', fontWeight: 'bold', marginTop: '0.25rem' }}>
                              {currentResult.evaluation_metrics.retrieval_accuracy.results_returned}
                            </div>
                          </div>
                          <div
                            style={{
                              padding: '1rem',
                              backgroundColor: 'var(--background)',
                              borderRadius: '0.375rem',
                              border: '1px solid var(--border-color)',
                            }}
                          >
                            <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase' }}>
                              Unique Documents
                            </div>
                            <div style={{ fontSize: '1.5rem', fontWeight: 'bold', marginTop: '0.25rem' }}>
                              {currentResult.evaluation_metrics.retrieval_accuracy.unique_docs}
                            </div>
                          </div>
                        </div>

                        {currentResult.evaluation_metrics.retrieval_accuracy.score_distribution?.min != null && (
                          <div style={{ marginTop: '1rem', padding: '1rem', backgroundColor: 'var(--background)', borderRadius: '0.375rem', border: '1px solid var(--border-color)' }}>
                            <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.5rem' }}>
                              Score Distribution
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '0.5rem', fontSize: '0.875rem' }}>
                              <div>
                                <span style={{ color: 'var(--foreground-muted)' }}>Min:</span>{' '}
                                <strong>{currentResult.evaluation_metrics.retrieval_accuracy.score_distribution.min.toFixed(4)}</strong>
                              </div>
                              <div>
                                <span style={{ color: 'var(--foreground-muted)' }}>Max:</span>{' '}
                                <strong>{currentResult.evaluation_metrics.retrieval_accuracy.score_distribution.max.toFixed(4)}</strong>
                              </div>
                              <div>
                                <span style={{ color: 'var(--foreground-muted)' }}>Mean:</span>{' '}
                                <strong>{currentResult.evaluation_metrics.retrieval_accuracy.score_distribution.mean.toFixed(4)}</strong>
                              </div>
                              <div>
                                <span style={{ color: 'var(--foreground-muted)' }}>Median:</span>{' '}
                                <strong>{currentResult.evaluation_metrics.retrieval_accuracy.score_distribution.median.toFixed(4)}</strong>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>

                      {/* Latency */}
                      <div>
                        <h4 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
                          Latency Breakdown
                        </h4>
                        <div
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <div style={{ fontSize: '0.875rem', marginBottom: '0.5rem' }}>
                            <strong>Total Time:</strong> {currentResult.evaluation_metrics.latency.total_time_ms.toFixed(2)} ms
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', fontSize: '0.875rem' }}>
                            {currentResult.evaluation_metrics.latency.embedding_ms != null && (
                              <div>Embedding: {currentResult.evaluation_metrics.latency.embedding_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.evaluation_metrics.latency.vector_search_ms != null && (
                              <div>Vector Search: {currentResult.evaluation_metrics.latency.vector_search_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.evaluation_metrics.latency.keyword_search_ms != null && (
                              <div>Keyword Search: {currentResult.evaluation_metrics.latency.keyword_search_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.evaluation_metrics.latency.fusion_ms != null && (
                              <div>Fusion: {currentResult.evaluation_metrics.latency.fusion_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.evaluation_metrics.latency.reranking_ms != null && (
                              <div>Reranking: {currentResult.evaluation_metrics.latency.reranking_ms.toFixed(2)} ms</div>
                            )}
                          </div>
                        </div>
                      </div>

                      {/* Hit Rate */}
                      <div>
                        <h4 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
                          Hit Rate
                        </h4>
                        <div
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <div style={{ fontSize: '1.5rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                            {currentResult.evaluation_metrics.hit_rate.success_rate != null
                              ? (currentResult.evaluation_metrics.hit_rate.success_rate * 100).toFixed(1)
                              : '0.0'}%
                          </div>
                          <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                            Success Rate
                          </div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Debug Tab */}
              {activeTab === 'debug' && (
                <div>
                  {!currentResult.debug_info ? (
                    <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', textAlign: 'center', padding: '2rem' }}>
                      No debug info available. Enable "Debug Mode" to see this data.
                    </p>
                  ) : (
                    <div>
                      <h3 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '1rem' }}>
                        Debug Information
                      </h3>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                        {/* Query Info */}
                        <div
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <h4 style={{ fontSize: '0.875rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                            Query Processing
                          </h4>
                          <div style={{ fontSize: '0.875rem' }}>
                            <div><strong>Original Query:</strong> {currentResult.debug_info.original_query}</div>
                            <div><strong>Total Queries:</strong> {currentResult.debug_info.num_queries}</div>
                            {currentResult.debug_info.query_variants && currentResult.debug_info.query_variants.length > 0 && (
                              <div style={{ marginTop: '0.5rem' }}>
                                <strong>Query Variants:</strong>
                                <ul style={{ marginTop: '0.25rem', paddingLeft: '1.5rem' }}>
                                  {currentResult.debug_info.query_variants.map((variant, idx) => (
                                    <li key={idx}>{variant}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        </div>

                        {/* Timing */}
                        <div
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <h4 style={{ fontSize: '0.875rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                            Timing Breakdown
                          </h4>
                          <div style={{ fontSize: '0.875rem' }}>
                            <div><strong>Total Time:</strong> {currentResult.debug_info.total_time_ms.toFixed(2)} ms</div>
                            {currentResult.debug_info.embedding_time_ms != null && (
                              <div>Embedding: {currentResult.debug_info.embedding_time_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.debug_info.vector_search_time_ms != null && (
                              <div>Vector Search: {currentResult.debug_info.vector_search_time_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.debug_info.keyword_search_time_ms != null && (
                              <div>Keyword Search: {currentResult.debug_info.keyword_search_time_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.debug_info.fusion_time_ms != null && (
                              <div>Fusion: {currentResult.debug_info.fusion_time_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.debug_info.query_expansion_time_ms != null && (
                              <div>Query Expansion: {currentResult.debug_info.query_expansion_time_ms.toFixed(2)} ms</div>
                            )}
                            {currentResult.debug_info.reranking_time_ms != null && (
                              <div>Reranking: {currentResult.debug_info.reranking_time_ms.toFixed(2)} ms</div>
                            )}
                          </div>
                        </div>

                        {/* Results Filtering */}
                        <div
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <h4 style={{ fontSize: '0.875rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                            Results Filtering Pipeline
                          </h4>
                          <div style={{ fontSize: '0.875rem' }}>
                            <div><strong>Raw Results:</strong> {currentResult.debug_info.raw_results_count}</div>
                            <div><strong>After Min Score Filter:</strong> {currentResult.debug_info.after_min_score_filter}</div>
                            <div><strong>After Deduplication:</strong> {currentResult.debug_info.after_dedupe_filter}</div>
                            <div><strong>After Max Chunks Limit:</strong> {currentResult.debug_info.after_max_chunks_filter}</div>
                            {currentResult.debug_info.score_min != null && (
                              <>
                                <div style={{ marginTop: '0.5rem' }}>
                                  <strong>Score Range:</strong> {currentResult.debug_info.score_min.toFixed(4)} - {currentResult.debug_info.score_max?.toFixed(4)}
                                </div>
                                <div><strong>Mean Score:</strong> {currentResult.debug_info.score_mean?.toFixed(4)}</div>
                              </>
                            )}
                          </div>
                        </div>

                        {/* Full JSON */}
                        <details
                          style={{
                            padding: '1rem',
                            backgroundColor: 'var(--background)',
                            borderRadius: '0.375rem',
                            border: '1px solid var(--border-color)',
                          }}
                        >
                          <summary style={{ cursor: 'pointer', fontWeight: 'bold', fontSize: '0.875rem' }}>
                            Full Debug JSON
                          </summary>
                          <pre
                            style={{
                              marginTop: '0.5rem',
                              padding: '1rem',
                              backgroundColor: '#1e1e1e',
                              color: '#d4d4d4',
                              borderRadius: '0.25rem',
                              fontSize: '0.75rem',
                              overflow: 'auto',
                              maxHeight: '400px',
                            }}
                          >
                            {JSON.stringify(currentResult.debug_info, null, 2)}
                          </pre>
                        </details>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Right Panel - Comparison */}
        {showComparison && (
          <div
            style={{
              padding: '1.5rem',
              backgroundColor: 'var(--background-secondary)',
              borderRadius: '0.5rem',
              border: '1px solid var(--border-color)',
              minHeight: '600px',
            }}
          >
            <h3 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '1rem' }}>
              Comparison ({selectedHistoryIds.length} selected)
            </h3>

            {selectedHistoryIds.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', textAlign: 'center', padding: '2rem' }}>
                Select queries from history to compare
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                {selectedHistoryIds.map((id) => {
                  const item = queryHistory.find((q) => q.id === id)!;

                  return (
                    <div
                      key={id}
                      style={{
                        padding: '1rem',
                        backgroundColor: 'var(--background)',
                        borderRadius: '0.375rem',
                        border: '1px solid var(--border-color)',
                      }}
                    >
                      <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginBottom: '0.5rem' }}>
                        {new Date(item.timestamp).toLocaleString()}
                      </div>
                      <div style={{ fontSize: '0.875rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
                        {item.query}
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '0.5rem', fontSize: '0.75rem' }}>
                        <div>
                          <span style={{ color: 'var(--foreground-muted)' }}>Results:</span>{' '}
                          <strong>{item.results.length}</strong>
                        </div>
                        <div>
                          <span style={{ color: 'var(--foreground-muted)' }}>Top K:</span>{' '}
                          <strong>{item.params.top_k}</strong>
                        </div>
                        {item.debug_info && (
                          <>
                            <div>
                              <span style={{ color: 'var(--foreground-muted)' }}>Time:</span>{' '}
                              <strong>{item.debug_info.total_time_ms.toFixed(2)} ms</strong>
                            </div>
                            <div>
                              <span style={{ color: 'var(--foreground-muted)' }}>Avg Score:</span>{' '}
                              <strong>{item.debug_info.score_mean?.toFixed(4) || 'N/A'}</strong>
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

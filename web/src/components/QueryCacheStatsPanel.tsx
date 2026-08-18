'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from './LoadingSpinner';
import ErrorMessage from './ErrorMessage';
import { apiErrorText, errorMessage } from '../lib/apiError';

type QueryCacheStats = {
  hits: number;
  misses: number;
  hit_rate: number;
  size: number;
  enabled: boolean;
  backend: string;
  max_size: number | null;
  ttl_seconds: number | null;
  rag_enabled: boolean;
};

const statTileStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const statLabelStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--muted-foreground)',
};

const statValueStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
};

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div style={statTileStyle}>
      <span style={statLabelStyle}>{label}</span>
      <span style={statValueStyle}>{value}</span>
    </div>
  );
}

function QueryCacheStatsBody({
  stats,
  clearing,
  clearError,
  clearMessage,
  onClear,
}: {
  stats: QueryCacheStats;
  clearing: boolean;
  clearError: string | null;
  clearMessage: string | null;
  onClear: () => void;
}) {
  if (!stats.enabled) {
    return (
      <p style={{ margin: 0, fontSize: 14, color: 'var(--muted-foreground)' }}>
        Query result caching is disabled. Enable it in the{' '}
        <a href="/admin" style={{ color: 'var(--primary)' }}>
          Configuration Wizard
        </a>{' '}
        (Additional Settings &rarr; RAG, retrieval &amp; caching &rarr;{' '}
        <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
          query_cache_enabled
        </code>
        ).
      </p>
    );
  }

  const ragDisabledAndUnused =
    !stats.rag_enabled && stats.hits === 0 && stats.misses === 0 && stats.size === 0;

  if (ragDisabledAndUnused) {
    return (
      <p style={{ margin: 0, fontSize: 14, color: 'var(--muted-foreground)' }}>
        RAG is disabled globally, so the query cache isn&apos;t exercised -- hits and misses will
        stay at zero until RAG is enabled (globally or per-chat). This isn&apos;t an error; you
        don&apos;t need to change anything here.
      </p>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {!stats.rag_enabled && (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted-foreground)' }}>
          RAG is disabled globally, but these stats reflect activity from chats where RAG was
          enabled per-chat.
        </p>
      )}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
          gap: 12,
        }}
      >
        <StatTile label="Hit rate" value={`${(stats.hit_rate * 100).toFixed(1)}%`} />
        <StatTile label="Hits" value={String(stats.hits)} />
        <StatTile label="Misses" value={String(stats.misses)} />
        <StatTile
          label="Size"
          value={stats.max_size != null ? `${stats.size} / ${stats.max_size}` : String(stats.size)}
        />
        <StatTile label="Backend" value={stats.backend} />
        <StatTile label="TTL" value={stats.ttl_seconds != null ? `${stats.ttl_seconds}s` : 'n/a'} />
      </div>

      {clearError && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: 'var(--error-text)' }}>
          {clearError}
        </p>
      )}
      {clearMessage && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: 'var(--success-text)' }}>
          {clearMessage}
        </p>
      )}

      <button
        onClick={onClear}
        disabled={clearing}
        style={{
          padding: '8px 16px',
          background: clearing ? 'var(--muted-foreground)' : 'var(--error-text)',
          color: 'var(--background)',
          border: '1px solid var(--border)',
          borderRadius: 6,
          fontSize: 14,
          fontWeight: 600,
          cursor: clearing ? 'not-allowed' : 'pointer',
          alignSelf: 'flex-start',
        }}
      >
        {clearing ? 'Clearing...' : 'Clear Cache'}
      </button>
    </div>
  );
}

export default function QueryCacheStatsPanel() {
  const [stats, setStats] = useState<QueryCacheStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const [clearMessage, setClearMessage] = useState<string | null>(null);

  const loadStats = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/rag/cache/stats', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load query cache stats: HTTP ${res.status}`);
      const data = await res.json();
      setStats(data);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  async function handleClearCache() {
    if (!confirm('Clear the query result cache? Subsequent queries will be recomputed.')) {
      return;
    }
    setClearing(true);
    setClearError(null);
    setClearMessage(null);
    try {
      const res = await fetch('/api/v1/rag/cache/clear', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiErrorText(data, `HTTP ${res.status}`));
      setClearMessage(data.status || 'Query result cache cleared');
      await loadStats();
    } catch (e: unknown) {
      setClearError(errorMessage(e));
    } finally {
      setClearing(false);
    }
  }

  return (
    <section
      style={{
        padding: '1.25rem',
        border: '1px solid var(--border)',
        borderRadius: 8,
        background: 'var(--background)',
      }}
      aria-label="Query cache statistics"
    >
      <h2 style={{ margin: 0, marginBottom: '1rem', fontSize: '1.1rem' }}>Query Cache</h2>

      {loading ? (
        <LoadingSpinner label="Loading query cache stats..." />
      ) : error ? (
        <ErrorMessage title="Failed to load query cache stats" message={error} onRetry={loadStats} retrying={loading} />
      ) : (
        <QueryCacheStatsBody
          stats={stats as QueryCacheStats}
          clearing={clearing}
          clearError={clearError}
          clearMessage={clearMessage}
          onClear={handleClearCache}
        />
      )}
    </section>
  );
}

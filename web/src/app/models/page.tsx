'use client';

import { useEffect, useState } from 'react';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';
import SkeletonLoader from '../../components/SkeletonLoader';
import { useToast } from '../../contexts/ToastContext';

type Model = {
  name: string;
  size?: number;
  modified_at?: string;
};

// Rough parameter-count -> RAM guidance, matching docs/performance.md's
// Small/Medium/Large model tiers (and its per-tag table, where llama3.1:8b
// is listed at 8-16 GB, i.e. Large). Best-effort only: quantization, context
// length, and host overhead all shift the real number.
export function estimateModelResourceHint(modelName: string): string | null {
  const match = modelName.match(/(\d+(?:\.\d+)?)b(?:$|[^a-z0-9])/i);
  if (!match) return null;

  const params = parseFloat(match[1]);
  if (params < 1) {
    return 'Small model (~1-2 GB RAM)';
  }
  if (params < 8) {
    return 'Medium model (~4-8 GB RAM)';
  }
  return 'Large model (~8-16+ GB RAM) — make sure this host has enough free memory before pulling, or chat requests may fail once you select it';
}

export default function ModelsPage() {
  const toast = useToast();
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pullModelName, setPullModelName] = useState('');
  const [pulling, setPulling] = useState(false);
  const [pullError, setPullError] = useState<string | null>(null);
  const [deletingModel, setDeletingModel] = useState<string | null>(null);

  async function loadModels() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/models');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setModels(data.models || []);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadModels();
  }, []);

  async function handlePullModel(e: React.FormEvent) {
    e.preventDefault();
    if (!pullModelName.trim() || pulling) return;

    setPulling(true);
    setPullError(null);

    try {
      const res = await fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: pullModelName.trim() }),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || `HTTP ${res.status}`);
      }

      setPullModelName('');
      await loadModels();
      toast.success('Model pulled successfully');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setPullError(msg);
      toast.error(`Failed to pull model: ${msg}`);
    } finally {
      setPulling(false);
    }
  }

  async function handleDeleteModel(modelName: string) {
    if (!confirm(`Delete model "${modelName}"?`)) return;

    setDeletingModel(modelName);

    try {
      const res = await fetch(`/api/models?model=${encodeURIComponent(modelName)}`, {
        method: 'DELETE',
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || `HTTP ${res.status}`);
      }

      await loadModels();
      toast.success('Model deleted successfully');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to delete model: ${msg}`);
    } finally {
      setDeletingModel(null);
    }
  }

  return (
    <main style={{ padding: '2rem', maxWidth: 1200, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>Ollama Models</h1>
        <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Chat
        </a>
      </div>

      {/* Pull model form */}
      <section style={{ marginBottom: '2rem', padding: '1rem', border: '1px solid #ddd', borderRadius: 8 }}>
        <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Pull Model</h2>
        <form onSubmit={handlePullModel} style={{ display: 'flex', gap: 10 }}>
          <input
            type="text"
            placeholder="Model name (e.g., llama3.1:8b)"
            value={pullModelName}
            onChange={(e) => setPullModelName(e.target.value)}
            disabled={pulling}
            style={{
              flex: 1,
              padding: '10px 12px',
              border: '1px solid #ddd',
              borderRadius: 6,
              fontSize: 14,
            }}
          />
          <button
            type="submit"
            disabled={!pullModelName.trim() || pulling}
            style={{
              padding: '10px 20px',
              background: pulling ? '#ccc' : '#0066cc',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor: pulling ? 'not-allowed' : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            {pulling ? 'Pulling...' : 'Pull'}
          </button>
        </form>
        {pullModelName.trim() && estimateModelResourceHint(pullModelName.trim()) && (
          <div style={{ marginTop: 8, fontSize: 12, color: '#666' }}>
            {estimateModelResourceHint(pullModelName.trim())}
          </div>
        )}
        {pullError && (
          <div style={{ marginTop: 10 }}>
            <ErrorMessage
              title="Failed to pull model"
              message={pullError}
              onRetry={() => void handlePullModel({ preventDefault: () => {} } as React.FormEvent)}
              retrying={pulling}
            />
          </div>
        )}
      </section>

      {/* Models list */}
      <section>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h2 style={{ margin: 0, fontSize: '1.2rem' }}>Available Models</h2>
          <button
            onClick={loadModels}
            disabled={loading}
            style={{
              padding: '8px 16px',
              background: loading ? '#ccc' : '#f4f4f4',
              border: '1px solid #ddd',
              borderRadius: 6,
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 14,
            }}
          >
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>

        {error && (
          <ErrorMessage
            title="Failed to load models"
            message={error}
            onRetry={loadModels}
            retrying={loading}
          />
        )}

        {loading && !error && (
          <div style={{ display: 'grid', gap: 10 }}>
            <SkeletonLoader height={60} borderRadius={8} count={3} />
          </div>
        )}

        {!error && !loading && models.length === 0 && (
          <div style={{ padding: '2rem', textAlign: 'center', color: '#666' }}>
            No models found. Pull a model to get started.
          </div>
        )}

        {!error && !loading && models.length > 0 && (
          <div style={{ display: 'grid', gap: 10 }}>
            {models.map((model) => (
              <div
                key={model}
                style={{
                  padding: '1rem',
                  border: '1px solid #ddd',
                  borderRadius: 8,
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                }}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{model}</div>
                </div>
                <button
                  onClick={() => handleDeleteModel(model)}
                  disabled={deletingModel === model}
                  style={{
                    padding: '6px 12px',
                    background: deletingModel === model ? '#ccc' : '#ff4444',
                    color: 'white',
                    border: 'none',
                    borderRadius: 4,
                    cursor: deletingModel === model ? 'not-allowed' : 'pointer',
                    fontSize: 12,
                    fontWeight: 600,
                  }}
                >
                  {deletingModel === model ? 'Deleting...' : 'Delete'}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

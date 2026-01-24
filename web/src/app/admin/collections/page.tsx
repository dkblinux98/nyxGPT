'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type CollectionInfo = {
  name: string;
  doc_count: number;
  chunk_count: number;
  embedding_models: string[];
};

export default function CollectionsPage() {
  const router = useRouter();
  const [collections, setCollections] = useState<CollectionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingCollection, setDeletingCollection] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  async function loadCollections() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/rag/collections', {
        cache: 'no-store',
      });
      if (!res.ok) {
        throw new Error(`Failed to load collections: HTTP ${res.status}`);
      }
      const data = await res.json();
      setCollections(data.collections || []);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  async function handleDeleteCollection(collectionName: string) {
    if (collectionName === 'default') {
      alert('Cannot delete the default collection.');
      return;
    }

    setDeletingCollection(collectionName);
    setError(null);
    try {
      const res = await fetch(`/api/v1/rag/collections/${encodeURIComponent(collectionName)}`, {
        method: 'DELETE',
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(errorData.error || errorData.detail || `HTTP ${res.status}`);
      }

      // Reload collections after successful delete
      await loadCollections();
      setDeleteConfirm(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to delete collection: ${msg}`);
    } finally {
      setDeletingCollection(null);
    }
  }

  useEffect(() => {
    void loadCollections();
  }, []);

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading collections...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '1200px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            RAG Collections
          </h1>
          <p style={{ color: 'var(--foreground-muted)' }}>
            Manage vector store collections and their settings
          </p>
        </div>
        <button
          onClick={() => router.push('/')}
          style={{
            padding: '0.5rem 1rem',
            backgroundColor: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            borderRadius: '0.375rem',
            cursor: 'pointer',
            fontSize: '0.875rem',
          }}
        >
          Back to Chat
        </button>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadCollections} />
        </div>
      )}

      {collections.length === 0 ? (
        <div
          style={{
            padding: '3rem',
            textAlign: 'center',
            backgroundColor: 'var(--background-secondary)',
            borderRadius: '0.5rem',
            border: '1px solid var(--border-color)',
          }}
        >
          <p style={{ fontSize: '1.125rem', color: 'var(--foreground-muted)' }}>
            No collections found
          </p>
          <p style={{ marginTop: '0.5rem', fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
            Collections are created automatically when you ingest documents with specific embedding models.
          </p>
        </div>
      ) : (
        <div style={{ display: 'grid', gap: '1rem' }}>
          {collections.map((coll) => (
            <div
              key={coll.name}
              style={{
                padding: '1.5rem',
                backgroundColor: 'var(--background-secondary)',
                borderRadius: '0.5rem',
                border: '1px solid var(--border-color)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start', marginBottom: '1rem' }}>
                <div>
                  <h3 style={{ fontSize: '1.25rem', fontWeight: 'bold', marginBottom: '0.25rem' }}>
                    {coll.name}
                    {coll.name === 'default' && (
                      <span style={{ marginLeft: '0.5rem', fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                        (Default)
                      </span>
                    )}
                  </h3>
                </div>
                {coll.name !== 'default' && (
                  <button
                    onClick={() => setDeleteConfirm(coll.name)}
                    disabled={deletingCollection === coll.name}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: '#dc2626',
                      color: 'white',
                      border: 'none',
                      borderRadius: '0.375rem',
                      cursor: deletingCollection === coll.name ? 'not-allowed' : 'pointer',
                      fontSize: '0.875rem',
                      opacity: deletingCollection === coll.name ? 0.6 : 1,
                    }}
                  >
                    {deletingCollection === coll.name ? 'Deleting...' : 'Clear Collection'}
                  </button>
                )}
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem', marginBottom: '1rem' }}>
                <div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.25rem' }}>
                    Documents
                  </div>
                  <div style={{ fontSize: '1.5rem', fontWeight: 'bold' }}>
                    {coll.doc_count.toLocaleString()}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.25rem' }}>
                    Chunks
                  </div>
                  <div style={{ fontSize: '1.5rem', fontWeight: 'bold' }}>
                    {coll.chunk_count.toLocaleString()}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.25rem' }}>
                    Embedding Models
                  </div>
                  <div style={{ fontSize: '0.875rem', marginTop: '0.25rem' }}>
                    {coll.embedding_models.length === 0 ? (
                      <span style={{ color: 'var(--foreground-muted)' }}>None</span>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                        {coll.embedding_models.map((model) => (
                          <code
                            key={model}
                            style={{
                              fontSize: '0.75rem',
                              backgroundColor: 'var(--background)',
                              padding: '0.25rem 0.5rem',
                              borderRadius: '0.25rem',
                              display: 'inline-block',
                            }}
                          >
                            {model}
                          </code>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {deleteConfirm === coll.name && (
                <div
                  style={{
                    marginTop: '1rem',
                    padding: '1rem',
                    backgroundColor: '#fee2e2',
                    border: '1px solid #dc2626',
                    borderRadius: '0.375rem',
                  }}
                >
                  <p style={{ marginBottom: '0.75rem', fontWeight: '600', color: '#991b1b' }}>
                    Are you sure you want to clear this collection?
                  </p>
                  <p style={{ marginBottom: '1rem', fontSize: '0.875rem', color: '#7f1d1d' }}>
                    This will permanently delete all {coll.doc_count} documents and {coll.chunk_count} chunks.
                    This action cannot be undone.
                  </p>
                  <div style={{ display: 'flex', gap: '0.5rem' }}>
                    <button
                      onClick={() => handleDeleteCollection(coll.name)}
                      style={{
                        padding: '0.5rem 1rem',
                        backgroundColor: '#dc2626',
                        color: 'white',
                        border: 'none',
                        borderRadius: '0.375rem',
                        cursor: 'pointer',
                        fontSize: '0.875rem',
                        fontWeight: '600',
                      }}
                    >
                      Yes, Clear Collection
                    </button>
                    <button
                      onClick={() => setDeleteConfirm(null)}
                      style={{
                        padding: '0.5rem 1rem',
                        backgroundColor: 'white',
                        color: '#374151',
                        border: '1px solid #d1d5db',
                        borderRadius: '0.375rem',
                        cursor: 'pointer',
                        fontSize: '0.875rem',
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: '2rem', padding: '1rem', backgroundColor: 'var(--background-secondary)', borderRadius: '0.5rem', border: '1px solid var(--border-color)' }}>
        <h3 style={{ fontSize: '1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>About Collections</h3>
        <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', lineHeight: '1.5' }}>
          Collections allow you to use different embedding models for different documents. Each collection maintains
          its own vector index optimized for the embedding model and dimension you choose. Use the CLI to create
          new collections with custom models: <code style={{ backgroundColor: 'var(--background)', padding: '0.125rem 0.25rem', borderRadius: '0.25rem' }}>nyxgpt rag ingest --collection &lt;name&gt; --model &lt;model&gt;</code>
        </p>
      </div>
    </div>
  );
}

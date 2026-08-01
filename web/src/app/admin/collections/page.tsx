'use client';

import { useEffect, useRef, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type CollectionInfo = {
  name: string;
  doc_count: number;
  chunk_count: number;
  embedding_model: string | null;
  embedding_models: string[];
};

// Mirrors `COLLECTION_NAME_PATTERN` / `max_collection_name_length()` in
// src/nyxgpt/rag/vectorstore_cassandra.py: collection names become part of a
// Cassandra table identifier, so only letters, digits, and underscores are
// allowed, and the length is capped by Cassandra's 48-char identifier limit
// minus the default `rag_chunks_` table prefix. The server is authoritative
// (e.g. if `[rag] cassandra_table` is customized); this is a fast client-side
// check to avoid a round trip for obviously invalid names.
const COLLECTION_NAME_PATTERN = /^[a-zA-Z0-9_]+$/;
const COLLECTION_NAME_MAX_LENGTH = 37;

function validateCollectionName(name: string): string | null {
  if (!name) {
    return 'Collection name is required';
  }
  if (!COLLECTION_NAME_PATTERN.test(name)) {
    return 'Collection names may contain only letters, digits, and underscores (no hyphens, spaces, or other characters).';
  }
  if (name.length > COLLECTION_NAME_MAX_LENGTH) {
    return `Collection name must be at most ${COLLECTION_NAME_MAX_LENGTH} characters.`;
  }
  return null;
}

// The API's error envelope (see `http_exception_handler` in src/nyxgpt/app.py)
// nests the human-readable message under `error.message`; a plain string
// `error` (from the Next.js proxy route's own catch block, e.g. on a network
// failure) or a top-level `detail` are also handled as fallbacks.
function extractErrorMessage(errorData: unknown, fallback: string): string {
  if (errorData && typeof errorData === 'object') {
    const { error, detail } = errorData as { error?: unknown; detail?: unknown };
    if (typeof error === 'string') {
      return error;
    }
    if (error && typeof error === 'object' && typeof (error as { message?: unknown }).message === 'string') {
      return (error as { message: string }).message;
    }
    if (typeof detail === 'string') {
      return detail;
    }
  }
  return fallback;
}

export default function CollectionsPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingCollection, setDeletingCollection] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = useState('');
  const [clearingCollection, setClearingCollection] = useState<string | null>(null);
  const [clearConfirm, setClearConfirm] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newCollectionName, setNewCollectionName] = useState('');
  const [newCollectionDim, setNewCollectionDim] = useState('768');
  const [newCollectionModel, setNewCollectionModel] = useState('');
  const [viewingSettings, setViewingSettings] = useState<string | null>(null);
  const [collectionSettings, setCollectionSettings] = useState<any>(null);
  const [loadingSettings, setLoadingSettings] = useState(false);
  const [editingSettings, setEditingSettings] = useState(false);
  const [settingsEmbeddingModel, setSettingsEmbeddingModel] = useState('');
  const [settingsChunkSize, setSettingsChunkSize] = useState('');
  const [settingsChunkOverlap, setSettingsChunkOverlap] = useState('');
  const [uploadTargetCollection, setUploadTargetCollection] = useState<string | null>(null);
  const [uploadingCollection, setUploadingCollection] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);

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

  async function handleClearCollection(collectionName: string) {
    setClearingCollection(collectionName);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(
        `/api/v1/rag/collections/${encodeURIComponent(collectionName)}/clear`,
        { method: 'POST' }
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      const data = await res.json();
      // Reload collections so the card reflects the now-empty doc/chunk counts
      await loadCollections();
      setClearConfirm(null);
      setActionMessage(
        `Cleared '${collectionName}': ${data.doc_count} documents / ${data.chunk_count} chunks remain. The collection and its settings are intact.`
      );
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to clear collection: ${msg}`);
    } finally {
      setClearingCollection(null);
    }
  }

  async function handleDeleteCollection(collectionName: string) {
    setDeletingCollection(collectionName);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`/api/v1/rag/collections/${encodeURIComponent(collectionName)}`, {
        method: 'DELETE',
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      // Reload collections after successful delete
      await loadCollections();
      setDeleteConfirm(null);
      setDeleteConfirmText('');
      setActionMessage(
        `Collection '${collectionName}' and its settings have been permanently deleted.`
      );
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to delete collection: ${msg}`);
    } finally {
      setDeletingCollection(null);
    }
  }

  async function handleCreateCollection() {
    const trimmedName = newCollectionName.trim();
    const nameError = validateCollectionName(trimmedName);
    if (nameError) {
      alert(nameError);
      return;
    }

    const dim = parseInt(newCollectionDim, 10);
    if (isNaN(dim) || dim <= 0) {
      alert('Valid embedding dimension is required');
      return;
    }

    setCreating(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/rag/collections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: trimmedName,
          embedding_dim: dim,
          embedding_model: newCollectionModel.trim() || undefined,
        }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      // Reload collections and close modal
      await loadCollections();
      setShowCreateModal(false);
      setNewCollectionName('');
      setNewCollectionDim('768');
      setNewCollectionModel('');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to create collection: ${msg}`);
    } finally {
      setCreating(false);
    }
  }

  function handleUploadClick(collectionName: string) {
    setUploadTargetCollection(collectionName);
    // Ref is attached to a single shared hidden input; click() must happen
    // after the target collection state above so onChange picks it up.
    uploadInputRef.current?.click();
  }

  async function handleUploadFile(file: File) {
    // Always set by handleUploadClick before the shared file input is opened.
    const targetCollection = uploadTargetCollection!;

    setUploadingCollection(targetCollection);
    setError(null);
    setActionMessage(null);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch(
        `/api/rag/upload?collection=${encodeURIComponent(targetCollection)}`,
        { method: 'POST', body: formData }
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      const data = await res.json();
      // Reload collections so the card reflects the new doc/chunk counts
      await loadCollections();
      setActionMessage(
        `Uploaded '${data.doc_id}' into '${data.collection || targetCollection}': ${data.chunks_ingested} chunk(s) ingested (${data.status}).`
      );
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to upload document to '${targetCollection}': ${msg}`);
    } finally {
      setUploadingCollection(null);
      setUploadTargetCollection(null);
    }
  }

  async function handleViewSettings(collectionName: string) {
    setViewingSettings(collectionName);
    setLoadingSettings(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/rag/collections/${encodeURIComponent(collectionName)}/settings`, {
        cache: 'no-store',
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      const data = await res.json();
      setCollectionSettings(data);

      // Initialize edit state with current values
      setSettingsEmbeddingModel(data.settings?.embedding_model || '');
      setSettingsChunkSize(String(data.settings?.chunk_size || ''));
      setSettingsChunkOverlap(String(data.settings?.chunk_overlap || ''));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to load settings: ${msg}`);
      setViewingSettings(null);
    } finally {
      setLoadingSettings(false);
    }
  }

  async function handleSaveSettings() {
    setEditingSettings(true);
    setError(null);
    try {
      const chunkSize = settingsChunkSize.trim() ? parseInt(settingsChunkSize, 10) : null;
      const chunkOverlap = settingsChunkOverlap.trim() ? parseInt(settingsChunkOverlap, 10) : null;

      const res = await fetch(`/api/v1/rag/collections/${encodeURIComponent(viewingSettings!)}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          embedding_model: settingsEmbeddingModel.trim() || null,
          chunk_size: chunkSize,
          chunk_overlap: chunkOverlap,
        }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(extractErrorMessage(errorData, `HTTP ${res.status}`));
      }

      const data = await res.json();
      setCollectionSettings(data);
      // Refresh the collection list so the card's configured embedding model
      // reflects this edit immediately, without requiring a page reload.
      await loadCollections();
      alert('Settings saved successfully');
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`Failed to save settings: ${msg}`);
    } finally {
      setEditingSettings(false);
    }
  }

  function closeSettingsModal() {
    setViewingSettings(null);
    setCollectionSettings(null);
    setError(null);
    setSettingsEmbeddingModel('');
    setSettingsChunkSize('');
    setSettingsChunkOverlap('');
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
          <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
            Manage vector store collections and their settings
          </p>
          <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to Chat
          </a>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            onClick={() => setShowCreateModal(true)}
            style={{
              padding: '0.5rem 1rem',
              backgroundColor: '#3b82f6',
              color: 'white',
              border: 'none',
              borderRadius: '0.375rem',
              cursor: 'pointer',
              fontSize: '0.875rem',
              fontWeight: '600',
            }}
          >
            Create Collection
          </button>
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadCollections} />
        </div>
      )}

      {actionMessage && (
        <div
          style={{
            marginBottom: '1rem',
            padding: '0.75rem 1rem',
            backgroundColor: '#dcfce7',
            border: '1px solid #16a34a',
            borderRadius: '0.375rem',
            color: '#166534',
            fontSize: '0.875rem',
          }}
        >
          {actionMessage}
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
                  {coll.name === 'default' && (
                    <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                      Protected: chat and ingestion fall back to this collection, so it cannot be cleared or deleted.
                    </p>
                  )}
                </div>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  <button
                    onClick={() => handleUploadClick(coll.name)}
                    disabled={uploadingCollection === coll.name}
                    title={`Upload a document into '${coll.name}'`}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: '#16a34a',
                      color: 'white',
                      border: 'none',
                      borderRadius: '0.375rem',
                      cursor: uploadingCollection === coll.name ? 'not-allowed' : 'pointer',
                      fontSize: '0.875rem',
                      fontWeight: '600',
                      opacity: uploadingCollection === coll.name ? 0.6 : 1,
                    }}
                  >
                    {uploadingCollection === coll.name ? 'Uploading...' : 'Upload Document'}
                  </button>
                  <button
                    onClick={() => handleViewSettings(coll.name)}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: 'var(--background)',
                      color: 'var(--foreground)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      cursor: 'pointer',
                      fontSize: '0.875rem',
                    }}
                  >
                    View Settings
                  </button>
                  {coll.name !== 'default' && (
                    <button
                      onClick={() => {
                        setActionMessage(null);
                        setDeleteConfirm(null);
                        setDeleteConfirmText('');
                        setClearConfirm(coll.name);
                      }}
                      disabled={clearingCollection === coll.name}
                      title="Remove all documents and chunks from this collection; the collection and its settings remain"
                      style={{
                        padding: '0.5rem 1rem',
                        backgroundColor: '#d97706',
                        color: 'white',
                        border: 'none',
                        borderRadius: '0.375rem',
                        cursor: clearingCollection === coll.name ? 'not-allowed' : 'pointer',
                        fontSize: '0.875rem',
                        opacity: clearingCollection === coll.name ? 0.6 : 1,
                      }}
                    >
                      {clearingCollection === coll.name ? 'Clearing...' : 'Clear Collection'}
                    </button>
                  )}
                  {coll.name !== 'default' && (
                    <button
                      onClick={() => {
                        setActionMessage(null);
                        setClearConfirm(null);
                        setDeleteConfirmText('');
                        setDeleteConfirm(coll.name);
                      }}
                      disabled={deletingCollection === coll.name}
                      title="Permanently delete this collection, its documents, and its settings"
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
                      {deletingCollection === coll.name ? 'Deleting...' : 'Delete Collection'}
                    </button>
                  )}
                </div>
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
                    Embedding Model
                  </div>
                  <div style={{ fontSize: '0.875rem', marginTop: '0.25rem' }}>
                    {coll.embedding_model ? (
                      <code
                        style={{
                          fontSize: '0.75rem',
                          backgroundColor: 'var(--background)',
                          padding: '0.25rem 0.5rem',
                          borderRadius: '0.25rem',
                          display: 'inline-block',
                        }}
                      >
                        {coll.embedding_model}
                      </code>
                    ) : (
                      <span style={{ color: 'var(--foreground-muted)' }}>Not configured</span>
                    )}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.25rem' }}>
                    Observed In Chunks
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

              {clearConfirm === coll.name && (
                <div
                  style={{
                    marginTop: '1rem',
                    padding: '1rem',
                    backgroundColor: '#fef3c7',
                    border: '1px solid #d97706',
                    borderRadius: '0.375rem',
                  }}
                >
                  <p style={{ marginBottom: '0.75rem', fontWeight: '600', color: '#92400e' }}>
                    Remove all {coll.doc_count} documents and {coll.chunk_count} chunks from &apos;{coll.name}&apos;?
                  </p>
                  <p style={{ marginBottom: '1rem', fontSize: '0.875rem', color: '#78350f' }}>
                    The collection and its settings (embedding model, chunk size/overlap) remain, so you can
                    start ingesting into it again right away. This action cannot be undone.
                  </p>
                  <div style={{ display: 'flex', gap: '0.5rem' }}>
                    <button
                      onClick={() => handleClearCollection(coll.name)}
                      disabled={clearingCollection === coll.name}
                      style={{
                        padding: '0.5rem 1rem',
                        backgroundColor: '#d97706',
                        color: 'white',
                        border: 'none',
                        borderRadius: '0.375rem',
                        cursor: clearingCollection === coll.name ? 'not-allowed' : 'pointer',
                        fontSize: '0.875rem',
                        fontWeight: '600',
                        opacity: clearingCollection === coll.name ? 0.6 : 1,
                      }}
                    >
                      {clearingCollection === coll.name ? 'Clearing...' : 'Yes, Clear Collection'}
                    </button>
                    <button
                      onClick={() => setClearConfirm(null)}
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
                    Permanently delete &apos;{coll.name}&apos;?
                  </p>
                  <p style={{ marginBottom: '0.75rem', fontSize: '0.875rem', color: '#7f1d1d' }}>
                    This removes all {coll.doc_count} documents and {coll.chunk_count} chunks, the
                    collection&apos;s settings, and its backing storage. It will disappear from every
                    collection picker (this screen, the playground, and chat scoping). This action
                    cannot be undone.
                  </p>
                  <label
                    htmlFor={`delete-confirm-${coll.name}`}
                    style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.875rem', fontWeight: '600', color: '#7f1d1d' }}
                  >
                    Type <code>{coll.name}</code> to confirm
                  </label>
                  <input
                    id={`delete-confirm-${coll.name}`}
                    type="text"
                    value={deleteConfirmText}
                    onChange={(e) => setDeleteConfirmText(e.target.value)}
                    placeholder={coll.name}
                    disabled={deletingCollection === coll.name}
                    style={{
                      width: '100%',
                      padding: '0.5rem',
                      marginBottom: '1rem',
                      border: '1px solid #dc2626',
                      borderRadius: '0.375rem',
                      fontSize: '0.875rem',
                      backgroundColor: 'var(--background)',
                      color: 'var(--foreground)',
                    }}
                  />
                  <div style={{ display: 'flex', gap: '0.5rem' }}>
                    <button
                      onClick={() => handleDeleteCollection(coll.name)}
                      disabled={deletingCollection === coll.name || deleteConfirmText !== coll.name}
                      style={{
                        padding: '0.5rem 1rem',
                        backgroundColor: '#dc2626',
                        color: 'white',
                        border: 'none',
                        borderRadius: '0.375rem',
                        cursor:
                          deletingCollection === coll.name || deleteConfirmText !== coll.name
                            ? 'not-allowed'
                            : 'pointer',
                        fontSize: '0.875rem',
                        fontWeight: '600',
                        opacity: deletingCollection === coll.name || deleteConfirmText !== coll.name ? 0.6 : 1,
                      }}
                    >
                      {deletingCollection === coll.name ? 'Deleting...' : 'Yes, Delete Permanently'}
                    </button>
                    <button
                      onClick={() => {
                        setDeleteConfirm(null);
                        setDeleteConfirmText('');
                      }}
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
          its own vector index optimized for the embedding model and dimension you choose.
        </p>
      </div>

      {/* Hidden file input shared by every collection's "Upload Document" button;
          the target collection is tracked in `uploadTargetCollection` state set
          by handleUploadClick just before this input is clicked. */}
      <input
        ref={uploadInputRef}
        type="file"
        accept=".txt,.md,.json,.pdf,.docx,.pptx,.epub,.html,.htm"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void handleUploadFile(file);
          else setUploadTargetCollection(null);
          e.target.value = '';
        }}
        style={{ display: 'none' }}
      />

      {/* Create Collection Modal */}
      {showCreateModal && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
          onClick={() => !creating && setShowCreateModal(false)}
        >
          <div
            style={{
              backgroundColor: 'var(--background)',
              padding: '2rem',
              borderRadius: '0.5rem',
              maxWidth: '500px',
              width: '90%',
              border: '1px solid var(--border-color)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 style={{ fontSize: '1.5rem', fontWeight: 'bold', marginBottom: '1rem' }}>
              Create Collection
            </h2>

            <div style={{ marginBottom: '1rem' }}>
              <label
                htmlFor="collection-name"
                style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.875rem', fontWeight: '600' }}
              >
                Collection Name *
              </label>
              <input
                id="collection-name"
                type="text"
                value={newCollectionName}
                onChange={(e) => setNewCollectionName(e.target.value)}
                disabled={creating}
                placeholder="my_collection"
                style={{
                  width: '100%',
                  padding: '0.5rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  backgroundColor: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: '0.875rem',
                }}
              />
              <p style={{ marginTop: '0.25rem', fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                Letters, numbers, and underscores only (no hyphens or spaces), max {COLLECTION_NAME_MAX_LENGTH} characters
              </p>
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label
                htmlFor="embedding-dim"
                style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.875rem', fontWeight: '600' }}
              >
                Embedding Dimension *
              </label>
              <input
                id="embedding-dim"
                type="number"
                value={newCollectionDim}
                onChange={(e) => setNewCollectionDim(e.target.value)}
                disabled={creating}
                placeholder="768"
                style={{
                  width: '100%',
                  padding: '0.5rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  backgroundColor: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: '0.875rem',
                }}
              />
              <p style={{ marginTop: '0.25rem', fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                Must match your embedding model (e.g., 768, 1536, 3072)
              </p>
            </div>

            <div style={{ marginBottom: '1.5rem' }}>
              <label
                htmlFor="embedding-model"
                style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.875rem', fontWeight: '600' }}
              >
                Embedding Model (Optional)
              </label>
              <input
                id="embedding-model"
                type="text"
                value={newCollectionModel}
                onChange={(e) => setNewCollectionModel(e.target.value)}
                disabled={creating}
                placeholder="nomic-embed-text"
                style={{
                  width: '100%',
                  padding: '0.5rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  backgroundColor: 'var(--background)',
                  color: 'var(--foreground)',
                  fontSize: '0.875rem',
                }}
              />
              <p style={{ marginTop: '0.25rem', fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                If set, this is saved as the collection&apos;s configured embedding model
                and used by default when ingesting documents. Leave blank to configure it
                later via View Settings.
              </p>
            </div>

            <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
              <button
                onClick={() => setShowCreateModal(false)}
                disabled={creating}
                style={{
                  padding: '0.5rem 1rem',
                  backgroundColor: 'var(--background-secondary)',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  cursor: creating ? 'not-allowed' : 'pointer',
                  fontSize: '0.875rem',
                  opacity: creating ? 0.6 : 1,
                }}
              >
                Cancel
              </button>
              <button
                onClick={handleCreateCollection}
                disabled={creating}
                style={{
                  padding: '0.5rem 1rem',
                  backgroundColor: '#3b82f6',
                  color: 'white',
                  border: 'none',
                  borderRadius: '0.375rem',
                  cursor: creating ? 'not-allowed' : 'pointer',
                  fontSize: '0.875rem',
                  fontWeight: '600',
                  opacity: creating ? 0.6 : 1,
                }}
              >
                {creating ? 'Creating...' : 'Create Collection'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Settings Modal */}
      {viewingSettings && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
          onClick={closeSettingsModal}
        >
          <div
            style={{
              backgroundColor: 'var(--background)',
              padding: '2rem',
              borderRadius: '0.5rem',
              maxWidth: '600px',
              width: '90%',
              border: '1px solid var(--border-color)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 style={{ fontSize: '1.5rem', fontWeight: 'bold', marginBottom: '1rem' }}>
              Collection Settings: {viewingSettings}
            </h2>

            {loadingSettings ? (
              <div style={{ padding: '2rem', textAlign: 'center' }}>
                <LoadingSpinner size="medium" />
                <p style={{ marginTop: '1rem', color: 'var(--foreground-muted)' }}>Loading settings...</p>
              </div>
            ) : (
              <div>
                <div style={{ marginBottom: '1rem' }}>
                  <label style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.5rem', display: 'block' }}>
                    Embedding Model
                  </label>
                  <input
                    type="text"
                    value={settingsEmbeddingModel}
                    onChange={(e) => setSettingsEmbeddingModel(e.target.value)}
                    placeholder="e.g., nomic-embed-text"
                    style={{
                      width: '100%',
                      padding: '0.75rem',
                      backgroundColor: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      fontSize: '0.875rem',
                      color: 'var(--foreground)',
                    }}
                  />
                  <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                    Optional: Preferred embedding model for this collection
                  </p>
                </div>

                <div style={{ marginBottom: '1rem' }}>
                  <label style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.5rem', display: 'block' }}>
                    Chunk Size
                  </label>
                  <input
                    type="number"
                    value={settingsChunkSize}
                    onChange={(e) => setSettingsChunkSize(e.target.value)}
                    placeholder="e.g., 800"
                    style={{
                      width: '100%',
                      padding: '0.75rem',
                      backgroundColor: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      fontSize: '0.875rem',
                      color: 'var(--foreground)',
                    }}
                  />
                  <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                    Default chunk size for new documents (characters)
                  </p>
                </div>

                <div style={{ marginBottom: '1.5rem' }}>
                  <label style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', textTransform: 'uppercase', marginBottom: '0.5rem', display: 'block' }}>
                    Chunk Overlap
                  </label>
                  <input
                    type="number"
                    value={settingsChunkOverlap}
                    onChange={(e) => setSettingsChunkOverlap(e.target.value)}
                    placeholder="e.g., 100"
                    style={{
                      width: '100%',
                      padding: '0.75rem',
                      backgroundColor: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      fontSize: '0.875rem',
                      color: 'var(--foreground)',
                    }}
                  />
                  <p style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', marginTop: '0.25rem' }}>
                    Chunk overlap for new documents (characters)
                  </p>
                </div>

                <div style={{ padding: '1rem', backgroundColor: '#dbeafe', border: '1px solid #3b82f6', borderRadius: '0.375rem', marginBottom: '1rem' }}>
                  <p style={{ fontSize: '0.875rem', color: '#1e40af' }}>
                    <strong>Note:</strong> These settings are stored per-collection and will be used as defaults when ingesting new documents.
                  </p>
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem' }}>
                  <button
                    onClick={closeSettingsModal}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      cursor: 'pointer',
                      fontSize: '0.875rem',
                    }}
                  >
                    Close
                  </button>
                  <button
                    onClick={handleSaveSettings}
                    disabled={editingSettings}
                    style={{
                      padding: '0.5rem 1rem',
                      backgroundColor: editingSettings ? 'var(--background-secondary)' : 'var(--primary-color, #3b82f6)',
                      color: editingSettings ? 'var(--foreground-muted)' : 'white',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                      cursor: editingSettings ? 'not-allowed' : 'pointer',
                      fontSize: '0.875rem',
                    }}
                  >
                    {editingSettings ? 'Saving...' : 'Save Settings'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

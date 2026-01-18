

# Retrieval‑Augmented Generation (RAG)

myGPT supports optional Retrieval‑Augmented Generation using **Apache Cassandra 5.0** as a vector database.

RAG allows myGPT to:
- ingest documents
- store embeddings persistently
- retrieve relevant context at query time
- inject that context into chat prompts

---

## Architecture overview

- **Embeddings** are generated locally via Ollama
- **Vectors** are stored in Cassandra using native vector search (SAI indexes)
- **Retrieval** happens before chat prompting
- **Injection** is controlled by configuration

---

## Requirements

- Docker (for Cassandra)
- Ollama running locally
- An embedding model available in Ollama (e.g. `nomic-embed-text`)

---

## Running Cassandra (Docker)

### Start Cassandra

```bash
docker run -d \
  --name mygpt-cassandra \
  -p 9042:9042 \
  cassandra:5.0
```

Wait several minutes for Cassandra to finish starting.

### Optional: persistent storage

To persist data across container restarts:

```bash
docker volume create mygpt_cassandra_data

docker rm -f mygpt-cassandra

docker run -d \
  --name mygpt-cassandra \
  -p 9042:9042 \
  -v mygpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```

---

## Cassandra schema

Create the keyspace and table:

```sql
CREATE KEYSPACE IF NOT EXISTS mygpt
WITH replication = {
  'class': 'SimpleStrategy',
  'replication_factor': 1
};

USE mygpt;

CREATE TABLE IF NOT EXISTS rag_chunks (
  doc_id text,
  chunk_id int,
  embedding vector<float, 768>,
  text text,
  PRIMARY KEY (doc_id, chunk_id)
);

CREATE CUSTOM INDEX IF NOT EXISTS rag_embedding_idx
ON rag_chunks (embedding)
USING 'org.apache.cassandra.index.sai.StorageAttachedIndex';
```

> ⚠️ The `embedding` dimension **must match** the configured embedding model.

---

## Configuration

Relevant `[rag]` configuration:

```ini
[rag]
# Global RAG setting (can be overridden per-session)
enable_chat_context = false
embedding_model = nomic-embed-text
embedding_dim = 768
chat_top_k = 3
chat_context_max_chars = 4000
cassandra_hosts = 127.0.0.1
cassandra_port = 9042
cassandra_keyspace = mygpt
cassandra_table = rag_chunks
```

---

## CLI commands

### Ingest a document

```bash
mygpt rag ingest README.md --doc-id readme-v1
```

### List ingested documents

```bash
mygpt rag list
```

### Delete a document

```bash
mygpt rag delete readme-v1
```

### Wipe all RAG data (development only)

```bash
mygpt rag wipe --yes-really
```

---

## API Endpoints

### Per-Session RAG Control

#### `GET /api/v1/sessions/{name}/metadata`

Get session metadata including RAG status.

**Response:**
```json
{
  "created_at": "2026-01-06T12:00:00",
  "updated_at": "2026-01-06T12:30:00",
  "pinned": false,
  "tags": [],
  "model": "qwen2.5:0.5b",
  "rag_enabled": false
}
```

#### `POST /api/v1/sessions/{name}/rag/enable`

Enable RAG for a specific session.

**Response:**
```json
{
  "session": "my-session",
  "rag_enabled": true
}
```

#### `POST /api/v1/sessions/{name}/rag/disable`

Disable RAG for a specific session.

**Response:**
```json
{
  "session": "my-session",
  "rag_enabled": false
}
```

### Document Upload

#### `POST /api/v1/rag/upload`

Upload and ingest a document for RAG.

**Supported file types:** `.txt`, `.md` (with frontmatter parsing), `.json`, `.pdf`

**Request:**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/upload \
  -F "file=@document.md" \
  -F "doc_id=my-doc"  # optional, defaults to filename
```

**Response:**
```json
{
  "doc_id": "document.md",
  "chunks_ingested": 5
}
```

**Markdown Files:**
- Extracts YAML frontmatter (title, author, tags, etc.)
- Preserves headers hierarchy
- Handles code blocks
- Falls back to plain text if parsing libraries unavailable

**Error (unsupported file type):**
```json
{
  "detail": "File type .exe not supported. Allowed: {'.txt', '.md', '.json', '.pdf'}"
}
```

---

## RAG‑assisted chat

When enabled globally or per-session, retrieved context is injected automatically during chat:

```bash
mygpt chat "What does Cassandra support for vector search?"
```

The retrieved context is prepended as a system message.

**Priority chain:** Explicit API `rag_enabled` parameter > Session metadata > Global config `enable_chat_context`

---

## Multiple Embedding Models

**New in v1.0:** myGPT now supports using multiple embedding models simultaneously via collections.

### Why Multiple Models?

Different embedding models have different strengths:
- **Small/fast models** (e.g., `all-minilm:latest`, 384 dimensions): Quick retrieval, lower memory
- **High-quality models** (e.g., `nomic-embed-text`, 768 dimensions): Better semantic understanding
- **Specialized models**: Multilingual, code-specific, domain-adapted

### Collections

Each collection:
- Uses a separate Cassandra table
- Supports a specific embedding model and dimension
- Maintains its own vector index
- Can be queried independently

### Using Multiple Models

#### 1. Ingest documents with different models

```python
from mygpt.rag.rag import ingest_document

# Default collection (nomic-embed-text, 768d)
n = ingest_document(
    doc_id="doc1",
    text=content,
    ensure_schema=True
)

# Fast model collection (all-minilm, 384d)
n = ingest_document(
    doc_id="doc2",
    text=content,
    collection="all-minilm",
    embedding_model="all-minilm:latest",
    embedding_dim=384,
    ensure_schema=True  # First time for this collection
)

# High-quality collection (mxbai-embed-large, 1024d)
n = ingest_document(
    doc_id="doc3",
    text=content,
    collection="mxbai",
    embedding_model="mxbai-embed-large:latest",
    embedding_dim=1024,
    ensure_schema=True  # First time for this collection
)
```

#### 2. Query specific collections

```python
from mygpt.rag.rag import retrieve_context

# Query default collection
results = retrieve_context("test query")

# Query fast model collection
results = retrieve_context(
    "test query",
    collection="all-minilm",
    embedding_model="all-minilm:latest",
    embedding_dim=384
)

# Query high-quality collection
results = retrieve_context(
    "test query",
    collection="mxbai",
    embedding_model="mxbai-embed-large:latest",
    embedding_dim=1024
)
```

#### 3. Compare model performance

```python
from mygpt.rag.model_compare import compare_models, print_comparison_table

models = [
    ("nomic-embed-text", 768, "default"),
    ("all-minilm:latest", 384, "all-minilm"),
    ("mxbai-embed-large:latest", 1024, "mxbai"),
]

test_texts = ["sample text 1", "sample text 2"]
test_queries = ["test query 1", "test query 2"]

results = compare_models(models, test_texts, test_queries)
print_comparison_table(results)
```

**Example output:**

```
================================================================================
Model                      Dim      Embed (ms)      Query (ms)
================================================================================
nomic-embed-text           768      145.23          67.89
all-minilm:latest          384      89.45           42.11
mxbai-embed-large:latest   1024     312.67          98.34
================================================================================

Fastest embedding: all-minilm:latest (89.45 ms)
Fastest query: all-minilm:latest (42.11 ms)
```

### Model Switching Without Re-indexing

You can switch between models **without re-indexing** by using collections:

1. Ingest the same documents into multiple collections (one per model)
2. Query the collection that best fits your use case
3. Switch collections at runtime based on speed/quality trade-offs

**Benefits:**
- No downtime during model transitions
- A/B testing of different models
- Dynamic selection based on query type

**Trade-off:** Higher disk usage (one copy per collection)

### List Available Collections

```python
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

store = CassandraVectorStore()
collections = store.list_collections()
print(f"Available collections: {collections}")
store.close()
```

### Collection Best Practices

1. **Naming:** Use descriptive names: `all-minilm`, `nomic768`, `mxbai1024`
2. **Schema:** Call `ensure_schema=True` once per collection
3. **Consistency:** Always use the same model/dimension for a collection
4. **Cleanup:** Delete unused collections to free disk space

---

## Re‑ingesting data

If you change:
- embedding model
- embedding dimension
- chunking parameters

For **single-model setups**, you **must delete existing chunks and re‑ingest** documents.

For **multi-model setups**, use collections to avoid re-ingestion (see "Multiple Embedding Models" above).

---

## Notes

- RAG is optional and can be disabled at any time.
- Cassandra is used only for vector storage; no full‑text search is required.
- RAG latency depends on embedding generation and vector search performance.
- Multiple embedding models are supported via collections (separate tables per model).


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

## Re‑ingesting data

If you change:
- embedding model
- embedding dimension
- chunking parameters

You **must delete existing chunks and re‑ingest** documents.

---

## Notes

- RAG is optional and can be disabled at any time.
- Cassandra is used only for vector storage; no full‑text search is required.
- RAG latency depends on embedding generation and vector search performance.
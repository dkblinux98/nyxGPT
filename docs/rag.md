

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
enabled = true
embedding_model = nomic-embed-text
embedding_dim = 768
chat_top_k = 3
chat_context_max_chars = 4000
cassandra_host = 127.0.0.1
cassandra_port = 9042
keyspace = mygpt
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

## RAG‑assisted chat

When enabled, retrieved context is injected automatically during chat:

```bash
mygpt chat "What does Cassandra support for vector search?"
```

The retrieved context is prepended as a system message.

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


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

## RAG Evaluation Metrics

**New in v1.0:** myGPT now provides comprehensive evaluation metrics to assess RAG quality.

### Overview

The metrics endpoint `/api/v1/rag/metrics/query` extends the standard RAG query with detailed performance analytics:

- **Retrieval Accuracy** - Hit rate, unique documents, score distribution
- **Latency Tracking** - Per-stage timing breakdowns
- **Hit Rate Analysis** - Success rates, threshold performance

### API Endpoint

#### `POST /api/v1/rag/metrics/query`

Query RAG with comprehensive evaluation metrics.

**Request:**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/metrics/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is vector search?",
    "top_k": 5,
    "collect_metrics": true
  }'
```

**Response:**
```json
{
  "results": [
    {
      "doc_id": "doc1",
      "chunk_id": 0,
      "text": "Vector search is...",
      "score": 0.95
    }
  ],
  "debug_info": {
    "total_time_ms": 125.5,
    "embedding_time_ms": 45.2,
    "vector_search_time_ms": 65.3,
    "filtering_time_ms": 5.0,
    "score_min": 0.85,
    "score_max": 0.95,
    "score_mean": 0.90
  },
  "evaluation_metrics": {
    "retrieval_accuracy": {
      "results_returned": 5,
      "query_success": true,
      "unique_docs_retrieved": 3,
      "total_chunks_retrieved": 5,
      "score_distribution": {
        "p50": 0.90,
        "p75": 0.93,
        "p95": 0.95,
        "p99": 0.95
      }
    },
    "latency": {
      "total_time_ms": 125.5,
      "stage_timings": {
        "query_expansion": 0.0,
        "embedding": 45.2,
        "vector_search": 65.3,
        "filtering": 5.0,
        "composition": 10.0
      },
      "percentiles": null
    },
    "hit_rate": {
      "query_success_rate": 1.0,
      "total_queries": 1,
      "successful_queries": 1,
      "failed_queries": 0,
      "avg_top_score": 0.95,
      "score_above_threshold_rate": 1.0
    },
    "query_id": "550e8400-e29b-41d4-a716-446655440000",
    "timestamp": 1704067200.0
  }
}
```

### Metrics Explained

#### Retrieval Accuracy Metrics

- **results_returned**: Total number of chunks returned
- **query_success**: Boolean indicating if any results were found
- **unique_docs_retrieved**: Number of unique documents in results
- **total_chunks_retrieved**: Total chunks across all documents
- **score_distribution**: Percentiles (p50/p75/p95/p99) of similarity scores

#### Latency Metrics

- **total_time_ms**: End-to-end query latency
- **stage_timings**: Per-stage breakdown (query expansion, embedding, vector search, filtering, composition)
- **percentiles**: Historical latency percentiles (computed from aggregated data, null for single queries)

#### Hit Rate Metrics

- **query_success_rate**: Percentage of queries returning results (0.0 or 1.0 for single queries)
- **total_queries**: Number of queries in this batch (always 1 for single queries)
- **successful_queries**: Queries that returned results
- **failed_queries**: Queries that returned no results
- **avg_top_score**: Score of the top-ranked result
- **score_above_threshold_rate**: Percentage of results above the configured min_score threshold

### Use Cases

**Performance Monitoring:**
```bash
# Track latency over time
for i in {1..10}; do
  curl -X POST http://127.0.0.1:8000/api/v1/rag/metrics/query \
    -H "Content-Type: application/json" \
    -d "{\"query\": \"test query $i\", \"collect_metrics\": true}" \
    | jq '.evaluation_metrics.latency.total_time_ms'
done
```

**Quality Assessment:**
```bash
# Analyze score distributions
curl -X POST http://127.0.0.1:8000/api/v1/rag/metrics/query \
  -H "Content-Type: application/json" \
  -d '{"query": "machine learning", "collect_metrics": true}' \
  | jq '.evaluation_metrics.retrieval_accuracy.score_distribution'
```

**Hit Rate Analysis:**
```bash
# Check query success rates
curl -X POST http://127.0.0.1:8000/api/v1/rag/metrics/query \
  -H "Content-Type: application/json" \
  -d '{"query": "nonexistent topic", "collect_metrics": true}' \
  | jq '.evaluation_metrics.hit_rate'
```

### Configuration

Metrics collection is always enabled for the metrics endpoint. For standard queries with debug info, use:

```ini
[rag]
debug_mode = true  # Enables debug_info in standard /rag/query endpoint
```

---

## Notes

- RAG is optional and can be disabled at any time.
- Cassandra is used only for vector storage; no full‑text search is required.
- RAG latency depends on embedding generation and vector search performance.
- Multiple embedding models are supported via collections (separate tables per model).
- Evaluation metrics provide detailed quality and performance insights for RAG queries.

---

## Hybrid Search (Keyword + Vector)

**New in v1.0:** myGPT now supports hybrid search that combines BM25 keyword search with vector similarity search for improved retrieval quality.

### Why Hybrid Search?

Pure vector search excels at semantic understanding but can miss exact keyword matches. Pure keyword search (BM25) excels at exact term matching but lacks semantic understanding. Hybrid search combines both:

1. **Keyword Precision**: BM25 finds documents with exact term matches that vectors might miss
2. **Semantic Understanding**: Vector search captures meaning and context
3. **Complementary Strengths**: Documents matching both signals rank higher
4. **Better Recall**: Retrieves relevant docs that only match one signal

### How It Works

1. **Vector Search**: Generate query embedding and search Cassandra vector index
2. **Keyword Search (BM25)**: Tokenize query and rank documents by term frequency/IDF
3. **Fusion**: Merge rankings using Reciprocal Rank Fusion (RRF) or weighted fusion
4. **Filtering**: Apply min_score and max_chunks limits
5. **Return**: Top-k fused results

### Configuration

Add to `~/.myGPT/config.ini`:

```ini
[rag]
# Enable hybrid search (default: true)
enable_hybrid_search = true

# BM25 parameters
bm25_k1 = 1.5          # Term frequency saturation (typical: 1.2-2.0)
bm25_b = 0.75          # Length normalization (typical: 0.0-1.0)

# Reciprocal Rank Fusion parameter
rrf_k = 60             # Rank weighting constant (typical: 60)

# Alternative: weighted fusion (overrides RRF when set)
# hybrid_alpha = 0.5   # Weight: 1.0=vector only, 0.0=keyword only
```

### BM25 Parameters

**k1 (term frequency saturation)**:
- Controls how much additional occurrences of a term increase the score
- Low k1 (1.2): Diminishing returns after a few occurrences
- High k1 (2.0): More weight to term frequency
- Default: 1.5

**b (length normalization)**:
- Controls how much document length affects scoring
- b=0.0: No length normalization (longer docs not penalized)
- b=1.0: Full length normalization (longer docs penalized more)
- Default: 0.75

### Fusion Methods

**Reciprocal Rank Fusion (RRF)** - Default:
- Combines rankings without needing score normalization
- Formula: `score = Σ(1 / (k + rank))` for each system
- Robust to score scale differences between vector and keyword search
- No tuning required (k=60 is standard)

**Weighted Fusion** - Alternative:
- Directly combines normalized scores: `score = α * vector + (1-α) * keyword`
- Requires setting `hybrid_alpha` in config
- α=1.0: Vector search only (semantic)
- α=0.5: Equal weight (balanced)
- α=0.0: Keyword search only (exact matching)

### Usage

Hybrid search is enabled by default. No code changes required.

**Python API:**

```python
from mygpt.rag.rag import retrieve_context

# Hybrid search automatically enabled (if configured)
results = retrieve_context("Cassandra vector search", top_k=5)

# With debug info to see fusion method
results, debug_info = retrieve_context(
    "Cassandra vector search",
    top_k=5,
    debug_mode=True
)

print(f"Fusion method: {debug_info.fusion_method}")
print(f"Vector results: {debug_info.vector_results_count}")
print(f"Keyword results: {debug_info.keyword_results_count}")
```

**CLI:**

```bash
# RAG query uses hybrid search automatically
mygpt rag query "Cassandra vector search"

# Debug mode shows fusion details
mygpt rag query "Cassandra vector search" --debug
```

**REST API:**

```bash
# Query with hybrid search (default)
curl -X POST http://127.0.0.1:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Cassandra vector search", "top_k": 5, "debug_mode": true}'

# Response includes hybrid search metrics
{
  "results": [...],
  "debug_info": {
    "hybrid_enabled": true,
    "fusion_method": "reciprocal_rank_fusion",
    "vector_results_count": 5,
    "keyword_results_count": 5,
    "keyword_search_time_ms": 12.3,
    "fusion_time_ms": 0.5,
    ...
  }
}
```

### Disabling Hybrid Search

To revert to vector-only search:

```ini
[rag]
enable_hybrid_search = false
```

### Performance Considerations

**Memory:**
- BM25 index is built in-memory from Cassandra documents
- Memory usage: O(N * V) where N=docs, V=vocabulary size
- For large corpora (>10k docs), consider caching or disk-based indices

**Latency:**
- Adds keyword search + fusion time (typically 10-50ms)
- Vector search remains the dominant latency factor
- BM25 index is rebuilt per query (no caching yet)

**Optimization Tips:**
1. Reduce `chat_top_k` if querying large corpora
2. Use `min_score` to filter low-quality matches early
3. Consider vector-only mode for latency-critical applications
4. Profile with `debug_mode=True` to identify bottlenecks

### When to Use Hybrid vs. Vector-Only

**Use Hybrid Search (default) when:**
- Queries contain specific terminology or product names
- Exact keyword matches are important
- Documents have distinct vocabulary (technical, legal, medical)
- You want maximum recall (find all relevant docs)

**Use Vector-Only when:**
- Queries are conversational/semantic
- Low latency is critical (<100ms)
- Corpus has consistent vocabulary
- Keyword ambiguity is high (same word, different meanings)

### Examples

**Example 1: Technical Terminology**

Query: "Cassandra SAI index"

- Vector search: Finds docs about "database indexing", "search optimization"
- Keyword search: Finds docs containing exactly "SAI" and "Cassandra"
- Hybrid: Ranks highest docs with both "Cassandra SAI" terms AND semantic relevance

**Example 2: Product Names**

Query: "iPhone 15 battery life"

- Vector search: Finds docs about "smartphone batteries", "mobile power"
- Keyword search: Finds docs containing exactly "iPhone 15"
- Hybrid: Correctly finds iPhone 15 battery docs, not generic phone articles

**Example 3: Acronyms**

Query: "RAG implementation"

- Vector search: Finds docs about "retrieval augmented generation", "AI systems"
- Keyword search: Finds docs containing "RAG" (exact acronym match)
- Hybrid: Finds both explicit "RAG" mentions and semantic retrieval discussions

### Troubleshooting

**Problem: Hybrid search not improving results**

Check:
- Is `enable_hybrid_search = true` in config?
- Run with `debug_mode=True` to see `keyword_results_count`
- If keyword_results_count=0, BM25 found no matches (normal for very semantic queries)

**Problem: Keyword results dominating**

- Increase `hybrid_alpha` toward 1.0 (more weight to vectors)
- Or adjust `bm25_k1` and `bm25_b` parameters

**Problem: High latency**

- Check `keyword_search_time_ms` and `fusion_time_ms` in debug output
- If `keyword_search_time_ms` is high, corpus may be large
- Consider `enable_hybrid_search = false` for latency-critical use

**Problem: Unexpected rankings**

- Use `debug_mode=True` to inspect scores from each system
- Verify both vector and keyword searches return expected results independently
- Try adjusting fusion method (RRF vs. weighted)

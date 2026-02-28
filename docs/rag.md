

# Retrieval‑Augmented Generation (RAG)

nyxGPT supports optional Retrieval‑Augmented Generation using **Apache Cassandra 5.0** as a vector database.

RAG allows nyxGPT to:
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
  --name nyxgpt-cassandra \
  -p 9042:9042 \
  cassandra:5.0
```

Wait several minutes for Cassandra to finish starting.

### Optional: persistent storage

To persist data across container restarts:

```bash
docker volume create nyxgpt_cassandra_data

docker rm -f nyxgpt-cassandra

docker run -d \
  --name nyxgpt-cassandra \
  -p 9042:9042 \
  -v nyxgpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```

---

## Cassandra schema

Create the keyspace and table:

```sql
CREATE KEYSPACE IF NOT EXISTS nyxgpt
WITH replication = {
  'class': 'SimpleStrategy',
  'replication_factor': 1
};

USE nyxgpt;

CREATE TABLE IF NOT EXISTS rag_chunks (
  doc_id text,
  chunk_id int,
  text text,
  metadata text,
  embedding vector<float, 768>,
  embedding_model text,
  embedding_dim int,
  doc_hash text,
  ingested_at timestamp,
  updated_at timestamp,
  PRIMARY KEY (doc_id, chunk_id)
);

CREATE CUSTOM INDEX IF NOT EXISTS rag_embedding_idx
ON rag_chunks (embedding)
USING 'org.apache.cassandra.index.sai.StorageAttachedIndex';

CREATE INDEX IF NOT EXISTS rag_model_idx
ON rag_chunks (embedding_model);
```

> ⚠️ The `embedding` dimension **must match** the configured embedding model.
>
> **Note:** The schema now includes version tracking fields (`doc_hash`, `ingested_at`, `updated_at`) for automatic update detection.

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
cassandra_keyspace = nyxgpt
cassandra_table = rag_chunks

# Connection pool settings (new in v2.0)
# cassandra_pool_size = 2              # Core connections per host (integer ≥ 1, default: 2)
# cassandra_health_check_interval = 30.0  # Seconds between health checks (default: 30.0)
# cassandra_reconnect_max_attempts = 3    # Max reconnect attempts (integer ≥ 1, default: 3)
```

---

## Document Update Detection

nyxGPT now includes automatic document update detection using SHA-256 content hashing. This feature:

- **Detects Changes**: Automatically detects when document content has changed
- **Skips Unnecessary Work**: Avoids re-ingesting unchanged documents
- **Incremental Updates**: Only re-indexes when content actually changes
- **Stale Chunk Cleanup**: Automatically deletes old chunks when updating documents
- **Version Tracking**: Tracks ingestion and update timestamps for each document

### How It Works

1. **Hash Computation**: When ingesting a document, nyxGPT computes a SHA-256 hash of the content
2. **Change Detection**: Before re-ingesting, compares new hash with stored hash
3. **Smart Re-indexing**:
   - If hashes match → skip re-ingestion (no-op)
   - If hashes differ → delete old chunks and ingest new version
   - If document doesn't exist → ingest normally
4. **Version Metadata**: Stores `doc_hash`, `ingested_at`, and `updated_at` timestamps

### Example Workflow

```bash
# First ingestion
nyxgpt rag ingest mydoc.txt my-doc-id
# Output: Ingested 5 chunks for doc_id=my-doc-id

# Re-ingest without changes
nyxgpt rag ingest mydoc.txt my-doc-id
# Output: Document my-doc-id unchanged (hash: 8f4e2a1b...), skipped re-ingestion

# Edit mydoc.txt then re-ingest
nyxgpt rag ingest mydoc.txt my-doc-id
# Output: Updated 7 chunks for doc_id=my-doc-id
#         Document hash: 3c9d8f2a...
#         Previous hash: 8f4e2a1b...
```

### Force Re-indexing

To force re-ingestion even when content hasn't changed (useful for testing or after config changes):

```python
from nyxgpt.rag.rag import ingest_document

result = ingest_document(
    doc_id="my-doc",
    text=content,
    force_update=True  # Bypass hash check
)
```

---

## CLI commands

### Ingest a document

```bash
nyxgpt rag ingest <doc_id> <path> [--ensure-schema] [--collection default]
```

Example:
```bash
nyxgpt rag ingest readme-v1 README.md --ensure-schema
```

The command now outputs update detection status:
- **Ingested**: New document added
- **Updated**: Existing document with changed content
- **Skipped**: Existing document with unchanged content

### Show document information

Get version tracking info for a specific document:

```bash
nyxgpt rag info <doc_id> [--collection default]
```

Example:
```bash
nyxgpt rag info readme-v1

# Output:
# Document: readme-v1
#   Collection: default
#   Chunks: 15
#   Embedding model: nomic-embed-text:latest
#   Document hash: 8f4e2a1b3c9d8f2a...
#   Ingested at: 2026-01-20T10:30:45
#   Updated at: 2026-01-20T14:15:22
```

### List ingested documents

```bash
nyxgpt rag list [--collection default]
```

### Delete a document

```bash
nyxgpt rag delete <doc_id> [--collection default]
```

### Wipe all RAG data (development only)

```bash
nyxgpt rag wipe --yes-really [--collection default]
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

### Per-Session Document Attachment (Force-Include Mode)

Force-include mode lets you pin specific documents to a session so their chunks are **always** retrieved, regardless of semantic relevance. This is useful when you want the model to reference a particular document in every response.

#### How It Works

When one or more documents are attached to a session, nyxGPT performs **two** retrieval passes during chat:

1. **Normal RAG pass** — semantic retrieval based on your query (subject to `rag_filters` if set)
2. **Force-include pass** — targeted retrieval filtered to the attached `doc_id`s

Results are merged with deduplication by `(doc_id, chunk_id)`. Force-included chunks appear **first** in the context, giving them higher priority.

**Note:** The doc_id must already be ingested into the RAG index. Attaching an unknown doc_id silently produces no chunks for that document.

#### `GET /api/v1/sessions/{name}/documents`

List documents currently attached to a session.

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": ["report-2025.pdf", "spec-v2.md"]
}
```

#### `POST /api/v1/sessions/{name}/documents`

Attach a document to a session. Idempotent — attaching the same doc_id twice has no effect.

**Request body:**
```json
{
  "doc_id": "report-2025.pdf"
}
```

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": ["report-2025.pdf"]
}
```

#### `DELETE /api/v1/sessions/{name}/documents/{doc_id}`

Detach a document from a session.

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": []
}
```

#### Example: Attach and chat

```bash
# Ingest a document
curl -X POST http://127.0.0.1:8000/api/v1/rag/upload -F "file=@spec.md"

# Attach it to your session
curl -X POST http://127.0.0.1:8000/api/v1/sessions/my-session/documents \
  -H "Content-Type: application/json" \
  -d '{"doc_id": "spec.md"}'

# Chat — spec.md chunks are always included regardless of query
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Summarise the key requirements", "session": "my-session"}'
```

---

### Document Upload

#### `POST /api/v1/rag/upload`

Upload and ingest a document for RAG.

**Supported file types:** `.txt`, `.md` (with frontmatter parsing), `.json`, `.pdf` (with OCR support for image-based PDFs), `.pptx` (PowerPoint presentations), `.docx` (Microsoft Word), `.epub` (eBooks), `.html`/`.htm` (web pages)

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

**File Type Handling:**

- **Markdown (`.md`)**: Extracts YAML frontmatter (title, author, tags, etc.), preserves headers hierarchy, handles code blocks
- **PDF (`.pdf`)**: Extracts text from all pages with automatic OCR fallback for image-based PDFs (configurable)
- **PowerPoint (`.pptx`)**: Extracts slide text, speaker notes, preserves slide order
- **Microsoft Word (`.docx`)**: Extracts paragraphs, headings, tables, and embedded image markers
- **ePUB (`.epub`)**: Extracts metadata (title, author, publisher, etc.), chapter structure, and clean text content
- **HTML (`.html`/`.htm`)**: Extracts clean text with semantic structure preservation, removes boilerplate (scripts, styles, nav, headers, footers, ads), preserves headings, paragraphs, lists, tables, and blockquotes
- **JSON (`.json`)**: Formatted with indentation for readability
- **Plain text (`.txt`)**: UTF-8 encoded text

**PDF Files:**
- Extracts document metadata (title, author, subject, creator, dates)
- Improved table handling with preserved structure
- Layout-aware text extraction for multi-column documents
- Formatting preservation for better context quality
- Tables are marked with `[Table]` headers in extracted text
- Metadata is prepended as `[Metadata]` section
- **OCR support** for image-based PDFs (automatically triggered when text extraction is minimal)
  - Configurable DPI, language, and page segmentation mode
  - Requires Tesseract OCR to be installed on the system
  - See `[pdf]` section in config.ini for OCR settings

**ePUB Files:**
- Extracts rich metadata (title, author, publisher, description, date, language)
- Preserves chapter structure with chapter markers
- Cleans HTML content to plain text while maintaining semantic structure
- Removes boilerplate (scripts, styles) for cleaner text
- Handles multi-chapter books with proper organization

**HTML Files:**
- Extracts metadata from meta tags (title, description, author, keywords, Open Graph tags)
- Preserves semantic structure (headings, paragraphs, lists, tables, blockquotes, code blocks)
- Removes boilerplate and non-content elements (scripts, styles, nav, headers, footers, ads, sidebars)
- Intelligently identifies main content area (main, article, or content divs)
- Handles multiple encodings (UTF-8, ISO-8859-1, Windows-1252)
- Formats lists and tables for better readability
- Clean text extraction suitable for RAG context

**Error (unsupported file type):**
```json
{
  "detail": "File type .exe not supported. Allowed: {'.txt', '.md', '.json', '.pdf', '.pptx', '.docx', '.epub', '.html', '.htm'}"
}
```

---

## RAG‑assisted chat

When enabled globally or per-session, retrieved context is injected automatically during chat:

```bash
nyxgpt chat "What does Cassandra support for vector search?"
```

The retrieved context is prepended as a system message.

**Priority chain:** Explicit API `rag_enabled` parameter > Session metadata > Global config `enable_chat_context`

---

## Multiple Embedding Models

**New in v1.0:** nyxGPT now supports using multiple embedding models simultaneously via collections.

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
from nyxgpt.rag.rag import ingest_document

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
from nyxgpt.rag.rag import retrieve_context

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
from nyxgpt.rag.model_compare import compare_models, print_comparison_table

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
from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

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

## Chunk Boundary Optimization

**New in v1.0:** Enhanced chunking with semantic boundary awareness for improved retrieval quality.

### Overview

nyxGPT implements advanced text chunking that goes beyond simple character-based splitting:

- **Sentence Boundary Awareness**: Splits on sentence boundaries rather than arbitrary character positions
- **Heading-Aware Splitting**: Preserves Markdown heading structure with content
- **Configurable Overlap Strategies**: Three overlap methods for different use cases
- **Semantic Chunking**: Respects paragraph and section boundaries

### Why It Matters

Better chunk boundaries lead to:
- **Higher retrieval quality**: Complete sentences and paragraphs are more semantically meaningful
- **Better context**: Headings provide hierarchical context for content
- **Reduced fragmentation**: Sentences aren't split mid-thought
- **Improved LLM comprehension**: Complete semantic units are easier to understand

### Configuration Options

Add to `~/.nyxGPT/config.ini` under `[rag]`:

```ini
[rag]
# Basic chunking
chunk_size = 800             # Target max characters per chunk
chunk_overlap = 100          # Characters to overlap between chunks

# Chunk boundary optimization (new)
overlap_strategy = trailing  # Options: trailing, sentence, semantic
preserve_headings = true     # Keep headings with their content
sentence_aware = true        # Split on sentence boundaries
```

### Overlap Strategies

**trailing** (default, legacy behavior):
- Overlaps with trailing characters from previous chunk
- Word-safe (starts at whitespace boundary)
- Good for: General-purpose use, backward compatibility

**sentence**:
- Overlaps with complete sentences from previous chunk
- Finds sentences that fit within overlap budget
- Good for: Q&A, fact retrieval, precise context

**semantic**:
- Overlaps with complete paragraphs/sections from previous chunk
- Preserves full semantic units
- Good for: Narrative text, documentation, long-form content

### Heading-Aware Splitting

When `preserve_headings = true`:

```markdown
# Main Section

Content under main section.

## Subsection

Content under subsection.
```

Results in chunks that keep headings with their content, maintaining hierarchical context.

### Sentence Boundary Detection

When `sentence_aware = true`:

- Detects sentence endings: `.`, `!`, `?`
- Avoids false splits on abbreviations: `Dr.`, `Mr.`, `U.S.`, etc.
- Handles edge cases: numbers, URLs, etc.

### Example Comparison

**Without optimization** (legacy):
```
Chunk 1: "This is the first sentence. This is the sec"
Chunk 2: "ond sentence. This is the third sentence."
```

**With optimization** (sentence-aware):
```
Chunk 1: "This is the first sentence. This is the second sentence."
Chunk 2: "This is the second sentence. This is the third sentence."
```

### Performance Impact

- **Ingestion**: Minimal overhead (< 5% slower for sentence detection)
- **Retrieval**: No impact (chunking happens at ingestion time)
- **Quality**: Significant improvement in retrieval relevance

### Best Practices

1. **Enable sentence_aware for most content**: Better boundaries → better retrieval
2. **Use semantic overlap for documentation**: Preserves full paragraphs
3. **Use sentence overlap for Q&A**: Complete sentences provide better context
4. **Keep preserve_headings enabled**: Hierarchical context improves relevance
5. **Adjust chunk_size based on content**:
   - Technical docs: 800-1200 characters
   - Narrative text: 1200-1600 characters
   - Code/snippets: 400-800 characters

---

## RAG Evaluation Metrics

**New in v1.0:** nyxGPT now provides comprehensive evaluation metrics to assess RAG quality.

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

## RAG Playground

The RAG Playground is an interactive web interface for testing queries, adjusting parameters, comparing results, and performing A/B testing. It provides real-time debugging and performance metrics visualization to help optimize your RAG system.

### Access

Navigate to `http://127.0.0.1:3000/admin/playground` in your web browser while the FastAPI backend and Next.js web UI are running.

### Interface Overview

The playground consists of four main panels:

#### 1. Query Builder (Left Panel)

**Query Input:**
- Multi-line text area for entering search queries
- Supports any query text

**Adjustable Parameters:**
- **Top K** (1-50): Number of results to retrieve
  - Slider control with real-time value display
  - Default: 5
- **Min Score** (0.0-1.0): Minimum relevance threshold
  - Slider control with 0.05 step increments
  - Default: 0.0
- **Collection**: Dropdown selector for choosing which collection to query
  - Shows document count and chunk count for each collection
  - Default: "default"

**Feature Toggles:**
- **Enable Debug Mode**: Collect detailed timing and performance metrics
  - Enables debug_info in query response
  - Shows timing breakdowns, query variants, filtering pipeline
- **Collect Evaluation Metrics**: Gather comprehensive retrieval accuracy and latency data
  - Uses `/api/v1/rag/metrics/query` endpoint
  - Provides retrieval accuracy, latency breakdown, hit rate metrics

**Execution:**
- "Run Query" button executes the query with current parameters
- Button disabled when query is empty or query is executing
- Shows "Executing..." state during query execution

#### 2. Results Display (Center Panel)

**Tabbed Interface:**

**Results Tab:**
- List of retrieved chunks with relevance scores
- Each result card shows:
  - Result rank number
  - Document ID and chunk ID
  - Color-coded relevance score:
    - Green (≥0.7): High relevance
    - Yellow (0.4-0.7): Medium relevance
    - Red (<0.4): Low relevance
  - Full chunk text with word wrapping
- Empty state message when no results found

**Metrics Tab** (requires `collect_metrics` enabled):
- **Retrieval Accuracy**:
  - Results returned count
  - Unique documents count
  - Score distribution: min, max, mean, median
- **Latency Breakdown**:
  - Total time in milliseconds
  - Per-stage timings: embedding, vector search, keyword search, fusion, reranking
- **Hit Rate**:
  - Success rate percentage
  - Threshold performance metrics
- Empty state message when metrics not collected

**Debug Tab** (requires `debug_mode` enabled):
- **Query Processing**:
  - Original query text
  - Total queries executed
  - Query variants (if query expansion enabled)
- **Timing Breakdown**:
  - Total time and per-stage execution times
- **Results Filtering Pipeline**:
  - Raw results count
  - After min score filter
  - After deduplication
  - After max chunks limit
  - Score range and mean
- **Full JSON Debug Output**:
  - Expandable details section
  - Complete debug_info JSON with syntax highlighting
- Empty state message when debug mode not enabled

#### 3. Query History (Left Panel, Bottom)

- Automatically stores last 20 queries in browser localStorage
- Each history item shows:
  - Execution timestamp
  - Query text (truncated with ellipsis)
  - Result count and parameters used
- Click any history item to view its results
- Checkbox for selecting queries to compare (when comparison mode enabled)
- "Clear" button to delete all history

#### 4. Comparison Panel (Right Panel, toggleable)

- Shows side-by-side comparison of selected queries
- Activated by "Show Comparison" button in header
- For each selected query, displays:
  - Execution timestamp
  - Query text
  - Result count and top_k parameter
  - Total execution time (if debug mode was enabled)
  - Average score (if debug info available)
- Select queries from history using checkboxes
- Compare up to multiple queries simultaneously

### Use Cases

**Parameter Optimization:**
```
1. Run a query with top_k=5
2. Run the same query with top_k=10
3. Compare results to see if more results improve coverage
4. Adjust min_score to filter low-quality results
5. Monitor score distribution in Metrics tab
```

**A/B Testing:**
```
1. Enable comparison mode
2. Test different query phrasings for the same information need
3. Compare result counts, scores, and execution times
4. Identify which query variant performs best
5. Use query history to revisit and compare previous experiments
```

**Performance Tuning:**
```
1. Enable debug mode and metrics collection
2. Run a representative query
3. Review latency breakdown in Debug tab
4. Identify bottlenecks (embedding, search, fusion, reranking)
5. Adjust config.ini settings to optimize slow stages
6. Re-run query and compare metrics
```

**Collection Comparison:**
```
1. Ingest the same document into multiple collections with different embedding models
2. Run the same query against each collection
3. Compare result quality, scores, and execution times
4. Determine optimal embedding model for your use case
```

**Query Refinement:**
```
1. Start with a broad query
2. Review results in Results tab
3. Refine query based on what was retrieved
4. Use query history to track refinement progression
5. Compare original vs refined query performance
```

### API Endpoints Used

The playground uses existing RAG API endpoints:

- **POST /api/v1/rag/query**: Standard query with optional debug mode
- **POST /api/v1/rag/metrics/query**: Query with full evaluation metrics
- **GET /api/v1/rag/collections**: List available collections for selector
- **GET /api/v1/rag/config**: Retrieve min_score and threshold config

No new backend endpoints are required; the playground is a pure frontend enhancement.

### Storage and Privacy

- Query history stored in browser localStorage (client-side only)
- No server-side query logging or history persistence
- History limited to 20 most recent queries
- Clear history button deletes all stored queries
- History is per-browser, not synced across devices

### Prerequisites

- FastAPI backend running: `nyxgpt ops restart api`
- Next.js web UI running: `nyxgpt ops restart web`
- Cassandra running for RAG queries
- At least one collection with ingested documents

### Tips

- Enable both debug mode and metrics collection for maximum insight
- Use query history to build up a test suite of representative queries
- Compare queries with identical parameters except one variable to isolate impact
- Export comparison data (future enhancement) for reporting
- Color-coded scores help quickly identify result quality at a glance

---

## Notes

- RAG is optional and can be disabled at any time.
- Cassandra is used only for vector storage; no full‑text search is required.
- RAG latency depends on embedding generation and vector search performance.
- Multiple embedding models are supported via collections (separate tables per model).
- Evaluation metrics provide detailed quality and performance insights for RAG queries.

---

## Hybrid Search (Keyword + Vector)

**New in v1.0:** nyxGPT now supports hybrid search that combines BM25 keyword search with vector similarity search for improved retrieval quality.

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

Add to `~/.nyxGPT/config.ini`:

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
from nyxgpt.rag.rag import retrieve_context

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
nyxgpt rag query "Cassandra vector search"

# Debug mode shows fusion details
nyxgpt rag query "Cassandra vector search" --debug
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

---

## Reranking (Cross-Encoder)

Reranking is an optional second-pass scoring step that improves retrieval precision by re-scoring initial results with a more sophisticated relevance model.

### Why Reranking?

**First-pass retrieval** (vector/hybrid search) is optimized for speed and recall:
- Embeds query into a single vector
- Compares against millions of documents in milliseconds
- Fast but may miss subtle relevance signals

**Reranking** uses cross-encoder scoring for precision:
- Scores each query-document pair individually
- Captures fine-grained relevance signals
- More expensive, applied only to top candidates

**When to Use Reranking:**
- Precision is critical (top-3 results must be highly relevant)
- You have many similar documents in your corpus
- Initial retrieval returns too many marginally relevant results
- Latency budget allows for additional processing

**When to Skip Reranking:**
- Real-time applications requiring <200ms latency
- Small corpus (<100 documents) where initial retrieval is already precise
- Resource-constrained environments

### Configuration

Enable reranking in `~/.nyxGPT/config.ini`:

```ini
[rag]
# Enable reranking (disabled by default)
enable_reranking = true

# Model to use for reranking (defaults to nyxgpt.default_model)
# Use a fast model for better performance
reranker_model = qwen2.5:0.5b

# Number of results to return after reranking
# Smaller values focus on top precision
rerank_top_n = 3

# Timeout for each reranking request (seconds)
# Total reranking time = timeout * num_candidates
reranker_timeout_seconds = 30
```

### How It Works

**Pipeline:**

1. **First-pass retrieval**: Hybrid search returns top-K candidates (e.g., 10-20 results)
2. **Reranking**: Each candidate is scored using cross-encoder approach
3. **Top-N selection**: Return only the top-N highest-scoring results (e.g., 3)

**Scoring:**

Unlike embeddings (bi-encoder) which encode query and document separately, reranking uses a cross-encoder approach:
- Sends both query and document together to the LLM
- LLM scores relevance from 0.0 (irrelevant) to 1.0 (highly relevant)
- More context-aware than vector similarity alone

**Performance:**

- First-pass (10 candidates): ~50-100ms
- Reranking (10 scores): ~300-500ms per candidate = 3-5 seconds total
- Use fast models (qwen2.5:0.5b) to minimize latency
- Adjust `rerank_top_n` to control cost vs. quality trade-off

### Example: Impact on Precision

**Query:** "How do I configure Cassandra replication?"

**Without reranking (vector similarity only):**
1. "Cassandra replication strategies" (0.87) ✓
2. "Database replication concepts" (0.84) ✗ (too general)
3. "Cassandra configuration guide" (0.81) ~ (relevant but not specific)

**With reranking (cross-encoder scoring):**
1. "Cassandra replication strategies" (0.95) ✓✓
2. "Configuring Cassandra replication_factor" (0.91) ✓✓
3. "Cassandra cluster setup tutorial" (0.78) ✓

Reranking correctly identifies the most specific, actionable results.

### Debug Mode

Use `debug_mode=True` to inspect reranking metrics:

```python
from nyxgpt.rag import retrieve_context

results, debug_info = retrieve_context(
    "Cassandra replication",
    top_k=10,
    debug_mode=True
)

print(f"Reranking enabled: {debug_info.reranking_enabled}")
print(f"Reranker model: {debug_info.reranker_model}")
print(f"Candidates reranked: {debug_info.num_candidates_reranked}")
print(f"Results after rerank: {debug_info.num_results_after_rerank}")
print(f"Reranking time: {debug_info.reranking_time_ms:.2f}ms")
```

### Troubleshooting

**Problem: Reranking is too slow**

Solutions:
- Use a faster reranker model (e.g., `qwen2.5:0.5b` instead of larger models)
- Reduce `rerank_top_n` (fewer results = faster)
- Reduce first-pass `chat_top_k` (fewer candidates to rerank)
- Increase `reranker_timeout_seconds` if hitting timeouts

**Problem: Reranked results don't seem better**

Check:
- Is `enable_reranking=true` in config?
- Run with `debug_mode=True` to verify reranking is active
- Check `num_candidates_reranked` and `reranking_time_ms`
- Try a different `reranker_model` for better scoring quality

**Problem: All reranking scores are similar**

This can happen when:
- Documents are all highly relevant (good problem to have!)
- Reranker model is too small/simple (try a larger model)
- Query is too vague (reranker can't distinguish relevance)

**Problem: Reranking sometimes fails**

Check logs for reranking errors. Common causes:
- Ollama timeout (increase `reranker_timeout_seconds`)
- Ollama model not loaded (pull model first: `nyxgpt models pull qwen2.5:0.5b`)
- Invalid JSON response from LLM (model needs better instruction following)

Reranking failures are non-fatal - failed results keep their original scores and ranking.

### Performance Tuning

**For maximum precision:**
```ini
enable_reranking = true
reranker_model = qwen2.5:7b  # Larger model
rerank_top_n = 3
chat_top_k = 20  # More candidates
```

**For balanced speed/quality:**
```ini
enable_reranking = true
reranker_model = qwen2.5:0.5b  # Fast model
rerank_top_n = 5
chat_top_k = 10
```

**For maximum speed (disable reranking):**
```ini
enable_reranking = false
chat_top_k = 5
```

---

## Force-Include Mode (Per-Session Document Attachment)

nyxGPT supports **force-include mode**, which guarantees that specific documents are always retrieved and injected into the chat context for a session, regardless of the standard RAG relevance scoring.

### How It Works

When documents are attached to a session, the `_prepare_chat_context` function performs two retrieval passes:

1. **Normal RAG retrieval** — ranked by vector similarity for the user's query
2. **Force-include retrieval** — always retrieves chunks from the attached document IDs using a `MetadataFilter`

The two result sets are then merged with these rules:
- Force-included rows appear **first** in the merged list (highest priority)
- Deduplication by `(doc_id, chunk_id)` ensures no row appears twice
- If a chunk appears in both passes, the force-included copy is kept

### Use Cases

- Pin a reference document to every turn of a research session
- Ensure a specific policy or specification is always in context
- Work intensively with a single document without relying on relevance scoring

### API Endpoints

Three REST endpoints manage per-session document attachments:

#### List attached documents

```
GET /api/v1/sessions/{name}/documents
```

Response:
```json
{
  "session": "my-session",
  "attached_doc_ids": ["doc-abc", "doc-xyz"]
}
```

#### Attach a document

```
POST /api/v1/sessions/{name}/documents
Content-Type: application/json

{"doc_id": "doc-abc"}
```

Response (200 OK):
```json
{
  "session": "my-session",
  "attached_doc_ids": ["doc-abc"]
}
```

The operation is idempotent — attaching the same `doc_id` twice results in a single entry.

> **Note:** There is no validation that the `doc_id` exists in the RAG index. An unknown ID silently produces no force-included chunks.

#### Detach a document

```
DELETE /api/v1/sessions/{name}/documents/{doc_id}
```

Response (200 OK):
```json
{
  "session": "my-session",
  "attached_doc_ids": []
}
```

### Session Metadata

Attached document IDs are persisted in the session metadata file (`<name>.meta.json`) under the key `attached_doc_ids`:

```json
{
  "rag_enabled": true,
  "attached_doc_ids": ["doc-abc", "doc-xyz"]
}
```

The field is absent when no documents are attached (it is not initialised to an empty list by default).

### Interaction with RAG Filters

Force-include retrieval always uses the attached doc IDs as its filter and **ignores** any `rag_filters` passed to the chat endpoint. Normal RAG retrieval respects `rag_filters` as usual.

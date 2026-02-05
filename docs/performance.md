# Performance Tuning Guide

This guide covers optimization strategies for nyxGPT to improve response times, reduce resource usage, and enhance overall system performance.

---

## Quick Performance Checklist

- [ ] Choose appropriate model size for your hardware
- [ ] Enable system prompt minimization for verbose prompts
- [ ] Enable caching for embeddings and responses
- [ ] Tune RAG configuration for your use case
- [ ] Optimize Cassandra settings
- [ ] Configure batch sizes appropriately
- [ ] Monitor resource usage and adjust

---

## Model Selection

The choice of LLM model has the **largest impact** on performance.

### Small Models (< 1B parameters)

**Recommended for**:
- Low-end hardware (< 8GB RAM)
- Fast response times
- Development and testing
- Simple question answering

**Examples**:
```ini
[nyxgpt]
default_model = qwen2.5:0.5b
```

**Characteristics**:
- **Response time**: 1-3 seconds
- **Memory**: 1-2 GB
- **Quality**: Good for simple tasks, struggles with complex reasoning

### Medium Models (1B-8B parameters)

**Recommended for**:
- Mid-range hardware (8-16GB RAM)
- Balanced performance/quality
- General purpose use
- Production deployments

**Examples**:
```ini
[nyxgpt]
default_model = llama3.2:3b
# or
default_model = qwen2.5:7b
```

**Characteristics**:
- **Response time**: 3-10 seconds
- **Memory**: 4-8 GB
- **Quality**: Good balance of speed and capability

### Large Models (> 8B parameters)

**Recommended for**:
- High-end hardware (16GB+ RAM)
- Complex reasoning tasks
- High-quality outputs
- Non-latency-sensitive workflows

**Examples**:
```ini
[nyxgpt]
default_model = llama3.1:8b
# or
default_model = qwen2.5:14b
```

**Characteristics**:
- **Response time**: 10-60+ seconds
- **Memory**: 8-16+ GB
- **Quality**: Best reasoning and output quality

### Model Selection Tips

1. **Start small**: Begin with `qwen2.5:0.5b` and scale up as needed
2. **Test locally**: Use `ollama run <model>` to test performance before configuring
3. **Check GPU availability**: Models run 5-10x faster on GPU vs CPU
4. **Monitor memory**: Use `ollama ps` to see model memory usage
5. **Consider quantization**: Smaller quantized versions (e.g., `q4_0`, `q4_K_M`) trade quality for speed

---

## System Prompt Optimization

System prompts can be optimized to reduce token usage and improve response times.

### System Prompt Minimization

```ini
[nyxgpt]
system_prompt_minimize = false    # Default
```

**When to enable**:
- Using long or verbose system prompts
- Want to reduce token usage
- Need faster response times
- Approaching context window limits

**What it does**:
- Removes redundant whitespace and formatting
- Strips filler words ("Please", "You are", "Make sure to", etc.)
- Condenses verbose patterns ("in order to" → "to", "as well as" → "and")
- Preserves semantic meaning

**Performance impact**:
- Reduces system prompt tokens by 10-30% depending on verbosity
- Minimal processing overhead (<1ms)
- Particularly effective with verbose custom system prompts

**Example**:
```
Before: "You are a helpful assistant. Please respond to user queries carefully.
         Make sure to be accurate as well as concise."
After:  "a helpful assistant. respond to user queries carefully. be accurate and concise."
```

**Trade-offs**:
- Slightly less formal tone
- May affect very specific phrasing requirements
- Disable if exact wording of system prompt is critical

---

## Caching

Caching dramatically improves performance by avoiding redundant computations. nyxGPT supports two types of caching:

### Embedding Cache

Cache embeddings to avoid recomputing vectors for identical texts.

```ini
[cache]
# Enable embedding caching
embedding_cache_enabled = true

# Backend: "memory" (fast, volatile) or "disk" (persistent across restarts)
embedding_cache_backend = memory

# Memory backend: LRU cache with max size
embedding_cache_max_size = 1000

# TTL (time-to-live) in seconds
# Memory default: 3600s (1 hour)
# Disk default: 86400s (24 hours)
embedding_cache_ttl_seconds = 3600

# Disk backend only: cache directory
embedding_cache_dir = ~/.nyxGPT/cache/embeddings
```

**When to enable**:
- Using RAG with repeated queries
- Ingesting documents with overlapping content
- Running tests or benchmarks
- Processing batches of similar queries

**Performance impact**:
- **Cache hit**: ~1ms (memory) or ~10ms (disk) vs 100-500ms for embedding computation
- **Speedup**: 100-500x for identical texts
- **Memory overhead**: ~1KB per cached embedding (for 768-dim vectors)

**Backend selection**:
- **Memory**: Best for active development, testing, or short sessions
  - Pros: Extremely fast (1ms), no disk I/O
  - Cons: Lost on restart, limited by max_size
  - Recommended for: < 1000 unique embedding calls per session

- **Disk**: Best for long-running processes or persistent caching
  - Pros: Survives restarts, unlimited size (disk-limited)
  - Cons: Slower (10ms), file I/O overhead
  - Recommended for: Production, repeated document ingestion

**Tuning tips**:
- Start with memory cache (faster, simpler)
- Increase `embedding_cache_max_size` if you have RAM available
- Use disk cache for production or persistent workloads
- Set shorter TTL (e.g., 1800s) to avoid stale embeddings
- Clear cache after model changes: embeddings are model-specific

### Response Cache

Cache LLM responses to avoid redundant API calls for identical prompts.

```ini
[cache]
# Enable response caching
response_cache_enabled = true

# Backend: "memory" or "disk"
response_cache_backend = memory

# Memory backend: max number of cached responses
response_cache_max_size = 100

# TTL in seconds
# Memory default: 1800s (30 minutes)
# Disk default: 3600s (1 hour)
response_cache_ttl_seconds = 1800

# Disk backend only: cache directory
response_cache_dir = ~/.nyxGPT/cache/responses
```

**When to enable**:
- Testing or debugging (repeated identical prompts)
- Batch processing with duplicate queries
- Demo or presentation scenarios
- Development with fixed test inputs

**When NOT to enable**:
- Production chat (responses should vary based on context)
- Creative or non-deterministic use cases
- When using RAG (context may change even for identical prompts)

**Performance impact**:
- **Cache hit**: ~1ms (memory) or ~10ms (disk) vs 1-60s for LLM generation
- **Speedup**: 1000-60000x for identical prompts
- **Memory overhead**: ~1KB per cached response (varies with response length)

**WARNING**: Response caching is based on the full conversation context (all messages + model). Identical user prompts with different conversation history will **not** share cache entries. This is intentional to ensure correct responses, but means cache hit rate may be lower than expected.

**Tuning tips**:
- Keep `response_cache_max_size` small (50-100) for memory cache
- Use shorter TTL (900-1800s) to avoid stale responses
- Disable for production chat unless you need deterministic responses
- Best suited for testing, CI/CD, or batch processing
- Clear cache when changing models or system prompts

### Cache Invalidation

Both caches support TTL-based automatic expiration. Manual invalidation:

**Via Python**:
```python
from nyxgpt.rag.embeddings import clear_embedding_cache
from nyxgpt.chat import clear_response_cache

clear_embedding_cache()
clear_response_cache()
```

**Via config change**: Set `ttl_seconds = 0` for unlimited caching (cache persists until manually cleared or restart).

### Monitoring Cache Performance

Enable debug logging to see cache hits/misses:

```ini
[logging]
level = DEBUG
```

Then check logs:
```bash
tail -f ~/.nyxGPT/logs/nyxgpt.log | grep -i cache
```

Look for:
- `Cache hit: <key>...` - Successful cache retrieval
- `Cache miss (expired): <key>...` - Expired entry removed
- `Cache eviction (LRU): <key>...` - LRU eviction due to max_size

---

## RAG Configuration Tuning

RAG settings significantly impact both quality and performance.

### Disable RAG for Simple Use Cases

If you're not using RAG, disable it to avoid overhead:

```ini
[rag]
enabled = false
```

**Performance impact**: Saves 100-500ms per request when disabled.

### Chunk Retrieval Settings

#### chat_top_k

Number of candidate chunks retrieved before filtering.

```ini
[rag]
chat_top_k = 5    # Default
```

**Tuning guidance**:
- **Lower (1-3)**: Faster retrieval, may miss relevant context
- **Medium (4-6)**: Balanced performance/recall
- **Higher (7-10+)**: Better recall, slower retrieval

**Performance impact**: Each additional chunk adds ~10-50ms retrieval time.

#### max_chunks

Hard cap on chunks injected into the prompt.

```ini
[rag]
max_chunks = 6    # Default
```

**Tuning guidance**:
- **Lower (1-3)**: Faster generation, less context
- **Medium (4-6)**: Balanced
- **Higher (7-10+)**: More context, slower generation

**Performance impact**: More chunks = larger prompts = slower LLM processing.

#### chat_context_max_chars

Maximum characters of retrieved context.

```ini
[rag]
chat_context_max_chars = 2400    # Default
```

**Tuning guidance**:
- **Lower (500-1500)**: Minimal context, faster
- **Medium (1500-3000)**: Balanced
- **Higher (3000-6000)**: Maximum context, slower

**Performance impact**: Larger context = longer prompts = 2-5x slower generation for long contexts.

**CRITICAL**: Keep this modest (<3000) when using large models on CPU.

### Chunking Configuration

#### chunk_size

Size of text chunks for embedding.

```ini
[rag]
chunk_size = 800    # Default
chunk_overlap = 100
```

**Tuning guidance**:
- **Smaller (400-600)**: More granular, more chunks, slower ingestion
- **Medium (700-900)**: Balanced
- **Larger (1000-1500)**: Fewer chunks, faster ingestion, may lose granularity

**Performance impact**: Affects ingestion time and retrieval quality, not runtime chat performance.

### Embedding Configuration

#### embedding_batch_size

Number of chunks embedded in a single Ollama request.

```ini
[rag]
embedding_batch_size = 16    # Default
```

**Tuning guidance**:
- **Lower (4-8)**: Less memory, slower ingestion
- **Medium (12-20)**: Balanced
- **Higher (24-48)**: Faster ingestion, more memory

**Performance impact**: Only affects `nyxgpt rag ingest` speed, not chat performance.

#### Embedding Model Selection

```ini
[rag]
embedding_model = nomic-embed-text
embedding_dim = 768
```

**Options**:
- `nomic-embed-text` (768 dim): Recommended, good quality/speed balance
- `mxbai-embed-large` (1024 dim): Higher quality, slower
- Smaller models: Faster but lower retrieval quality

### Query Expansion

```ini
[rag]
enable_query_expansion = false    # Default
```

**When to enable**:
- Queries use different terminology than documents
- Need to find related concepts
- Willing to trade speed for better recall

**Performance impact**: Adds 1-3 seconds per query (LLM call to generate expansions).

**Parallel Query Execution**:

When query expansion is enabled, multiple query variants are executed concurrently for better performance:

```ini
[rag]
enable_query_expansion = true
query_parallel_workers = 4    # Default: 4, recommended: 2-8
```

**Tuning guidance**:
- **Lower (2-3)**: Less CPU usage, slightly slower for multiple queries
- **Medium (4-6)**: Balanced performance (recommended)
- **Higher (7-10)**: Faster parallel execution but higher CPU/memory usage

**Performance impact**:
- Sequential: 3 queries × 100ms = 300ms total
- Parallel (4 workers): 3 queries executed concurrently ≈ 100-150ms total
- Improvement: 2-3x faster when query expansion generates multiple variants

**Note**: Parallel execution is automatically enabled when `enable_query_expansion = true` and multiple query variants are generated. Single queries use sequential execution (no parallelism overhead).

### Deduplication

```ini
[rag]
dedupe = true    # Default recommended
```

**Keep enabled** unless:
- You know chunks are already unique
- Need absolute maximum speed (saves ~50ms)

---

## Cassandra Optimization

### Connection Settings

```ini
[rag]
cassandra_hosts = 127.0.0.1
cassandra_port = 9042
```

**For local use**: Default settings are optimal.

**For remote Cassandra**:
- Use connection pooling (handled automatically by driver)
- Ensure low network latency (<10ms)
- Consider local read replicas

### Vector Index Optimization

The SAI (Storage Attached Index) is automatically optimized by Cassandra, but you can improve performance by:

1. **Ensure sufficient memory**: Cassandra caches index data
   ```bash
   # Check Cassandra memory usage
   docker stats nyxgpt-cassandra
   ```

2. **Increase Docker memory limit** if needed:
   ```bash
   docker update nyxgpt-cassandra --memory=4g
   ```

3. **Monitor query performance**:
   ```sql
   -- In cqlsh
   TRACING ON;
   SELECT * FROM nyxgpt.rag_chunks ORDER BY embedding ANN OF [...] LIMIT 5;
   ```

### Keyspace Configuration

For production, consider adjusting replication:

```sql
ALTER KEYSPACE nyxgpt
WITH replication = {
  'class': 'NetworkTopologyStrategy',
  'datacenter1': 3
};
```

**For local development**: Keep `SimpleStrategy` with `replication_factor: 1`.

---

## Memory and CPU Tuning

### Ollama Resource Limits

Ollama automatically manages model memory, but you can optimize:

1. **Limit concurrent models**:
   ```bash
   # Unload unused models
   ollama rm <old-model>
   ```

2. **Set environment variables**:
   ```bash
   export OLLAMA_NUM_PARALLEL=1        # Limit parallel requests
   export OLLAMA_MAX_LOADED_MODELS=1   # Keep only one model in memory
   ```

3. **Monitor Ollama**:
   ```bash
   ollama ps                 # Check loaded models
   ollama show <model>       # Check model details
   ```

### FastAPI Worker Configuration

By default, FastAPI runs a single worker. For production:

```bash
# In production (not recommended for local use)
uvicorn nyxgpt.app:app --workers 4
```

**Note**: Multiple workers require careful session management due to file locking.

**For local use**: Keep default single worker.

### Cassandra Docker Memory

Increase if you're ingesting large document sets:

```bash
docker run -d \
  --name nyxgpt-cassandra \
  --memory=4g \
  --cpus=2 \
  -p 9042:9042 \
  -v nyxgpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```

**Recommended settings**:
- **Small datasets (<1000 documents)**: 2GB memory, 1 CPU
- **Medium datasets (1000-10000 documents)**: 4GB memory, 2 CPUs
- **Large datasets (>10000 documents)**: 8GB+ memory, 4 CPUs

---

## Batch Size Configuration

### Embedding Batch Size

Control memory vs speed tradeoff during ingestion:

```ini
[rag]
embedding_batch_size = 16    # Default
```

**Tuning based on available memory**:

| Available RAM | Recommended Batch Size |
|--------------|----------------------|
| < 8 GB       | 4-8                 |
| 8-16 GB      | 12-20               |
| > 16 GB      | 24-48               |

**Symptoms of too-large batches**:
- Ollama errors: "out of memory"
- System becomes unresponsive during ingestion
- OOM killer terminates Ollama

**Symptoms of too-small batches**:
- Ingestion is very slow
- High overhead per request

### Timeout Configuration

Adjust timeouts for slow hardware:

```ini
[nyxgpt]
chat_timeout_seconds = 180    # Default

[rag]
embedding_timeout_seconds = 120    # Default
```

**Increase if you see**:
- "Timeout" errors during chat
- Failed embeddings during ingestion

**Recommended adjustments**:
- **CPU-only, large model**: `chat_timeout_seconds = 300`
- **Slow internet to Ollama**: `embedding_timeout_seconds = 180`

---

## Performance Monitoring

### Metrics to Track

1. **Response time**:
   ```bash
   # Enable request ID logging
   tail -f ~/.nyxGPT/logs/nyxgpt.log | grep "request_id"
   ```

2. **Resource usage**:
   ```bash
   # Ollama
   ollama ps

   # Cassandra
   docker stats nyxgpt-cassandra

   # System
   top
   htop
   ```

3. **RAG performance**:
   - Check logs for retrieval times
   - Monitor chunk counts in responses
   - Track embedding generation time during ingestion

### Profiling Slow Requests

Enable debug logging:

```ini
[logging]
level = DEBUG
```

Then check logs:

```bash
tail -f ~/.nyxGPT/logs/nyxgpt.log
```

Look for:
- RAG retrieval times
- Model loading times
- Request processing duration

---

## Performance Optimization Workflow

### 1. Baseline Measurement

```bash
# Test without RAG
echo "[rag]\nenabled = false" >> ~/.nyxGPT/config.ini

# Run test query
time nyxgpt chat "Hello, how are you?"

# Note: Response time and memory usage
```

### 2. Identify Bottleneck

- **Slow response (> 30s)**: Model too large for hardware → use smaller model
- **High memory (> 80% usage)**: Reduce model size or batch sizes
- **Slow RAG retrieval (> 2s)**: Reduce `chat_top_k`, increase Cassandra memory
- **Context too large**: Reduce `chat_context_max_chars` or `max_chunks`

### 3. Apply Optimizations

Make one change at a time and measure impact:

```bash
# Example: Switch to smaller model
sed -i '' 's/default_model = .*/default_model = qwen2.5:0.5b/' ~/.nyxGPT/config.ini

# Test
time nyxgpt chat "Hello, how are you?"

# Compare to baseline
```

### 4. Iterate

Continue tuning until performance meets requirements.

---

## Recommended Configurations

### Minimal Resource (< 8GB RAM, CPU-only)

```ini
[nyxgpt]
default_model = qwen2.5:0.5b
chat_timeout_seconds = 120

[cache]
# Enable caching for faster repeated queries
embedding_cache_enabled = true
embedding_cache_backend = memory
embedding_cache_max_size = 500

response_cache_enabled = false  # Not recommended for production

[rag]
enabled = false
# Or if RAG needed:
# enabled = true
# chat_top_k = 2
# max_chunks = 3
# chat_context_max_chars = 1000
# embedding_batch_size = 4
```

### Balanced (8-16GB RAM, CPU-only)

```ini
[nyxgpt]
default_model = llama3.2:3b
chat_timeout_seconds = 180

[cache]
# Enable caching for better performance
embedding_cache_enabled = true
embedding_cache_backend = memory
embedding_cache_max_size = 1000
embedding_cache_ttl_seconds = 3600

response_cache_enabled = false  # Enable only for testing/demos

[rag]
enabled = true
chat_top_k = 5
max_chunks = 5
chat_context_max_chars = 2400
embedding_batch_size = 16
```

### High Performance (16GB+ RAM, GPU available)

```ini
[nyxgpt]
default_model = llama3.1:8b
chat_timeout_seconds = 300

[cache]
# Persistent disk cache for production
embedding_cache_enabled = true
embedding_cache_backend = disk
embedding_cache_ttl_seconds = 86400  # 24 hours
embedding_cache_dir = ~/.nyxGPT/cache/embeddings

response_cache_enabled = false  # Not recommended for production chat

[rag]
enabled = true
chat_top_k = 8
max_chunks = 8
chat_context_max_chars = 4000
embedding_batch_size = 32
enable_query_expansion = true
```

---

## Common Performance Issues

### Issue: Chat responses are very slow (> 60s)

**Diagnosis**:
```bash
ollama ps    # Check model size
```

**Solutions**:
1. Switch to smaller model (e.g., `qwen2.5:0.5b`)
2. Reduce RAG context (`chat_context_max_chars = 1000`)
3. Disable query expansion (`enable_query_expansion = false`)
4. Check if model is CPU-bound (consider GPU)

### Issue: Out of memory errors

**Diagnosis**:
```bash
free -h      # Linux
vm_stat      # macOS
```

**Solutions**:
1. Use smaller model
2. Reduce `embedding_batch_size`
3. Unload unused Ollama models: `ollama rm <model>`
4. Increase Docker memory for Cassandra
5. Disable RAG if not needed

### Issue: RAG retrieval is slow (> 3s)

**Diagnosis**:
```bash
# Check Cassandra stats
docker stats nyxgpt-cassandra
```

**Solutions**:
1. Reduce `chat_top_k` (try 3 instead of 5)
2. Increase Cassandra memory allocation
3. Ensure Cassandra has CPU resources
4. Check network latency to Cassandra (should be < 1ms for local)

### Issue: Ingestion is very slow

**Diagnosis**:
```bash
# Monitor during ingestion
ollama ps
docker stats nyxgpt-cassandra
```

**Solutions**:
1. Increase `embedding_batch_size` (try 32)
2. Use faster embedding model
3. Increase `embedding_timeout_seconds`
4. Check Ollama is running: `ollama list`

---

## GPU Acceleration

If you have a compatible GPU, Ollama will automatically use it. Verify:

```bash
ollama ps    # Shows which device model is on
```

**Expected speedup**:
- 5-10x faster inference vs CPU
- Same memory usage (on GPU instead of system RAM)

**Enable GPU in Docker (for Cassandra, optional)**:

Cassandra doesn't need GPU, but if you're running Ollama in Docker:

```bash
docker run --gpus all ...
```

See [Ollama Docker documentation](https://github.com/ollama/ollama/blob/main/docs/docker.md) for details.

---

## Benchmarking

Track performance over time:

```bash
# Simple benchmark
time nyxgpt chat "What is the capital of France?"

# With RAG
time nyxgpt chat "Based on the documentation, how does nyxGPT handle sessions?"

# Measure ingestion
time nyxgpt rag ingest README.md --doc-id readme
```

Create a benchmark script:

```bash
#!/bin/bash
# benchmark.sh

echo "Testing small model..."
echo "default_model = qwen2.5:0.5b" > /tmp/test-config.ini
time nyxgpt chat --config /tmp/test-config.ini "Write a haiku"

echo "Testing medium model..."
echo "default_model = llama3.2:3b" > /tmp/test-config.ini
time nyxgpt chat --config /tmp/test-config.ini "Write a haiku"
```

---

## Additional Resources

- **Configuration Reference**: [`docs/configuration.md`](configuration.md)
- **RAG Documentation**: [`docs/rag.md`](rag.md)
- **Ollama Performance**: https://github.com/ollama/ollama/blob/main/docs/faq.md#how-can-i-optimize-ollama-for-performance
- **Cassandra Tuning**: https://cassandra.apache.org/doc/latest/cassandra/operating/tuning.html

---

## Summary

**Key Performance Levers** (in order of impact):

1. **Model size**: Biggest impact on speed and memory
2. **Caching**: 100-60000x speedup for repeated queries
3. **RAG enabled/disabled**: 100-500ms per request
4. **RAG context size**: Affects prompt length and generation time
5. **Chunk retrieval count**: Affects RAG latency
6. **Batch sizes**: Affects ingestion speed only

**General Rule**: Start with minimal configuration and scale up as needed based on measured performance and quality requirements.

**Caching Best Practices**:
- Always enable embedding cache for RAG workloads
- Use memory cache for development, disk cache for production
- Response cache is best for testing/demos, not production chat
- Monitor cache hit rates with debug logging
- Clear caches after model or configuration changes

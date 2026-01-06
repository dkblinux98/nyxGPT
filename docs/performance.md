# Performance Tuning Guide

This guide covers optimization strategies for myGPT to improve response times, reduce resource usage, and enhance overall system performance.

---

## Quick Performance Checklist

- [ ] Choose appropriate model size for your hardware
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
[mygpt]
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
[mygpt]
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
[mygpt]
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

**Performance impact**: Only affects `mygpt rag ingest` speed, not chat performance.

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
   docker stats mygpt-cassandra
   ```

2. **Increase Docker memory limit** if needed:
   ```bash
   docker update mygpt-cassandra --memory=4g
   ```

3. **Monitor query performance**:
   ```sql
   -- In cqlsh
   TRACING ON;
   SELECT * FROM mygpt.rag_chunks ORDER BY embedding ANN OF [...] LIMIT 5;
   ```

### Keyspace Configuration

For production, consider adjusting replication:

```sql
ALTER KEYSPACE mygpt
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
uvicorn mygpt.app:app --workers 4
```

**Note**: Multiple workers require careful session management due to file locking.

**For local use**: Keep default single worker.

### Cassandra Docker Memory

Increase if you're ingesting large document sets:

```bash
docker run -d \
  --name mygpt-cassandra \
  --memory=4g \
  --cpus=2 \
  -p 9042:9042 \
  -v mygpt_cassandra_data:/var/lib/cassandra \
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
[mygpt]
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
   tail -f ~/.myGPT/logs/mygpt.log | grep "request_id"
   ```

2. **Resource usage**:
   ```bash
   # Ollama
   ollama ps

   # Cassandra
   docker stats mygpt-cassandra

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
tail -f ~/.myGPT/logs/mygpt.log
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
echo "[rag]\nenabled = false" >> ~/.myGPT/config.ini

# Run test query
time mygpt chat "Hello, how are you?"

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
sed -i '' 's/default_model = .*/default_model = qwen2.5:0.5b/' ~/.myGPT/config.ini

# Test
time mygpt chat "Hello, how are you?"

# Compare to baseline
```

### 4. Iterate

Continue tuning until performance meets requirements.

---

## Recommended Configurations

### Minimal Resource (< 8GB RAM, CPU-only)

```ini
[mygpt]
default_model = qwen2.5:0.5b
chat_timeout_seconds = 120

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
[mygpt]
default_model = llama3.2:3b
chat_timeout_seconds = 180

[rag]
enabled = true
chat_top_k = 5
max_chunks = 5
chat_context_max_chars = 2400
embedding_batch_size = 16
```

### High Performance (16GB+ RAM, GPU available)

```ini
[mygpt]
default_model = llama3.1:8b
chat_timeout_seconds = 300

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
docker stats mygpt-cassandra
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
docker stats mygpt-cassandra
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
time mygpt chat "What is the capital of France?"

# With RAG
time mygpt chat "Based on the documentation, how does myGPT handle sessions?"

# Measure ingestion
time mygpt rag ingest README.md --doc-id readme
```

Create a benchmark script:

```bash
#!/bin/bash
# benchmark.sh

echo "Testing small model..."
echo "default_model = qwen2.5:0.5b" > /tmp/test-config.ini
time mygpt chat --config /tmp/test-config.ini "Write a haiku"

echo "Testing medium model..."
echo "default_model = llama3.2:3b" > /tmp/test-config.ini
time mygpt chat --config /tmp/test-config.ini "Write a haiku"
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
2. **RAG enabled/disabled**: 100-500ms per request
3. **RAG context size**: Affects prompt length and generation time
4. **Chunk retrieval count**: Affects RAG latency
5. **Batch sizes**: Affects ingestion speed only

**General Rule**: Start with minimal configuration and scale up as needed based on measured performance and quality requirements.

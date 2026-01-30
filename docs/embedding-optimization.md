# Embedding Generation Optimization

## Overview

This document describes optimizations for embedding generation performance in nyxGPT.

## Optimizations Implemented

### 1. Batch Size Optimization

**Default batch size increased from 16 to 32**

- Reduces API overhead by sending more texts per request
- Better GPU utilization (when available via Ollama)
- Improved throughput for bulk operations

Configuration:
```ini
[rag]
embedding_batch_size = 32
```

**Tuning guidance:**
- Increase for better throughput (up to 64-128 for powerful systems)
- Decrease for memory-constrained environments (down to 8-16)
- Monitor memory usage during large ingestion operations

### 2. Memory-Efficient Iterator Pattern

The `_batched()` helper uses `itertools.islice` for memory-efficient chunking:
- Processes texts in batches without loading all into memory
- Suitable for large document ingestion
- Constant memory usage regardless of input size

### 3. GPU Utilization (via Ollama)

Embedding generation leverages Ollama's GPU support automatically:
- No code changes required in nyxGPT
- Configure Ollama to use GPU for best performance
- Larger batch sizes maximize GPU utilization

## Performance Impact

| Batch Size | Throughput | Memory | Best For |
|------------|------------|--------|----------|
| 8 | Low | Low | Constrained systems |
| 16 | Medium | Medium | Default (old) |
| 32 | High | Medium | Recommended |
| 64 | Very High | High | Powerful systems |
| 128 | Maximum | Very High | Bulk operations |

## Future Optimizations

Planned improvements:
- [ ] Async/await support for concurrent embedding generation
- [ ] Automatic batch size tuning based on available memory
- [ ] Connection pooling for Ollama API
- [ ] Caching of frequently-embedded texts (implemented in #2618)
- [ ] Parallel embedding generation for multiple models

## Configuration Examples

### High Performance (GPU-enabled system)
```ini
[rag]
embedding_batch_size = 64
embedding_timeout_seconds = 180
```

### Balanced (default)
```ini
[rag]
embedding_batch_size = 32
embedding_timeout_seconds = 120
```

### Low Memory
```ini
[rag]
embedding_batch_size = 8
embedding_timeout_seconds = 90
```

## Monitoring

To monitor embedding performance, enable debug logging:

```ini
[logging]
level = DEBUG
```

Look for log messages like:
- "Embedding batch size: X"
- "Embedding time: Xms for Y texts"

## Related Issues

- #2618: Embedding caching (reduces redundant embedding generation)
- #2619: Parallel RAG queries (concurrent embedding for query expansion)
- #2676: This issue - batch size optimization

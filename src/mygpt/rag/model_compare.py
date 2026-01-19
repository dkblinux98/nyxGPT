"""Performance comparison tools for embedding models.

This module provides utilities to compare different embedding models across
multiple dimensions: accuracy, speed, and disk usage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, cast

from mygpt.rag.embeddings import embed_texts, EmbeddingError
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore


@dataclass
class ModelPerformanceMetrics:
    """Performance metrics for an embedding model."""
    model_name: str
    dimension: int
    avg_embedding_time_ms: float
    avg_query_time_ms: float
    avg_recall_at_k: float | None
    disk_usage_mb: float | None


def benchmark_embedding_speed(
    model: str,
    dimension: int,
    test_texts: List[str],
    num_runs: int = 3,
) -> float:
    """Benchmark embedding speed for a model.

    Args:
        model: Embedding model name
        dimension: Expected embedding dimension
        test_texts: List of test texts to embed
        num_runs: Number of runs to average

    Returns:
        Average embedding time in milliseconds
    """
    times = []
    for _ in range(num_runs):
        start = time.perf_counter()
        try:
            embed_texts(test_texts, model=model, dimension=dimension)
            elapsed = (time.perf_counter() - start) * 1000.0
            times.append(elapsed)
        except EmbeddingError as e:
            raise ValueError(f"Failed to embed with model {model}: {e}")

    return sum(times) / len(times) if times else 0.0


def benchmark_query_speed(
    collection: str,
    test_queries: List[str],
    model: str,
    dimension: int,
    k: int = 5,
    num_runs: int = 3,
) -> float:
    """Benchmark query speed for a model.

    Args:
        collection: Collection name to query
        test_queries: List of test queries
        model: Embedding model name
        dimension: Expected embedding dimension
        k: Number of results to retrieve
        num_runs: Number of runs to average

    Returns:
        Average query time in milliseconds
    """
    times = []
    store = CassandraVectorStore(collection=collection)

    try:
        for _ in range(num_runs):
            start = time.perf_counter()
            for query in test_queries:
                # Embed query
                emb_result = embed_texts([query], model=model, dimension=dimension)
                # Type narrowing: emb_result is list[list[float]] (not tuple) since collect_metrics=False
                q_emb = cast(list[list[float]], emb_result)[0] if emb_result else []
                # Query vector store
                store.query_by_embedding(q_emb, k=k, embedding_model=model)
            elapsed = (time.perf_counter() - start) * 1000.0
            times.append(elapsed)
    finally:
        store.close()

    return sum(times) / len(times) if times else 0.0


def compare_models(
    models: List[tuple[str, int, str]],  # (model_name, dimension, collection)
    test_texts: List[str],
    test_queries: List[str] | None = None,
) -> List[ModelPerformanceMetrics]:
    """Compare multiple embedding models.

    Args:
        models: List of (model_name, dimension, collection) tuples
        test_texts: Test texts for embedding speed benchmark
        test_queries: Optional test queries for query speed benchmark

    Returns:
        List of ModelPerformanceMetrics for each model
    """
    results = []

    for model_name, dimension, collection in models:
        print(f"\nBenchmarking {model_name} (dim={dimension}, collection={collection})...")

        # Benchmark embedding speed
        try:
            emb_time = benchmark_embedding_speed(model_name, dimension, test_texts)
            print(f"  Embedding time: {emb_time:.2f} ms")
        except Exception as e:
            print(f"  Embedding failed: {e}")
            emb_time = 0.0

        # Benchmark query speed if test queries provided
        query_time = 0.0
        if test_queries:
            try:
                query_time = benchmark_query_speed(collection, test_queries, model_name, dimension)
                print(f"  Query time: {query_time:.2f} ms")
            except Exception as e:
                print(f"  Query failed: {e}")

        results.append(
            ModelPerformanceMetrics(
                model_name=model_name,
                dimension=dimension,
                avg_embedding_time_ms=emb_time,
                avg_query_time_ms=query_time,
                avg_recall_at_k=None,  # Not implemented yet
                disk_usage_mb=None,  # Not implemented yet
            )
        )

    return results


def print_comparison_table(metrics: List[ModelPerformanceMetrics]) -> None:
    """Print a comparison table of model metrics.

    Args:
        metrics: List of ModelPerformanceMetrics to display
    """
    if not metrics:
        print("No metrics to display.")
        return

    # Find column widths
    model_width = max(len(m.model_name) for m in metrics) + 2
    dim_width = 8

    # Print header
    print("\n" + "=" * 80)
    print(f"{'Model':<{model_width}} {'Dim':<{dim_width}} {'Embed (ms)':<15} {'Query (ms)':<15}")
    print("=" * 80)

    # Print rows
    for m in metrics:
        print(
            f"{m.model_name:<{model_width}} "
            f"{m.dimension:<{dim_width}} "
            f"{m.avg_embedding_time_ms:<15.2f} "
            f"{m.avg_query_time_ms:<15.2f}"
        )

    print("=" * 80)

    # Print summary
    if len(metrics) > 1:
        fastest_emb = min(metrics, key=lambda m: m.avg_embedding_time_ms)
        fastest_query = min(metrics, key=lambda m: m.avg_query_time_ms) if any(m.avg_query_time_ms > 0 for m in metrics) else None

        print(f"\nFastest embedding: {fastest_emb.model_name} ({fastest_emb.avg_embedding_time_ms:.2f} ms)")
        if fastest_query:
            print(f"Fastest query: {fastest_query.model_name} ({fastest_query.avg_query_time_ms:.2f} ms)")

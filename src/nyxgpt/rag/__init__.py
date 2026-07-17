"""Retrieval-Augmented Generation (RAG) subsystem for nyxGPT.

This package implements document ingestion, chunking, embedding, hybrid
(vector + BM25) search, cross-encoder reranking, and result fusion used to
ground model responses in retrieved context. See the individual modules
(`rag`, `embeddings`, `vectorstore_cassandra`, `bm25`, `reranker`, `fusion`,
`code_parser`) for details on each stage of the pipeline.
"""

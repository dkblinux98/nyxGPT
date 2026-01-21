"""BM25 keyword search implementation for hybrid search.

This module implements the BM25 (Best Matching 25) algorithm, a probabilistic
information retrieval function used for ranking documents based on keyword
relevance. BM25 complements vector search by capturing exact keyword matches
and term frequency patterns.

**Why BM25?**

1. **Keyword Precision**: Finds documents with exact term matches that vector
   embeddings might miss due to semantic approximation.

2. **Term Frequency**: Rewards documents that mention query terms frequently,
   capturing topic focus that vectors might dilute.

3. **Complementary to Vectors**: When combined with vector search via reciprocal
   rank fusion, provides both semantic understanding and keyword precision.

**Algorithm Overview:**

BM25 scores documents using:
- Term frequency (TF): How often query terms appear in the document
- Inverse document frequency (IDF): How rare/important each term is
- Document length normalization: Prevents long documents from dominating

Formula:
    score(D, Q) = Σ(IDF(qi) * (f(qi, D) * (k1 + 1)) / (f(qi, D) + k1 * (1 - b + b * |D| / avgdl)))

Where:
- D: document, Q: query
- f(qi, D): frequency of term qi in document D
- |D|: document length, avgdl: average document length
- k1: term frequency saturation parameter (default: 1.5)
- b: length normalization parameter (default: 0.75)
- IDF(qi): inverse document frequency of term qi

**Configuration:**

Controlled by `[rag]` section in config.ini:
- `bm25_k1`: Term frequency saturation (default: 1.5)
- `bm25_b`: Length normalization (default: 0.75)

**Usage Example:**

    >>> from mygpt.rag.bm25 import BM25Index
    >>>
    >>> # Index documents
    >>> docs = ["Python programming guide", "Java tutorial", "Python API reference"]
    >>> index = BM25Index()
    >>> index.build_index(docs)
    >>>
    >>> # Search for relevant documents
    >>> results = index.search("Python API", k=2)
    >>> for doc_idx, score in results:
    ...     print(f"{docs[doc_idx]}: {score:.3f}")
    Python API reference: 1.234
    Python programming guide: 0.876
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from mygpt.config import load_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BM25Config:
    """BM25 algorithm parameters."""

    k1: float  # Term frequency saturation parameter (typically 1.2 to 2.0)
    b: float  # Length normalization parameter (typically 0.75)


def _bm25_cfg() -> BM25Config:
    """Load BM25 configuration from config.ini."""
    cfg = load_config(None)
    k1 = cfg.getfloat("rag", "bm25_k1", fallback=1.5)
    b = cfg.getfloat("rag", "bm25_b", fallback=0.75)
    return BM25Config(k1=k1, b=b)


def tokenize(text: str) -> list[str]:
    """Simple tokenization: lowercase and split on whitespace.

    For production, consider more sophisticated tokenization like:
    - Stemming (e.g., "running" → "run")
    - Stop word removal (e.g., "the", "a", "an")
    - N-grams for phrase matching

    Args:
        text: Input text to tokenize

    Returns:
        List of lowercase tokens
    """
    return text.lower().split()


class BM25Index:
    """In-memory BM25 index for keyword search.

    This class builds and maintains a BM25 index over a corpus of documents.
    It supports efficient keyword search with relevance scoring based on
    term frequency, inverse document frequency, and document length normalization.

    **Index Structure:**

    The index stores:
    - Document term frequencies (how often each term appears per document)
    - Document lengths (for normalization)
    - Inverse document frequencies (term importance scores)
    - Average document length (for length normalization)

    **Performance Notes:**

    - Build time: O(N * M) where N=docs, M=avg tokens per doc
    - Search time: O(Q * D) where Q=query tokens, D=matching docs
    - Memory: O(N * V) where V=vocabulary size

    For very large corpora (>10k docs), consider:
    - Disk-based indices (e.g., Whoosh, Elasticsearch)
    - Approximate search (e.g., inverted index pruning)
    - Caching frequently queried terms

    **Thread Safety:**

    This class is NOT thread-safe. If multiple threads access the same
    instance, use external synchronization (e.g., locks).

    Attributes:
        doc_count: Number of indexed documents
        doc_lengths: List of document lengths (in tokens)
        avg_doc_length: Average document length across corpus
        doc_freqs: Dict mapping terms to number of documents containing them
        term_doc_freqs: Nested dict of term frequencies per document
        cfg: BM25 algorithm parameters (k1, b)
    """

    def __init__(self) -> None:
        """Initialize an empty BM25 index."""
        self.doc_count = 0
        self.doc_lengths: list[int] = []
        self.avg_doc_length = 0.0
        self.doc_freqs: dict[str, int] = {}  # term -> num docs containing it
        self.term_doc_freqs: dict[
            int, dict[str, int]
        ] = {}  # doc_idx -> {term -> count}
        self.cfg = _bm25_cfg()

    def build_index(self, documents: Iterable[str]) -> None:
        """Build BM25 index from a corpus of documents.

        This method processes all documents and builds the internal data
        structures needed for BM25 scoring. It must be called before search().

        **Incremental Updates:**

        Currently, this method rebuilds the entire index from scratch.
        For incremental updates (adding/removing documents), you would need to:
        1. Store document IDs alongside indices
        2. Update doc_freqs when adding/removing terms
        3. Recompute avg_doc_length incrementally

        Args:
            documents: Iterable of document strings to index
        """
        docs_list = list(documents)
        self.doc_count = len(docs_list)
        self.doc_lengths = []
        self.term_doc_freqs = {}
        doc_freq_counter: dict[str, set[int]] = defaultdict(set)

        # First pass: compute term frequencies and document lengths
        for doc_idx, doc in enumerate(docs_list):
            tokens = tokenize(doc)
            self.doc_lengths.append(len(tokens))

            term_counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                term_counts[token] += 1
                doc_freq_counter[token].add(doc_idx)

            self.term_doc_freqs[doc_idx] = dict(term_counts)

        # Compute average document length
        self.avg_doc_length = (
            sum(self.doc_lengths) / self.doc_count if self.doc_count > 0 else 0.0
        )

        # Compute document frequencies (number of docs containing each term)
        self.doc_freqs = {
            term: len(doc_set) for term, doc_set in doc_freq_counter.items()
        }

    def _idf(self, term: str) -> float:
        """Compute IDF (Inverse Document Frequency) for a term.

        IDF measures how important a term is across the corpus. Rare terms
        get higher scores than common terms.

        Formula:
            IDF(t) = log((N - df(t) + 0.5) / (df(t) + 0.5))

        Where:
        - N: total number of documents
        - df(t): number of documents containing term t
        - +0.5 smoothing prevents division by zero

        Args:
            term: The term to compute IDF for

        Returns:
            IDF score (higher = more important/rare term)
        """
        df = self.doc_freqs.get(term, 0)
        # IDF formula with smoothing to prevent negative values
        return math.log((self.doc_count - df + 0.5) / (df + 0.5) + 1.0)

    def _score_doc(self, doc_idx: int, query_tokens: list[str]) -> float:
        """Compute BM25 score for a document given query tokens.

        This is the core BM25 scoring function that combines:
        - Term frequency (how often query terms appear)
        - IDF (how important each term is)
        - Length normalization (prevents long docs from dominating)

        Args:
            doc_idx: Index of the document to score
            query_tokens: List of tokenized query terms

        Returns:
            BM25 score (higher = more relevant)
        """
        if doc_idx not in self.term_doc_freqs:
            return 0.0

        score = 0.0
        doc_len = self.doc_lengths[doc_idx]
        term_freqs = self.term_doc_freqs[doc_idx]

        for term in query_tokens:
            if term not in term_freqs:
                continue

            tf = term_freqs[term]
            idf = self._idf(term)

            # BM25 formula
            numerator = tf * (self.cfg.k1 + 1)
            denominator = tf + self.cfg.k1 * (
                1 - self.cfg.b + self.cfg.b * doc_len / self.avg_doc_length
            )

            score += idf * (numerator / denominator)

        return score

    def search(self, query: str, k: int = 10) -> list[tuple[int, float]]:
        """Search the index for documents matching the query.

        Returns the top-k most relevant documents ranked by BM25 score.

        **Empty Results:**

        An empty list is returned if:
        - Query contains no valid tokens after tokenization
        - No documents in the index contain any query terms
        - Index is empty (build_index not called)

        **Score Interpretation:**

        BM25 scores are not normalized and have no fixed range. Typical values:
        - 0.0: No matching terms
        - 1-5: Weak match (few terms or low frequency)
        - 5-15: Strong match (multiple terms, high frequency)
        - 15+: Very strong match (many terms, repeated frequently)

        Scores are relative within a query, not comparable across different queries.

        Args:
            query: Search query string
            k: Maximum number of results to return

        Returns:
            List of (doc_idx, score) tuples, sorted by score descending.
            Limited to top k results.
        """
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # Score all documents
        scores: list[tuple[int, float]] = []
        for doc_idx in range(self.doc_count):
            score = self._score_doc(doc_idx, query_tokens)
            if score > 0:
                scores.append((doc_idx, score))

        # Sort by score descending and return top k
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]

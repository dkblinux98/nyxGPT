"""Unit tests for the shared collection-name validation rule (#3460).

`nyxgpt.rag.vectorstore_cassandra.COLLECTION_NAME_PATTERN` and
`max_collection_name_length()` are the single source of truth for what
collection names are legal, consumed directly by the create-collection API
endpoint (`nyxgpt.app.rag_collection_create`) and mirrored by the web UI. No
Cassandra connection is needed for these -- `max_collection_name_length`
only reads config, it doesn't touch a Session.
"""

from __future__ import annotations

import pytest

from nyxgpt.rag.vectorstore_cassandra import (
    CASSANDRA_IDENTIFIER_MAX_LEN,
    COLLECTION_NAME_PATTERN,
    max_collection_name_length,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["my_collection", "collection1", "a", "ABC_123"])
def test_pattern_accepts_alphanumeric_and_underscores(name: str) -> None:
    assert COLLECTION_NAME_PATTERN.match(name)


@pytest.mark.parametrize("name", ["my-collection", "test-collection", "has space", "bad!", ""])
def test_pattern_rejects_hyphens_spaces_and_punctuation(name: str) -> None:
    assert not COLLECTION_NAME_PATTERN.match(name)


def test_max_collection_name_length_derives_from_base_table_and_identifier_cap() -> None:
    assert (
        max_collection_name_length("rag_chunks")
        == CASSANDRA_IDENTIFIER_MAX_LEN - len("rag_chunks") - 1
    )


def test_max_collection_name_length_shrinks_with_longer_base_table() -> None:
    short = max_collection_name_length("rag_chunks")
    long = max_collection_name_length("a_much_longer_base_table_name")
    assert long < short

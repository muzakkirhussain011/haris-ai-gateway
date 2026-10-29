"""Upstream behaviour the Chroma backend is designed around. Raw chromadb calls only.

In chromadb 1.5.x (local mode), deleting a collection leaves its array metadata behind and SQLite
reuses record ids, so new records in other collections come back with those arrays and `where`
filters use them (chroma-core/chroma#7594). Scalars stay isolated. wardowl's
Chroma backend therefore never stores arrays (see wardowl/backends/chroma.py). This test is
expected to fail while the upstream behaviour persists; if it starts passing, Chroma has fixed it.

Chroma 1.5 also writes each `$or` as a chain of binary ORs, one per clause, and SQLite refuses an
expression more than 1,000 deep, so a flat `$or` of 1,001 clauses is refused while the same clauses
nested 16 to an `$or` are not. The backend therefore nests them. Only clause counts that Chroma
refuses with an error are probed here: a flat `$or` of about 40,000 clauses ended the process with
SIGSEGV in the paper's filter-size experiment.
"""

from __future__ import annotations

import uuid

import pytest

chromadb = pytest.importorskip("chromadb")
VECTOR = [[1.0, 0.0, 0.0]]


def _new(client):
    return client.create_collection(f"probe_{uuid.uuid4().hex}", embedding_function=None)


def _seen(collection):
    q = collection.query(query_embeddings=VECTOR, n_results=1, include=["metadatas", "documents"])
    g = collection.get(ids=["x"], include=["metadatas", "documents"])
    return {"query": (q["documents"][0], q["metadatas"][0]), "get": (g["documents"], g["metadatas"])}


@pytest.mark.xfail(reason="chroma-core/chroma#7594: deleted collections leave array metadata behind", strict=False)
@pytest.mark.parametrize("delete_first", [False, True])
def test_probe_array_metadata_stays_in_its_own_collection(delete_first):
    client = chromadb.EphemeralClient()
    a = _new(client)
    a.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"groups": ["A", "*"]}], documents=["from A"])
    if delete_first:
        client.delete_collection(a.name)
    b = _new(client)
    b.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"h": "B"}], documents=["from B"])
    seen = _seen(b)
    assert seen == {
        "query": (["from B"], [{"h": "B"}]),
        "get": (["from B"], [{"h": "B"}]),
    }, f"chromadb {chromadb.__version__}: collection B returned {seen!r}"


def _one_record_and_1001_clauses():
    collection = _new(chromadb.EphemeralClient())
    collection.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"k0000": True}], documents=["x"])
    return collection, [{f"k{i:04d}": {"$eq": True}} for i in range(1001)]


@pytest.mark.xfail(reason="SQLite refuses an expression more than 1,000 deep: one OR per clause", strict=False)
def test_probe_a_flat_or_of_1001_clauses_is_accepted():
    collection, clauses = _one_record_and_1001_clauses()
    assert collection.get(where={"$or": clauses})["ids"] == ["x"]


def test_probe_the_same_clauses_nested_16_to_an_or_are_accepted():
    collection, clauses = _one_record_and_1001_clauses()
    nested = {"$or": [{"$or": clauses[i : i + 16]} for i in range(0, len(clauses), 16)]}
    assert collection.get(where=nested)["ids"] == ["x"]

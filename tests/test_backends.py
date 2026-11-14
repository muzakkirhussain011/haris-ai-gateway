"""Every backend must agree with the reference semantics and never leak through the retriever.

The in-memory backend always runs. pgvector runs when WARDOWL_TEST_DATABASE_URL points at a
PostgreSQL with pgvector; Chroma runs when chromadb is installed. CI runs all three.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers import (
    QUERY,
    HashingEmbedder,
    chroma_where_matches,
    corpora,
    expected_ids,
    filters,
    labelled_docs,
    may_read,
    queries,
    subjects,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wardowl import (
    Document,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    SearchError,
    Subject,
    matches,
)
from wardowl.backends import StaleRevision
from wardowl.testing import QUERY as KIT_QUERY
from wardowl.testing import check_backend

EMBED = HashingEmbedder()
DATABASE_URL = os.environ.get("WARDOWL_TEST_DATABASE_URL")
EXAMPLES = settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(scope="module")
def pgvector_pool():
    if not DATABASE_URL:
        pytest.skip("set WARDOWL_TEST_DATABASE_URL to run the pgvector tests")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    pool = psycopg_pool.ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=True)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def pgvector_factory(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    # No HNSW index here: exact search, so results can be compared with the reference exactly.
    create_schema(pgvector_pool, EMBED.dim, table="wo_conformance", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, EMBED, table="wo_conformance", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_conformance")
        backend.add(docs)
        return backend

    return make


@pytest.fixture(scope="module")
def chroma_factory():
    chromadb = pytest.importorskip("chromadb")
    from wardowl.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()
    previous: list[str] = []

    def make(docs):
        # A new, uniquely named collection per example keeps examples independent.
        for name in previous:
            with contextlib.suppress(Exception):
                client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
        collection = client.create_collection(previous[0], embedding_function=None)
        backend = ChromaBackend(collection, EMBED)
        backend.add(docs)
        return backend

    return make


@pytest.fixture(params=["memory", "pgvector", "chroma"])
def factory(request):
    if request.param == "memory":
        return InMemoryBackend
    return request.getfixturevalue(f"{request.param}_factory")


@EXAMPLES
@given(docs=labelled_docs(), f=filters)
def test_backend_returns_exactly_the_documents_the_filter_matches(factory, docs, f):
    hits = factory(docs).search(QUERY, len(docs) + 5, f)
    ids = [h.id for h in hits]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected_ids(docs, f)


@EXAMPLES
@given(
    docs=corpora(max_size=15),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_retriever_on_backend_never_leaks_and_says_when_it_is_complete(factory, docs, subject, query, k, strategy):
    backend = factory(docs)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy=strategy, max_candidates=len(docs) + k)
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    assert result.violations == 0
    # Post-filtering searches without a filter here (MetadataACLAuthorizer has no scope), so running
    # out proves the end of the store. A short pre-filtered result proves nothing more matches only
    # when the backend is exact: pgvector (here without an index) and Chroma do not declare it.
    exact = getattr(backend, "exact_search", False) is True
    assert result.complete is (exact or result.strategy == "postfilter" or len(result.hits) == k)
    if not isinstance(backend, InMemoryBackend):
        # Vector search ranks every stored chunk, so the subject gets min(k, everything permitted).
        assert len(result.hits) == min(k, sum(may_read(subject, d) for d in docs))


def test_backend_passes_the_public_conformance_kit(factory):
    # The checks third-party adapters run (wardowl.testing), on every built-in backend.
    check_backend(factory)


# ---- Chroma specifics ------------------------------------------------------------------------


def test_chroma_chunk_without_permissions_never_inherits_another_chunks(chroma_factory):
    # Regression: with None metadata, Chroma returned a new record carrying metadata from an
    # earlier record with the same id, which would make a chunk readable by everyone.
    chroma_factory([Document("doc-000", "policy", {"allowed_groups": ["*"]})])
    backend = chroma_factory([Document("doc-000", "policy", {})])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    assert retriever.retrieve("policy", Subject("alice"), k=5).hits == []
    assert backend.search("policy", 5) and backend.search("policy", 5)[0].metadata == {}


def test_chroma_set_metadata_never_recreates_a_deleted_chunk_nor_restores_old_text(chroma_factory):
    from wardowl.backends.chroma import ChromaBackend

    eng = {"allowed_groups": ["eng"]}
    backend = chroma_factory([Document("a", "leave plan", eng), Document("b", "old text", eng)])
    collection = backend.collection

    class Interleaved:
        """The collection, with another writer's changes landing just after set_metadata reads."""

        def __getattr__(self, name):
            return getattr(collection, name)

        def get(self, **kwargs):
            stored = collection.get(**kwargs)
            backend.delete(["a"])
            backend.add([Document("b", "new text", eng)])
            return stored

    ChromaBackend(Interleaved(), EMBED).set_metadata({"a": {"allowed_groups": ["*"]}, "b": {"allowed_groups": ["*"]}})
    hits = {h.id: (h.text, h.metadata) for h in backend.search("leave plan text", 5)}
    assert hits == {"b": ("new text", {"allowed_groups": ["*"]})}
    assert backend.search("text", 5, In("allowed_groups", ["eng"])) == []  # the old list field is gone


def test_chroma_search_drops_a_hit_only_a_stale_list_field_matches(chroma_factory):
    from wardowl.backends.chroma import ChromaBackend

    backend = chroma_factory([Document("a", "leave plan", {"allowed_groups": ["hr"]})])
    collection = backend.collection

    class Interleaved:
        """The collection, with another writer's add landing between set_metadata's read and update."""

        def __getattr__(self, name):
            return getattr(collection, name)

        def get(self, **kwargs):
            stored = collection.get(**kwargs)
            backend.add([Document("a", "leave plan", {"allowed_groups": ["eng"]})])
            return stored

    ChromaBackend(Interleaved(), EMBED).set_metadata({"a": {"allowed_groups": ["sales"]}})
    # The update knew only the list field it read, so the one the add wrote is left, and Chroma matches it.
    (fields,) = collection.get(ids=["a"], include=["metadatas"])["metadatas"]
    assert fields.get("allowed_groups::s:eng") is True
    assert backend.search("leave plan", 5, In("allowed_groups", ["eng"])) == []
    (hit,) = backend.search("leave plan", 5, In("allowed_groups", ["sales"]))
    assert hit.id == "a" and hit.metadata == {"allowed_groups": ["sales"]}


class SmallBatches:
    """A Chroma collection that refuses, as Chroma does, a call with more records than its maximum."""

    def __init__(self, maximum):
        self._client = SimpleNamespace(get_max_batch_size=lambda: maximum)  # where Chroma reports it
        self.maximum = maximum
        self.records = {}
        self.calls = []

    def _sized(self, name, ids):
        self.calls.append((name, len(ids)))
        if len(ids) > self.maximum:
            raise ValueError(f"Batch size {len(ids)} exceeds maximum batch size {self.maximum}")

    def add(self, ids, documents, metadatas, embeddings=None):
        self._sized("add", ids)
        for doc_id, text, metadata in zip(ids, documents, metadatas, strict=True):
            self.records[doc_id] = (text, dict(metadata))

    def delete(self, ids):
        self._sized("delete", ids)
        for doc_id in ids:
            self.records.pop(doc_id, None)

    def get(self, ids, include):
        self._sized("get", ids)
        found = [doc_id for doc_id in ids if doc_id in self.records]
        return {"ids": found, "metadatas": [self.records[doc_id][1] for doc_id in found]}

    def update(self, ids, metadatas):
        self._sized("update", ids)
        for doc_id, new in zip(ids, metadatas, strict=True):
            text, old = self.records[doc_id]
            self.records[doc_id] = (text, {k: v for k, v in {**old, **new}.items() if v is not None})


def test_chroma_writes_in_batches_no_larger_than_the_clients_maximum():
    from wardowl.backends.chroma import JSON_KEY, ChromaBackend

    collection = SmallBatches(maximum=3)
    backend = ChromaBackend(collection, EMBED)
    assert backend.batch_size == 3  # read from the collection's client
    docs = [Document(f"d{i}", f"text {i}", {"allowed_groups": ["eng"]}) for i in range(10)]
    backend.add(docs)
    assert set(collection.records) == {d.id for d in docs}
    backend.set_metadata({**{d.id: {"allowed_groups": ["hr"]} for d in docs}, "not-stored": {}})
    stored = [metadata for _, metadata in collection.records.values()]
    assert all(
        json.loads(m[JSON_KEY]) == {"allowed_groups": ["hr"]} and "allowed_groups::s:eng" not in m for m in stored
    )
    backend.delete([*(d.id for d in docs), "not-stored"])
    assert collection.records == {}
    assert {name for name, _ in collection.calls} == {"add", "delete", "get", "update"}
    assert max(size for _, size in collection.calls) == 3
    # Every record is checked before the first batch is written.
    with pytest.raises(ValueError):
        backend.add([*docs, Document("bad", "text", {"a::b": 1})])
    assert collection.records == {}
    assert ChromaBackend(collection, batch_size=2).batch_size == 2
    assert ChromaBackend(SimpleNamespace()).batch_size == 1000  # a collection that reports no maximum
    with pytest.raises(ValueError):
        ChromaBackend(collection, batch_size=0)


def test_chroma_takes_more_records_than_its_maximum_batch_with_the_defaults(chroma_factory):
    backend = chroma_factory([])
    maximum = backend.collection._client.get_max_batch_size()
    assert backend.batch_size == maximum  # read from the client
    docs = [Document(f"d{i:05d}", "leave", {"allowed_groups": ["eng"]}) for i in range(maximum + 1)]
    backend.add(docs)  # in one call, Chroma would refuse it
    assert backend.collection.count() == maximum + 1
    backend.set_metadata({d.id: {"allowed_groups": ["hr"]} for d in docs})
    assert backend.search("leave", 5, In("allowed_groups", ["eng"])) == []
    assert len(backend.search("leave", 5, In("allowed_groups", ["hr"]))) == 5
    backend.delete([d.id for d in docs])
    assert backend.collection.count() == 0


class SearchableChroma:
    """Enough of a Chroma collection to search: it answers with the stored records a `where` admits,
    in order of id and all at distance 0.5, and keeps each query's `where` and `n_results`."""

    def __init__(self):
        self.records = {}
        self.queries = []

    def count(self):
        return len(self.records)

    def add(self, ids, documents, metadatas, embeddings=None):
        for doc_id, text, metadata in zip(ids, documents, metadatas, strict=True):
            self.records[doc_id] = (text, dict(metadata))

    def delete(self, ids):
        for doc_id in ids:
            self.records.pop(doc_id, None)

    def query(self, n_results, include, where=None, **embedded):
        self.queries.append({"where": where, "n_results": n_results})
        found = [i for i in sorted(self.records) if where is None or chroma_where_matches(where, self.records[i][1])]
        found = found[:n_results]
        return {
            "ids": [found],
            "documents": [[self.records[i][0] for i in found]],
            "metadatas": [[self.records[i][1] for i in found]],
            "distances": [[0.5] * len(found)],
        }


def test_chroma_never_sends_a_where_too_large_and_applies_the_filter_itself():
    # Chroma refused the flat where of a subject in 1,000 groups, and the process died (SIGSEGV) on
    # that of a subject in 40,000. A where beyond the bounds is never sent: the search sends the
    # part every match must meet, if one fits, and applies the whole filter to the candidates.
    from wardowl import And
    from wardowl.backends.chroma import ChromaBackend, filter_to_where

    held = [f"group-{i:05d}" for i in range(40_000)]
    collection = SearchableChroma()
    backend = ChromaBackend(collection, EMBED)
    backend.add(
        [
            Document("a1", "doc", {"tenant": "acme", "allowed_groups": [held[5]]}),
            Document("a2", "doc", {"tenant": "acme", "allowed_groups": ["other"]}),
            Document("a3", "doc", {"tenant": "acme", "allowed_groups": ["other", held[-1]]}),
            Document("g1", "doc", {"tenant": "globex", "allowed_groups": [held[5]]}),
        ]
    )
    tenant, groups = In("tenant", ["acme"]), In("allowed_groups", held)
    assert [h.id for h in backend.search(QUERY, 10, And(tenant, groups))] == ["a1", "a3"]
    assert collection.queries == [{"where": filter_to_where(tenant), "n_results": 1000}]
    # Nothing every match must meet fits: no where, and never more than k hits.
    collection.queries.clear()
    assert [h.id for h in backend.search(QUERY, 1, groups)] == ["a1"]
    assert collection.queries == [{"where": None, "n_results": 1000}]
    # fallback_candidates sets how many candidates are read, k if more.
    collection.queries.clear()
    fewer = ChromaBackend(collection, EMBED, fallback_candidates=2)
    assert [h.id for h in fewer.search(QUERY, 1, groups)] == ["a1"]
    assert [h.id for h in fewer.search(QUERY, 3, groups)] == ["a1", "a3"]
    assert [q["n_results"] for q in collection.queries] == [2, 3]
    with pytest.raises(ValueError, match="fallback_candidates"):
        ChromaBackend(collection, fallback_candidates=0)
    # A where that fits is sent whole, for k hits, as before.
    collection.queries.clear()
    few = In("allowed_groups", held[:20])
    assert [h.id for h in backend.search(QUERY, 10, few)] == ["a1", "g1"]
    assert collection.queries == [{"where": filter_to_where(few), "n_results": 10}]


def test_chroma_records_whose_json_copy_is_not_a_json_object_carry_no_permissions():
    # A record another tool wrote with a non-JSON wardowl_metadata failed every search that returned
    # it; like a record without the copy, it is now read as carrying no permissions.
    from wardowl.backends.chroma import JSON_KEY, ChromaBackend

    collection = SearchableChroma()
    backend = ChromaBackend(collection, EMBED)
    everyone = {"allowed_groups": ["*"]}
    backend.add([Document("a", "doc", everyone)])
    stored = [{JSON_KEY: "{not json", "allowed_groups::s:*": True}, {JSON_KEY: "[1]", "allowed_groups::s:*": True}]
    collection.add(ids=["broken", "listed"], documents=["doc", "doc"], metadatas=stored)
    assert {h.id: h.metadata for h in backend.search(QUERY, 5)} == {"a": everyone, "broken": {}, "listed": {}}
    # Chroma matches their list keys, but their true metadata cannot be read, so they are dropped.
    assert [h.id for h in backend.search(QUERY, 5, In("allowed_groups", ["*"]))] == ["a"]
    for strategy in ("prefilter", "postfilter"):
        retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy=strategy)
        result = retriever.retrieve(QUERY, Subject("ann"), 5)
        assert [h.id for h in result.hits] == ["a"] and result.violations == 0


def test_chroma_records_written_by_needtoknow_1x_raise_instead_of_being_denied_one_by_one():
    # needtoknow 1.x kept the JSON copy under needtoknow_metadata. Read as records without a copy,
    # every one was denied, and a pre-filtered search found nothing, with no error.
    from wardowl.backends.chroma import ChromaBackend

    collection = SearchableChroma()
    backend = ChromaBackend(collection, EMBED)
    backend.add([Document("a", "doc", {"allowed_groups": ["hr"]})])
    legacy = {"needtoknow_metadata": '{"allowed_groups": ["hr"]}', "allowed_groups::s:hr": True}
    collection.add(ids=["old"], documents=["doc"], metadatas=[legacy])
    for f in (None, In("allowed_groups", ["hr"])):
        with pytest.raises(ValueError, match=r"needtoknow 1\.x.*upgrading"):
            backend.search(QUERY, 5, f)
    with pytest.raises(SearchError) as raised:
        PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve(QUERY, Subject("ann", ["hr"]), 5)
    assert "needtoknow_metadata" in str(raised.value.__cause__)
    # A chunk wardowl wrote whose metadata has a key of that name is wardowl's.
    backend.delete(["old"])
    backend.add([Document("b", "doc", {"allowed_groups": ["hr"], "needtoknow_metadata": "notes"})])
    assert {h.id for h in backend.search(QUERY, 5, In("allowed_groups", ["hr"]))} == {"a", "b"}


def test_chroma_reads_foreign_and_legacy_records_as_it_returns_them(chroma_factory):
    # The same two cases against Chroma itself.
    from wardowl.backends.chroma import JSON_KEY

    backend = chroma_factory([Document("a", "leave plan", {"allowed_groups": ["*"]})])
    vector = [EMBED("leave plan")]
    broken = {JSON_KEY: "{not json", "allowed_groups::s:*": True}
    backend.collection.add(ids=["broken"], documents=["leave plan"], embeddings=vector, metadatas=[broken])
    assert {h.id: h.metadata for h in backend.search("leave plan", 5)} == {"a": {"allowed_groups": ["*"]}, "broken": {}}
    assert [h.id for h in backend.search("leave plan", 5, In("allowed_groups", ["*"]))] == ["a"]
    legacy = {"needtoknow_metadata": '{"allowed_groups": ["*"]}', "allowed_groups::s:*": True}
    backend.collection.add(ids=["old"], documents=["leave plan"], embeddings=vector, metadatas=[legacy])
    with pytest.raises(ValueError, match=r"needtoknow 1\.x"):
        backend.search("leave plan", 5, In("allowed_groups", ["*"]))


class RecordedWheres:
    """A Chroma collection that keeps the `where` of every query."""

    def __init__(self, collection):
        self.collection = collection
        self.wheres = []

    def count(self):
        return self.collection.count()

    def query(self, **kwargs):
        self.wheres.append(kwargs.get("where"))
        return self.collection.query(**kwargs)


@pytest.mark.parametrize("count", [1_001, 40_000])
def test_chroma_answers_a_subject_in_thousands_of_groups(chroma_factory, count):
    # Chroma 1.5 writes an $or as one binary OR per clause, and SQLite refuses an expression more
    # than 1,000 deep. With the flat $or wardowl used to send, every search of a subject in 1,001
    # groups failed, and the first search of a subject in 40,000 groups ended the process with
    # SIGSEGV (chromadb 1.5.9, the paper's filter-size experiment). Both take well under a second.
    from wardowl.backends.chroma import filter_to_where

    held = [f"group-{i:05d}" for i in range(count)]
    step = count // 20
    docs = [Document(f"in-{i:02d}", "doc", {"allowed_groups": [held[i * step]]}) for i in range(20)]
    docs += [Document(f"out-{i:02d}", "doc", {"allowed_groups": [f"other-{i}"]}) for i in range(10)]
    docs += [
        Document("mixed", "doc", {"allowed_groups": ["other", held[-1]]}),
        Document("scalar", "doc", {"allowed_groups": held[1]}),
        Document("user", "doc", {"allowed_users": ["alice"]}),
        Document("nobody", "doc", {}),
    ]
    backend = chroma_factory(docs)
    backend.collection = recorder = RecordedWheres(backend.collection)
    subject = Subject("alice", held)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve(QUERY, subject, len(docs))
    assert {h.id for h in result.hits} == {f"in-{i:02d}" for i in range(20)} | {"mixed", "scalar", "user"}
    assert result.violations == 0
    (where,) = recorder.wheres
    if count == 1_001:  # sent whole, as a tree of nodes of at most 16 clauses
        assert where == filter_to_where(MetadataACLAuthorizer().filter_for(subject))
    else:  # far too large to send: Chroma got no where, and the backend applied the filter
        assert where is None


@EXAMPLES
@given(docs=labelled_docs(), f=filters)
def test_chroma_reads_nested_where_trees_as_the_reference(chroma_factory, docs, f):
    # The backend nests $and and $or 16 clauses to a node. With 2 to a node, every filter here
    # becomes a tree several levels deep, and Chroma itself must find exactly the records it matches.
    from wardowl.backends import chroma

    backend = chroma_factory(docs)
    with mock.patch.object(chroma, "_FANOUT", 2):
        where = chroma._where(f)
    found = set() if where is None else set(backend.collection.get(where=where, include=["metadatas"])["ids"])
    assert found == expected_ids(docs, f)


def test_the_in_memory_backend_deletes_chunks_and_ignores_ids_it_does_not_hold():
    backend = InMemoryBackend([Document("a", "leave plan", {}), Document("b", "leave rota", {})])
    backend.delete(["a", "not-stored"])
    assert [h.id for h in backend.search("leave", 5)] == ["b"] and len(backend) == 1
    backend.delete([])
    assert len(backend) == 1


# ---- pgvector specifics ----------------------------------------------------------------------


def test_pgvector_iterative_scan_fills_k_under_a_very_selective_filter(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_hnsw", hnsw=True)
    with pgvector_pool.connection() as conn:
        conn.execute("TRUNCATE wo_hnsw")
    docs = [Document(f"hr-{i:03d}", f"leave policy note {i}", {"allowed_groups": ["hr"]}) for i in range(400)]
    docs += [Document(f"bob-{i}", f"leave policy note bob {i}", {"allowed_users": ["bob"]}) for i in range(5)]
    backend = PgVectorBackend(pgvector_pool, EMBED, table="wo_hnsw")
    backend.add(docs)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve("leave policy", Subject("bob"), k=5)
    # About 1% of rows are readable. Whether the planner uses the HNSW index (with an iterative
    # scan) or a sequential scan, all 5 must come back; without iterative scans an index scan
    # could stop short.
    assert sorted(h.id for h in result.hits) == [f"bob-{i}" for i in range(5)]


def test_pgvector_sends_many_values_as_two_array_parameters():
    from wardowl.backends.pgvector import MANY_VALUES, filter_to_sql

    sql, params = filter_to_sql(In("allowed_groups", [f"g{i}" for i in range(MANY_VALUES + 1)]))
    assert sql.count("%s") == 4 and len(params) == 4
    assert params[1][0] == '"g0"' and params[3][0] == '["g0"]'  # values keep their JSON type
    ints_sql, ints_params = filter_to_sql(In("level", range(MANY_VALUES + 1)))
    assert ints_sql.count("%s") == len(ints_params) == 6 and ints_params[1][1] == "1" and ints_params[3][1] == "[1]"
    assert ints_params[5] == [str(i) for i in range(MANY_VALUES + 1)]  # the text each integer must read as
    few_sql, few_params = filter_to_sql(In("allowed_groups", ["hr", "eng"]))
    assert len(few_params) == 4 and few_sql.count("metadata @>") == 4  # GIN-friendly containment


def test_pgvector_matches_integers_only_where_the_reference_does(pgvector_factory):
    from wardowl.backends.pgvector import MANY_VALUES

    # jsonb compares numbers by value, so containment alone finds 1.0 for 1; the reference does not.
    docs = [
        Document("int", "leave", {"level": 1}),
        Document("float", "leave", {"level": 1.0}),
        Document("listed-int", "leave", {"level": [1.0, 1]}),
        Document("listed-float", "leave", {"level": [1.0, 2.5]}),
        Document("string", "leave", {"level": "1"}),
        Document("nested", "leave", {"level": [[1]]}),
        Document("object", "leave", {"level": {"a": 1}}),
        Document("bool", "leave", {"level": True}),
    ]
    backend = pgvector_factory(docs)
    many = list(range(2, MANY_VALUES + 5))
    for f in (In("level", [1]), In("level", [1, "x"]), In("level", [1, *many]), In("level", ["1"])):
        assert {h.id for h in backend.search("leave", 10, f)} == {d.id for d in docs if matches(f, d.metadata)}, f


def test_pgvector_writes_whole_floats_with_a_fraction_as_jsonb_drops_an_exponent():
    from wardowl.backends.pgvector import _metadata_json

    big = 1.2345678901234568e16  # json.dumps writes it with an exponent, which jsonb stores as an integer
    metadata = {"owner": big, "ids": [1e16, 7, -2e300], "acl": {"owner": [big], "1": 2.5e-7}, "flag": True}
    text = _metadata_json("d", metadata)
    assert "e+" not in text and "12345678901234568.0" in text
    loaded = json.loads(text)
    assert json.dumps(loaded) == json.dumps(metadata)  # the same values, each of the same type
    assert type(loaded["owner"]) is float and type(loaded["ids"][1]) is int
    # Everything else is written as json.dumps writes it, a nested object's keys that are not strings included.
    other = {"team": "hr", "level": 1.0, "notes": ["é", None, 0.5], "acl": {1: "x", None: [True]}}
    assert json.loads(_metadata_json("d", other)) == json.loads(json.dumps(other))
    with pytest.raises(TypeError):
        _metadata_json("d", {"tags": {"hr"}})


def test_pgvector_keeps_whole_floats_apart_from_integers(pgvector_factory):
    from wardowl.backends.pgvector import MANY_VALUES

    big = 12345678901234568
    docs = [
        Document("int", "leave", {"owner": big}),
        Document("float", "leave", {"owner": float(big)}),
        Document("listed-float", "leave", {"owner": [float(big), 1e300]}),
        Document("nested", "leave", {"owner": {"id": float(big)}}),
    ]
    backend = pgvector_factory(docs)
    for f in (In("owner", [big]), In("owner", [big, *range(MANY_VALUES + 1)])):
        assert {d.id for d in docs if matches(f, d.metadata)} == {"int"}
        assert {h.id for h in backend.search("leave", 10, f)} == {"int"}, f
    # Read back with the type each value was written with, so the retriever's re-check agrees too.
    stored = {h.id: json.dumps(h.metadata) for h in backend.search("leave", 10)}
    assert stored == {d.id: json.dumps(d.metadata) for d in docs}
    backend.set_metadata({"int": {"owner": float(big)}})
    assert backend.search("leave", 10, In("owner", [big])) == []


def test_pgvector_refuses_metadata_keys_that_json_would_turn_into_strings(pgvector_factory):
    backend = pgvector_factory([Document("a", "leave", {"level": 1})])
    with pytest.raises(ValueError, match="keys must be strings"):
        backend.add([Document("b", "leave", {1: "x"})])
    with pytest.raises(ValueError, match="keys must be strings"):
        backend.set_metadata({"a": {1: "x"}})
    assert [h.metadata for h in backend.search("leave", 5)] == [{"level": 1}]


def test_pgvector_serves_a_subject_in_forty_thousand_groups(pgvector_factory):
    # PostgreSQL accepts at most 65,535 bind parameters: one or two per group would fail here.
    docs = [
        Document("mine", "leave policy", {"allowed_groups": ["g39999"]}),
        Document("public", "leave policy", {"allowed_groups": ["*"]}),
        Document("other", "leave policy", {"allowed_groups": ["hr"]}),
    ]
    backend = pgvector_factory(docs)
    subject = Subject("ann", [f"g{i}" for i in range(40_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    assert sorted(h.id for h in result.hits) == ["mine", "public"] and result.violations == 0


@pytest.mark.parametrize("bad", [0, 1001, 1.5, True, "100"])
def test_pgvector_ef_search_must_be_an_integer_from_1_to_1000(bad):
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError, match="ef_search"):
        PgVectorBackend(None, EMBED, ef_search=bad)


def test_pgvector_ef_search_holds_for_the_search_only(pgvector_pool):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend

    class Probe(PgVectorBackend):
        # Returns the setting the search query sees, in place of rows.
        def _query(self, query_vector, k, f):
            return sql.SQL("SELECT current_setting('hnsw.ef_search'), '', NULL, 0.0"), []

    [hit] = Probe(pgvector_pool, EMBED, table="wo_hnsw", ef_search=123).search("anything", 1)
    assert hit.id == "123"
    # SET LOCAL ends with the search's transaction: the pooled connection is left as it was.
    with pgvector_pool.connection() as conn:
        assert conn.execute("SELECT current_setting('hnsw.ef_search', true)").fetchone()[0] != "123"


class Recorder:
    """A psycopg pool in place of a server: it records each statement as PostgreSQL would receive
    it, with its parameters, and answers every query with `rows`."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    @contextlib.contextmanager
    def connection(self):
        yield self

    @contextlib.contextmanager
    def transaction(self):
        yield

    def execute(self, statement, params=None):
        self.statements.append((statement.as_string(), params))
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class AsyncRecorder(Recorder):
    """The same, in place of an `AsyncConnectionPool`."""

    @contextlib.asynccontextmanager
    async def connection(self):
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, statement, params=None):
        return super().execute(statement, params)

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0]


ROW = ("d1", "leave policy", {"allowed_groups": ["hr"]}, 0.25)  # id, content, metadata, distance


def test_pgvector_sets_its_scan_limits_in_each_search_and_reads_every_row_for_an_exact_one():
    pytest.importorskip("psycopg")
    from wardowl.backends.pgvector import PgVectorBackend

    pool = Recorder([ROW])
    backend = PgVectorBackend(
        pool, EMBED, ef_search=100, max_scan_tuples=100_000, scan_mem_multiplier=8, work_mem="64MB"
    )
    f = In("allowed_groups", ["hr"])
    [hit] = backend.search("leave", 5, f)
    assert (hit.id, hit.score) == ("d1", 0.75)
    *settings, (query, params) = pool.statements
    assert [s for s, _ in settings] == [
        "SET LOCAL work_mem = '64MB'",
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
        "SET LOCAL hnsw.ef_search = 100",
        "SET LOCAL hnsw.max_scan_tuples = 100000",
        "SET LOCAL hnsw.scan_mem_multiplier = 8",
    ]
    assert query.startswith("WITH candidates AS MATERIALIZED") and params[-1] == 5
    # The same query, with no HNSW index scan to stop early.
    pool.statements.clear()
    backend.search("leave", 5, f, exact=True)
    assert [s for s, _ in pool.statements] == ["SET LOCAL work_mem = '64MB'", "SET LOCAL enable_indexscan = off", query]
    pool.statements.clear()
    PgVectorBackend(pool, EMBED).search("leave", 5, f, exact=True)
    PgVectorBackend(pool, EMBED).search("leave", 5, f)
    assert [s for s, _ in pool.statements if s.startswith("SET")] == [
        "SET LOCAL enable_indexscan = off",
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
    ]


def test_pgvector_counts_the_rows_a_filter_matches_no_further_than_a_limit():
    pytest.importorskip("psycopg")
    from wardowl.backends.pgvector import PgVectorBackend

    pool = Recorder([(3,)])
    backend = PgVectorBackend(pool, EMBED, table="chunks")
    assert backend.count(In("allowed_groups", ["hr"]), 11) == 3
    ((statement, params),) = pool.statements
    assert statement == (
        'SELECT count(*) FROM (SELECT 1 FROM "chunks" WHERE (metadata @> %s::jsonb OR metadata @> %s::jsonb)'
        " LIMIT %s) AS matching"
    )
    assert params == ['{"allowed_groups": "hr"}', '{"allowed_groups": ["hr"]}', 11]
    assert asyncio.run(backend.acount(In("allowed_groups", ["hr"]), 11)) == 3  # in a thread, with no async pool
    backend.count(In("allowed_groups", ["hr"]), 2**70)  # past a bigint: every row is counted instead
    assert pool.statements[-1][1][-1] == 2**63 - 1
    # A keyword search ranks only rows that contain the query's words: a count says nothing about it.
    keyword = PgVectorBackend(pool, None, mode="keyword", async_pool=AsyncRecorder([(3,)]))
    assert keyword.count(In("allowed_groups", ["hr"]), 11) is None
    assert asyncio.run(keyword.acount(In("allowed_groups", ["hr"]), 11)) is None
    assert len(pool.statements) == 3 and backend.supports_exact and not keyword.supports_exact


def test_pgvector_sets_a_statement_timeout_in_each_search_and_count():
    pytest.importorskip("psycopg")
    from wardowl.backends.pgvector import PgVectorBackend

    pool = Recorder([ROW])
    backend = PgVectorBackend(pool, EMBED, statement_timeout=1.5, work_mem="64MB")
    f = In("allowed_groups", ["hr"])
    backend.search("leave", 5, f)
    backend.search("leave", 5, f, exact=True)
    PgVectorBackend(pool, None, mode="keyword", statement_timeout=0.25).search("leave", 5, f)
    sent = [s for s, _ in pool.statements if s.startswith("SET")]
    assert sent == [
        "SET LOCAL statement_timeout = 1500",
        "SET LOCAL work_mem = '64MB'",
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
        "SET LOCAL statement_timeout = 1500",
        "SET LOCAL work_mem = '64MB'",
        "SET LOCAL enable_indexscan = off",
        "SET LOCAL statement_timeout = 250",  # a keyword search is bounded too
    ]
    counted = Recorder([(2,)])
    assert PgVectorBackend(counted, EMBED, statement_timeout=0.0012).count(f, 4) == 2
    assert counted.statements[0][0] == "SET LOCAL statement_timeout = 1"  # whole milliseconds
    # The native async paths send the same.
    async_pool, acount_pool = AsyncRecorder([ROW]), AsyncRecorder([(2,)])
    native = PgVectorBackend(None, EMBED, async_pool=async_pool, statement_timeout=1.5, work_mem="64MB")
    asyncio.run(native.asearch("leave", 5, f))
    acounter = PgVectorBackend(None, EMBED, async_pool=acount_pool, statement_timeout=0.0012)
    assert asyncio.run(acounter.acount(f, 4)) == 2
    assert async_pool.statements == pool.statements[:4] and acount_pool.statements == counted.statements


@pytest.mark.parametrize("bad", [0, -1, 2_147_484, float("nan"), float("inf"), True, "1s"])
def test_pgvector_statement_timeout_is_checked(bad):
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError, match="statement_timeout"):
        PgVectorBackend(None, EMBED, statement_timeout=bad)
    PgVectorBackend(None, EMBED, statement_timeout=2_147_483.647)  # the largest PostgreSQL takes


def test_pgvector_statement_timeout_cancels_a_search_held_up_by_a_lock(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_timeout", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, EMBED, table="wo_timeout", iterative_scan=None, statement_timeout=0.3)
    backend.add([Document("d1", "leave policy", {"allowed_groups": ["*"]})])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    assert [h.id for h in retriever.retrieve("leave", Subject("bob"), 1).hits] == ["d1"]
    with pgvector_pool.connection() as holder:
        holder.execute('LOCK TABLE "wo_timeout" IN ACCESS EXCLUSIVE MODE')  # held until this transaction ends
        started = time.monotonic()
        with pytest.raises(SearchError, match=r"^search failed$") as failure:
            retriever.retrieve("leave", Subject("bob"), 1)
        waited = time.monotonic() - started
        holder.rollback()
    # PostgreSQL cancelled the statement waiting for the lock; without the timeout it would wait.
    assert type(failure.value.__cause__).__name__ == "QueryCanceled" and waited < 5
    assert [h.id for h in retriever.retrieve("leave", Subject("bob"), 1).hits] == ["d1"]  # the pool is fine


def test_pgvector_native_async_paths_send_what_the_sync_ones_send():
    pytest.importorskip("psycopg")
    from wardowl.backends.pgvector import PgVectorBackend

    options = {"max_scan_tuples": 50_000, "scan_mem_multiplier": 2.5, "work_mem": 65536}
    sync_pool, async_pool = Recorder([ROW]), AsyncRecorder([ROW])
    sync = PgVectorBackend(sync_pool, EMBED, **options)
    native = PgVectorBackend(None, EMBED, async_pool=async_pool, **options)
    f = In("allowed_groups", ["hr"])
    for exact in (False, True):
        assert asyncio.run(native.asearch("leave", 3, f, exact=exact)) == sync.search("leave", 3, f, exact=exact)
    count_pool, acount_pool = Recorder([(2,)]), AsyncRecorder([(2,)])
    assert PgVectorBackend(count_pool, EMBED).count(f, 4) == 2
    assert asyncio.run(PgVectorBackend(None, EMBED, async_pool=acount_pool).acount(f, 4)) == 2
    assert async_pool.statements == sync_pool.statements and acount_pool.statements == count_pool.statements


@pytest.mark.parametrize(
    "options",
    [
        {"max_scan_tuples": 0},
        {"max_scan_tuples": 2**31},
        {"max_scan_tuples": True},
        {"max_scan_tuples": 1.5},
        {"scan_mem_multiplier": 0.5},
        {"scan_mem_multiplier": 1001},
        {"scan_mem_multiplier": float("nan")},
        {"scan_mem_multiplier": "2"},
        {"work_mem": 63},
        {"work_mem": True},
        {"work_mem": "64 megabytes"},
        {"work_mem": "64MB; RESET ALL"},
    ],
)
def test_pgvector_scan_settings_are_checked(options):
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError):
        PgVectorBackend(None, EMBED, **options)
    # The edges of each range are fine.
    PgVectorBackend(None, EMBED, max_scan_tuples=1, scan_mem_multiplier=1000, work_mem="1.5GB")


def test_pgvector_scan_limits_hold_for_the_search_only(pgvector_pool):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend

    class Probe(PgVectorBackend):
        # Returns the settings the search query sees, in place of rows.
        def _query(self, query_vector, k, f):
            settings = sql.SQL(
                "SELECT current_setting('hnsw.max_scan_tuples') || ' ' || current_setting('hnsw.scan_mem_multiplier')"
                " || ' ' || current_setting('work_mem'), '', NULL, 0.0"
            )
            return settings, []

    probe = Probe(pgvector_pool, EMBED, max_scan_tuples=100_000, scan_mem_multiplier=4, work_mem="64MB")
    [hit] = probe.search("anything", 1)
    assert hit.id == "100000 4 64MB"
    # SET LOCAL ends with the search's transaction: the pooled connection is left as it was.
    with pgvector_pool.connection() as conn:
        assert conn.execute("SELECT current_setting('work_mem')").fetchone()[0] != "64MB"


def test_pgvector_repairs_a_result_its_index_cut_short(pgvector_pool):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    class IndexOnly(PgVectorBackend):
        # A small table is read in full; this one must go through its HNSW index, as a large one does.
        def _settings(self, exact=False):
            if exact:
                return super()._settings(exact)
            names = ("enable_seqscan", "enable_bitmapscan", "enable_sort")
            return [*super()._settings(exact), *(sql.SQL(f"SET LOCAL {name} = off") for name in names)]

    create_schema(pgvector_pool, EMBED.dim, table="wo_short", hnsw=True)
    with pgvector_pool.connection() as conn:
        conn.execute("TRUNCATE wo_short")
    docs = [Document(f"hr-{i:03d}", f"leave policy note {i}", {"allowed_groups": ["hr"]}) for i in range(400)]
    docs += [Document(f"bob-{i}", f"leave policy note bob {i}", {"allowed_users": ["bob"]}) for i in range(5)]
    # Without iterative scans, the index returns ef_search rows (one, here) before the filter applies.
    backend = IndexOnly(pgvector_pool, EMBED, table="wo_short", iterative_scan=None, ef_search=1)
    backend.add(docs)
    bob = Subject("bob")
    reported = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    first = reported.retrieve("leave policy", bob, 5)
    assert len(first.hits) <= 1 and (first.shortfall, first.complete) == ("unknown", False)
    repaired = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter", short_results="exact")
    result = repaired.retrieve("leave policy", bob, 5)
    assert sorted(h.id for h in result.hits) == [f"bob-{i}" for i in range(5)]
    assert (result.shortfall, result.fallback, result.complete) == ("missed", "exact", True)
    assert asyncio.run(repaired.aretrieve("leave policy", bob, 5)) == result


def test_pgvector_native_async_search_matches_sync(pgvector_pool, pgvector_factory):
    psycopg_pool = pytest.importorskip("psycopg_pool")
    from wardowl.backends.pgvector import PgVectorBackend

    docs = [Document(f"d{i}", f"doc report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    pgvector_factory(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async with psycopg_pool.AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=False) as apool:
            backend = PgVectorBackend(
                pgvector_pool, EMBED, table="wo_conformance", async_pool=apool, iterative_scan=None
            )
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)

    async_result, sync_result = asyncio.run(run())
    assert async_result == sync_result and len(async_result.hits) == 4


# ---- pgvector keyword search and hybrid search -------------------------------------------------


@pytest.mark.parametrize("config", ["English", "english; DROP TABLE x", "", "pg catalog"])
def test_pgvector_text_search_configuration_must_be_a_plain_name(config):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword", text_search=config)
    with pytest.raises(ValueError):
        create_schema(None, 4, text_search=config)


def test_pgvector_mode_and_embed_are_checked():
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError):
        PgVectorBackend(None, EMBED, mode="semantic")
    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword").add([Document("a", "doc", {})])
    with pytest.raises(ValueError):
        PgVectorBackend(None, None).search("doc", 3)


@pytest.fixture(scope="module")
def pgvector_keyword_table(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_keyword", hnsw=False, text_search="english")
    writer = PgVectorBackend(pgvector_pool, EMBED, table="wo_keyword", iterative_scan=None)

    def load(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_keyword")
        writer.add(docs)
        return writer

    return load


def test_pgvector_keyword_mode_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from wardowl.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return keyword

    check_backend(make)


def test_pgvector_hybrid_search_on_one_table_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from wardowl import RankFusion
    from wardowl.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")

    def make(docs):
        return RankFusion(pgvector_keyword_table(docs), keyword)

    check_backend(make)


def test_pgvector_keyword_search_uses_the_full_text_index(pgvector_pool, pgvector_keyword_table):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend

    pgvector_keyword_table([Document("a", "leave policy", {"allowed_groups": ["*"]})])
    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")
    # Without a permission condition the full-text index is the only index that can serve the
    # query; with one, the planner may as well start from the metadata index.
    statement, params = keyword._keyword_query("leave", 5, None)
    with pgvector_pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")  # a tiny table would otherwise be scanned
        plan = "\n".join(row[0] for row in conn.execute(sql.SQL("EXPLAIN ") + statement, params))
    assert "wo_keyword_content_english_fts" in plan


# ---- searching with your own vectors -----------------------------------------------------------


def test_pgvector_without_embed_searches_with_query_vectors(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_vectors", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, table="wo_vectors", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_vectors")
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))
    with pytest.raises(ValueError, match="embed function"):
        backend.search(KIT_QUERY, 3)


def test_pgvector_hybrid_search_with_one_query_vector(pgvector_pool, pgvector_keyword_table):
    from wardowl import RankFusion
    from wardowl.backends.pgvector import PgVectorBackend

    vector = PgVectorBackend(pgvector_pool, table="wo_keyword", iterative_scan=None)
    keyword = PgVectorBackend(pgvector_pool, table="wo_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return RankFusion(vector, keyword)

    check_backend(make, examples=8, query=Query(KIT_QUERY, EMBED(KIT_QUERY)))


def test_chroma_searches_with_query_vectors():
    chromadb = pytest.importorskip("chromadb")
    from wardowl.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()

    def make(docs):
        collection = client.create_collection(f"wo_{uuid.uuid4().hex}", embedding_function=None)
        backend = ChromaBackend(collection)
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))


# ---- changing permissions ----------------------------------------------------------------------


@pytest.mark.parametrize("change", ["add", "set_metadata"])
def test_a_permission_change_leaves_nothing_of_the_old_permissions(factory, change):
    old = Document("plan", "doc leave plan", {"allowed_groups": ["eng"], "level": 1})
    backend = factory([old])
    new_metadata = {"allowed_groups": ["hr"]}
    if change == "add" and isinstance(backend, InMemoryBackend):
        backend.add([Document("plan", old.text, new_metadata)])
    elif change == "add":
        backend.add([Document("plan", old.text, new_metadata)], embeddings=[EMBED(old.text)])
    else:
        backend.set_metadata({"plan": new_metadata})
    assert backend.search("doc leave", 5, In("allowed_groups", ["eng"])) == []
    assert backend.search("doc leave", 5, In("level", [1])) == []
    (hit,) = backend.search("doc leave", 5, In("allowed_groups", ["hr"]))
    assert hit.text == old.text and hit.metadata == new_metadata
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert retriever.retrieve("doc leave", Subject("bob", ["eng"]), 5).hits == []
    assert [h.id for h in retriever.retrieve("doc leave", Subject("ann", ["hr"]), 5).hits] == ["plan"]


# ---- write ordering: revisions -----------------------------------------------------------------


def test_stale_revision_names_the_chunks_stored_with_a_newer_revision():
    error = StaleRevision([f"c{i}" for i in range(7)] + ["c0"], 4)
    assert error.ids == tuple(f"c{i}" for i in range(7)) and error.revision == 4
    assert str(error) == "a revision newer than 4 is stored for 'c0', 'c1', 'c2', 'c3', 'c4' and 2 more"
    assert isinstance(error, RuntimeError)


@pytest.mark.parametrize("bad", [True, 1.5, "7", 2**63, -(2**63) - 1])
def test_pgvector_revision_must_be_a_64_bit_integer(bad):
    pytest.importorskip("psycopg")
    from wardowl.backends.pgvector import PgVectorBackend

    backend = PgVectorBackend(None, EMBED)  # refused before the pool is used
    with pytest.raises(ValueError, match="revision must be an integer"):
        backend.add([Document("a", "leave", {})], revision=bad)
    with pytest.raises(ValueError, match="revision must be an integer"):
        backend.set_metadata({"a": {}}, revision=bad)


@pytest.fixture
def revised(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_revision", hnsw=False)
    with pgvector_pool.connection() as conn:
        conn.execute("TRUNCATE wo_revision")
    return PgVectorBackend(pgvector_pool, EMBED, table="wo_revision", iterative_scan=None)


def _part(users, ids=("a#0", "a#1")):
    """Chunks of one source document, readable by `users`."""
    return [Document(i, f"salary bands part {i}", {"allowed_users": list(users)}) for i in ids]


def _revoke(ids=("a#0", "a#1")):
    return {i: {"allowed_users": []} for i in ids}


def _pg_stored(pool):
    """Each stored chunk's readers and revision, by id."""
    with pool.connection() as conn:
        rows = conn.execute("SELECT id, metadata, revision FROM wo_revision").fetchall()
    return {i: (m.get("allowed_users"), r) for i, m, r in rows}


def test_pgvector_without_a_revision_a_late_reindex_undoes_a_revocation(revised):
    # Last writer wins: an indexer reads the document while alice may still read it, the permission
    # sync then writes the revocation, and the indexer's write lands last.
    revised.add(_part(["alice"]))
    revised.set_metadata(_revoke())
    revised.add(_part(["alice"]))
    retriever = PermissionedRetriever(revised, MetadataACLAuthorizer())
    assert len(retriever.retrieve("salary", Subject("alice"), 5).hits) == 2


def test_pgvector_refuses_a_write_older_than_the_stored_revision(revised, pgvector_pool):
    revised.add(_part(["alice"]), revision=5)  # the indexer, at revision 5
    revised.set_metadata(_revoke(), revision=6)  # the permission sync, at revision 6
    # The indexer's late write, read at revision 5, with a chunk the document did not have before.
    with pytest.raises(StaleRevision) as refused:
        revised.add(_part(["alice"], ("a#0", "a#1", "a#2")), revision=5)
    assert refused.value.ids == ("a#0", "a#1") and refused.value.revision == 5
    assert _pg_stored(pgvector_pool) == {"a#0": ([], 6), "a#1": ([], 6)}  # nothing written, a#2 neither
    with pytest.raises(StaleRevision) as refused:
        revised.set_metadata({"a#1": {"allowed_users": ["alice"]}, "a#0": {"allowed_users": ["alice"]}}, revision=5)
    assert refused.value.ids == ("a#0", "a#1")
    assert _pg_stored(pgvector_pool) == {"a#0": ([], 6), "a#1": ([], 6)}
    assert PermissionedRetriever(revised, MetadataACLAuthorizer()).retrieve("salary", Subject("alice"), 5).hits == []
    # The same revision again (a retry) is written, and so is a newer one; ids not stored stay ignored.
    revised.set_metadata({"a#0": {"allowed_users": ["bob"]}, "gone": {"allowed_users": ["bob"]}}, revision=6)
    assert _pg_stored(pgvector_pool) == {"a#0": (["bob"], 6), "a#1": ([], 6)}
    revised.add(_part(["carol"], ("a#0", "a#1", "a#2")), revision=7)
    assert _pg_stored(pgvector_pool) == {"a#0": (["carol"], 7), "a#1": (["carol"], 7), "a#2": (["carol"], 7)}


def test_pgvector_writes_without_a_revision_are_unconditional_and_keep_the_stored_one(revised, pgvector_pool):
    revised.add(_part(["alice"]))
    assert _pg_stored(pgvector_pool) == {"a#0": (["alice"], None), "a#1": (["alice"], None)}
    revised.add(_part(["bob"]), revision=3)  # a chunk stored without a revision is never newer
    revised.set_metadata({"a#0": {"allowed_users": ["carol"]}})
    revised.add(_part(["dave"], ("a#1",)))
    assert _pg_stored(pgvector_pool) == {"a#0": (["carol"], 3), "a#1": (["dave"], 3)}
    with pytest.raises(StaleRevision):
        revised.add(_part(["erin"]), revision=2)


def _until_a_write_waits_for_a_row_lock(pool, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A connection per look: pg_stat_activity is read once per transaction.
        with pool.connection() as conn:
            (waiting,) = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND wait_event_type = 'Lock' AND query LIKE %s",
                ["%wo_revision%"],
            ).fetchone()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the write never waited for the row lock")


@pytest.mark.parametrize("change", ["add", "set_metadata"])
def test_pgvector_compares_with_a_newer_write_that_commits_while_it_waits(revised, pgvector_pool, change):
    # A newer write holds the row when a write at the revision stored so far arrives. Compared with
    # what it read before waiting, that write would pass; it must wait, compare with what the newer
    # write committed, and write nothing.
    revised.add(_part(["alice"]), revision=5)
    outcome = []

    def late_write():
        try:
            if change == "add":
                revised.add(_part(["alice"]), revision=5)
            else:
                revised.set_metadata({c.id: c.metadata for c in _part(["alice"])}, revision=5)
        except StaleRevision as error:
            outcome.append(error.ids)
        else:
            outcome.append(None)

    writer = threading.Thread(target=late_write)
    with pgvector_pool.connection() as newer, newer.transaction():
        revoked = json.dumps({"allowed_users": []})
        newer.execute("UPDATE wo_revision SET metadata = %s::jsonb, revision = 6 WHERE id = 'a#0'", [revoked])
        writer.start()
        _until_a_write_waits_for_a_row_lock(pgvector_pool)
    writer.join(30)
    assert not writer.is_alive() and outcome == [("a#0",)]
    assert _pg_stored(pgvector_pool) == {"a#0": ([], 6), "a#1": (["alice"], 5)}  # rolled back whole

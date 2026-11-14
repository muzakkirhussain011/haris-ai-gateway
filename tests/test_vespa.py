"""Vespa: YQL translation, the schema, replies, and the conformance kit against a real Vespa."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, RankFusion, Subject
from wardowl.backends import NothingMatches
from wardowl.backends._engines import tag
from wardowl.backends.vespa import TERMS_FIELD, VespaBackend, filter_to_yql, schema
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
URL = os.environ.get("WARDOWL_TEST_VESPA_URL")
APP = Path(__file__).parent.parent / "examples" / "vespa" / "app"


def _h(field, value):
    return f'"{tag(field, value).encode().hex()}"'


# ---- translation and the schema (no server) ----------------------------------------------------


def test_filters_become_yql_on_hex_terms():
    assert filter_to_yql(In("team", ["b", 1])) == f"{TERMS_FIELD} in ({_h('team', 1)}, {_h('team', 'b')})"
    a, b, c = (f"{TERMS_FIELD} in ({_h(f, v)})" for f, v in [("a", "x"), ("b", "y"), ("c", "z")])
    assert filter_to_yql(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", ["z"])))) == f"({a} and ({b} or {c}))"
    assert filter_to_yql(And(In("a", ["x"]))) == a


def test_hostile_values_become_hex_digits():
    yql = filter_to_yql(In("allowed_groups", ['x") or true or ("']))
    assert set(yql.split("(", 1)[1].strip(')"')) <= set("0123456789abcdef")


def test_an_empty_in_matches_nothing():
    # Never None: a query without the condition matches every document.
    for nothing in (In("team", []), And(In("a", ["x"]), In("b", [])), Or(In("a", []))):
        with pytest.raises(NothingMatches):
            filter_to_yql(nothing)
    assert filter_to_yql(Or(In("a", []), In("b", ["y"]))) == f"{TERMS_FIELD} in ({_h('b', 'y')})"
    with pytest.raises(ValueError):
        filter_to_yql(In("a\x1fb", ["x"]))
    with pytest.raises(TypeError):
        filter_to_yql("team")


def test_the_example_package_holds_the_schema():
    assert (APP / "schemas" / "wardowl.sd").read_text(encoding="utf-8") == schema(16)
    with pytest.raises(ValueError):
        schema(0)
    with pytest.raises(ValueError):
        schema(8, "bad name")


# ---- requests and replies (a fake Vespa) -------------------------------------------------------


class Fake:
    def __init__(self, reply=None, status=200):
        httpx = pytest.importorskip("httpx")
        self.requests = []
        self.reply, self.status = reply, status
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        import httpx

        self.requests.append(request)
        if request.url.path == "/search/":
            return httpx.Response(self.status, json=self.reply)
        return httpx.Response(self.status, json={})

    def backend(self, **kwargs):
        return VespaBackend("http://vespa:8080/", EMBED, client=self.client, **kwargs)


def _hit(doc_id, metadata=None):
    fields = {"wardowl_id": doc_id, "text": doc_id, "wardowl_metadata": json.dumps(metadata or {})}
    return {"id": f"id:wardowl:wardowl::{doc_id}", "relevance": 0.5, "fields": fields}


def test_requests_carry_the_filter_vectors_and_escaped_ids():
    fake = Fake({"root": {"children": [_hit("a b", {"team": ["x"]})]}})
    backend = fake.backend(approximate=False)
    hits = backend.search("q", 3, In("team", ["x"]))
    assert [(h.id, h.metadata) for h in hits] == [("a b", {"team": ["x"]})]
    body = json.loads(fake.requests[0].content)
    assert body["yql"] == (
        "select * from wardowl where {targetHits: 3, approximate: false}nearestNeighbor(embedding, q)"
        f" and {TERMS_FIELD} in ({_h('team', 'x')})"
    )
    assert body["hits"] == 3 and len(body["input.query(q)"]) == EMBED.dim
    backend.add([Document("a/b", "one", {"team": "x"}), Document("a/b", "two", {})])
    backend.set_metadata({"a/b": {"team": ["y"]}})
    backend.delete(["a/b", "c"])
    put, update = json.loads(fake.requests[1].content), json.loads(fake.requests[2].content)
    assert fake.requests[1].url.raw_path == b"/document/v1/wardowl/wardowl/docid/a%2Fb"
    assert put["fields"]["text"] == "two" and len(fake.requests) == 5  # the last of a repeated id wins
    assert update["fields"][TERMS_FIELD]["assign"] == [tag("team", "y").encode().hex()]
    # httpx drops "." and ".." from a path, so their requests would reach another one: they are
    # refused before anything is sent. Ids that merely hold dots keep them.
    for bad in (".", ".."):
        with pytest.raises(ValueError, match="Vespa cannot address"):
            backend.delete(["c", bad])
        with pytest.raises(ValueError, match="Vespa cannot address"):
            backend.set_metadata({bad: {"team": ["y"]}})
        with pytest.raises(ValueError, match="Vespa cannot address"):
            backend.add([Document(bad, "text", {})])
    assert len(fake.requests) == 5
    backend.delete(["...", "a..b", "./x"])
    paths = sorted(request.url.raw_path for request in fake.requests[5:])
    assert paths == [b"/document/v1/wardowl/wardowl/docid/" + p for p in (b".%2Fx", b"...", b"a..b")]


def test_keyword_mode_sends_the_text():
    fake = Fake({"root": {}})
    backend = VespaBackend("http://vespa:8080", None, mode="keyword", client=fake.client)
    assert backend.search("leave policy", 5) == []
    body = json.loads(fake.requests[0].content)
    assert body["query"] == "leave policy" and body["ranking.profile"] == "wardowl_bm25"
    backend.add([Document("a", "text", {})])
    assert "embedding" not in json.loads(fake.requests[1].content)["fields"]
    with pytest.raises(ValueError, match="text"):
        backend.search(Query(vector=EMBED("x")), 5)


def test_nothing_to_match_asks_nothing():
    fake = Fake()
    backend = fake.backend()
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.delete([])
    assert fake.requests == []


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        ({"root": {"errors": [{"code": 12, "message": "Timeout"}], "children": []}}, 200),
        ({"root": {}}, 500),
        ({"nothing": 1}, 200),
        ({"root": {"children": [{"id": "x", "relevance": 1.0, "fields": {}}]}}, 200),
    ],
)
def test_failed_or_malformed_searches_raise(reply, status):
    with pytest.raises(RuntimeError, match="Vespa"):
        Fake(reply, status).backend().search("q", 3)


@pytest.mark.parametrize(
    "coverage",
    [
        {"coverage": 50, "documents": 5, "full": False, "nodes": 1, "results": 1, "resultsFull": 0},
        {"coverage": 100, "documents": 10, "degraded": {"timeout": True}, "nodes": 1, "results": 1, "resultsFull": 0},
    ],
)
def test_partial_coverage_raises_instead_of_passing_for_complete_results(coverage):
    reply = {"root": {"coverage": coverage, "children": [_hit("a")]}}
    with pytest.raises(RuntimeError, match="partial results"):
        Fake(reply).backend().search("q", 3)
    full = {"coverage": 100, "documents": 10, "full": True, "nodes": 1, "results": 1, "resultsFull": 1}
    assert [h.id for h in Fake({"root": {"coverage": full, "children": [_hit("a")]}}).backend().search("q", 3)] == ["a"]


def test_a_document_whose_metadata_is_not_a_json_object_carries_no_permissions():
    # As another tool could store it: JSON that is not an object, text that is not JSON, nothing.
    odd = ["[]", '["allowed_groups"]', "1", '"x"', "true", "null", "not json", ""]
    children = []
    for i, stored in enumerate([*odd, None]):
        fields = {"wardowl_id": f"d{i}", "text": "t"}
        if stored is not None:
            fields["wardowl_metadata"] = stored
        children.append({"id": f"id:wardowl:wardowl::d{i}", "relevance": 0.5, "fields": fields})
    reply = {"root": {"children": [*children, _hit("ok", {"allowed_groups": ["eng"]})]}}
    hits = Fake(reply).backend().search("q", 20)
    assert [h.metadata for h in hits] == [{}] * len(children) + [{"allowed_groups": ["eng"]}]
    # Denied and counted, with or without the re-check, where it failed the whole retrieval.
    for recheck in (True, False):
        retriever = PermissionedRetriever(Fake(reply).backend(), MetadataACLAuthorizer(), recheck=recheck)
        result = retriever.retrieve("q", Subject("ann", ["eng"]), k=20)
        assert [h.id for h in result.hits] == ["ok"] and result.violations == len(children)


def test_only_an_exact_vector_search_declares_exact_search():
    fake = Fake({"root": {"children": [_hit("a", {"allowed_groups": ["eng"]})]}})
    assert fake.backend(approximate=False).exact_search is True
    assert fake.backend().exact_search is False
    keyword = VespaBackend("http://vespa:8080", None, mode="keyword", approximate=False, client=fake.client)
    assert keyword.exact_search is False  # BM25 ranks only documents that hold the query's words
    # Vespa then computes the distance to every document the filter admits, so a short result is complete.
    ann = Subject("ann", ["eng"])
    exact = PermissionedRetriever(fake.backend(approximate=False), MetadataACLAuthorizer()).retrieve("q", ann, k=3)
    assert [h.id for h in exact.hits] == ["a"] and exact.complete and exact.shortfall == "exhausted"
    hnsw = PermissionedRetriever(fake.backend(), MetadataACLAuthorizer()).retrieve("q", ann, k=3)
    assert [h.id for h in hnsw.hits] == ["a"] and not hnsw.complete and hnsw.shortfall == "unknown"


def test_failed_writes_raise_and_settings_are_checked():
    backend = Fake(status=507).backend()
    with pytest.raises(RuntimeError, match="write failed"):
        backend.add([Document("a", "x", {}), Document("b", "y", {})])
    for bad in ({"schema": "a.b"}, {"namespace": ""}, {"mode": "hybrid"}, {"concurrency": 0}):
        with pytest.raises(ValueError):
            Fake().backend(**bad)
    pytest.importorskip("httpx")
    own = VespaBackend("http://vespa:8080", EMBED)
    own.close()


# ---- a real Vespa ------------------------------------------------------------------------------

needs_vespa = pytest.mark.skipif(not URL, reason="set WARDOWL_TEST_VESPA_URL to run against Vespa")
_STORED: set[str] = set()


def _factory(*, mode="vector", approximate=False, vectors=False, hybrid=False):
    httpx = pytest.importorskip("httpx")
    client = httpx.Client(timeout=30)
    stored = _STORED  # one Vespa for every test: each corpus replaces the one before

    def make(docs):
        embed = None if vectors or mode == "keyword" else EMBED
        backend = VespaBackend(URL, embed, mode=mode, approximate=approximate, client=client)
        backend.delete(stored)
        stored.clear()
        backend.add(docs, [EMBED(d.text) for d in docs] if vectors else None)
        stored.update(d.id for d in docs)
        if hybrid:
            return RankFusion(backend, VespaBackend(URL, None, mode="keyword", client=client))
        return backend

    return make


@needs_vespa
def test_the_conformance_kit_exact():
    check_backend(_factory())


@needs_vespa
def test_the_conformance_kit_with_hnsw():
    check_backend(_factory(approximate=True), examples=10, exact=False)


@needs_vespa
def test_the_conformance_kit_with_query_vectors():
    check_backend(_factory(vectors=True), examples=8, query=Query(vector=EMBED(QUERY)))


@needs_vespa
def test_keyword_mode_passes_the_conformance_kit():
    check_backend(_factory(mode="keyword"), examples=10)


@needs_vespa
def test_hybrid_search_passes_the_conformance_kit():
    check_backend(_factory(hybrid=True), examples=8)


@needs_vespa
def test_pre_filtering_against_vespa():
    backend = _factory()(
        [
            Document("eng", "engineering launch plan", {"allowed_groups": ["eng"]}),
            Document("hr", "pay bands for the launch", {"allowed_groups": ["hr"]}),
            Document("all", "launch date for everyone", {"allowed_groups": ["*"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=3).hits} == {"eng", "all"}

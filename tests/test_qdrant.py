"""QdrantBackend in Qdrant's local mode, and against a real Qdrant server when one is available."""

from __future__ import annotations

import asyncio
import os
import uuid
import zlib

import pytest

qdrant_client = pytest.importorskip("qdrant_client")
from qdrant_client import models  # noqa: E402
from qdrant_client.http import models as rest_models  # noqa: E402

from wardowl import (  # noqa: E402
    And,
    Document,
    In,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    Query,
    RankFusion,
    Subject,
)
from wardowl.backends import NothingMatches  # noqa: E402
from wardowl.backends.qdrant import (  # noqa: E402
    ID_KEY,
    QdrantBackend,
    bm25_terms,
    create_collection,
    filter_to_qdrant,
    point_id,
)
from wardowl.testing import QUERY, HashingEmbedder, check_backend  # noqa: E402

EMBED = HashingEmbedder()
URL = os.environ.get("WARDOWL_TEST_QDRANT_URL")
needs_server = pytest.mark.skipif(not URL, reason="set WARDOWL_TEST_QDRANT_URL to run against a Qdrant server")


def _factory(client, *, own_vectors=False):
    """Backends holding exactly the given documents; with `own_vectors`, built without `embed`."""
    previous: list[str] = []

    def make(docs):
        # A new collection per example keeps examples independent.
        for name in previous:
            client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
        create_collection(client, previous[0], EMBED.dim)
        backend = QdrantBackend(client, previous[0], None if own_vectors else EMBED)
        backend.add(docs, [EMBED(d.text) for d in docs] if own_vectors else None)
        return backend

    return make


# ---- translation -------------------------------------------------------------------------------


def test_in_becomes_one_match_any_per_value_type():
    f = filter_to_qdrant(In("team", ["b", 2, "a"]))
    assert f == models.Filter(
        should=[
            models.FieldCondition(key="metadata.team", match=models.MatchAny(any=["a", "b"])),
            models.FieldCondition(key="metadata.team", match=models.MatchAny(any=[2])),
        ]
    )
    # A single condition is wrapped, so the result is always a Filter.
    assert filter_to_qdrant(In("team", ["a"])) == models.Filter(
        must=[models.FieldCondition(key="metadata.team", match=models.MatchAny(any=["a"]))]
    )


def test_an_empty_in_matches_nothing_without_asking_qdrant():
    # Never None: Qdrant reads a missing query_filter as "no filter", which matches every point.
    with pytest.raises(NothingMatches):
        filter_to_qdrant(In("team", []))
    with pytest.raises(NothingMatches):
        filter_to_qdrant(And(In("team", ["a"]), In("level", [])))
    # In an Or, the empty clause drops out and the rest still applies.
    only = filter_to_qdrant(Or(In("team", []), In("level", [1])))
    assert only == models.Filter(should=[models.FieldCondition(key="metadata.level", match=models.MatchAny(any=[1]))])
    backend = QdrantBackend(qdrant_client.QdrantClient(":memory:"), "never-created", EMBED)
    assert backend.search("leave", 5, In("team", [])) == []  # the collection does not exist: Qdrant was not asked


@pytest.mark.parametrize("field", ["a.b", "tags[]", 'say"hi'])
def test_fields_that_look_like_qdrant_paths_are_refused(field):
    with pytest.raises(ValueError, match="path syntax"):
        filter_to_qdrant(In(field, ["x"]))


def test_every_document_id_is_hashed_to_its_own_point_id():
    u = uuid.uuid4()
    spellings = [str(u), str(u).upper(), u.hex, f"{{{u}}}", f"urn:uuid:{u}"]
    # A UUID is hashed too, so no spelling of one id stands for another.
    assert len({point_id(s) for s in spellings}) == len(spellings) and point_id(str(u)) != str(u)
    assert point_id("doc-1") == point_id("doc-1") != point_id("doc-2")
    parsed = uuid.UUID(point_id("doc-1"))
    assert str(parsed) == point_id("doc-1") and parsed.version == 8


# ---- local mode (no server) --------------------------------------------------------------------


def test_local_mode_passes_the_conformance_kit():
    check_backend(_factory(qdrant_client.QdrantClient(":memory:")))


def test_local_mode_searches_with_query_vectors():
    client = qdrant_client.QdrantClient(":memory:")
    check_backend(_factory(client, own_vectors=True), examples=8, query=Query(vector=EMBED(QUERY)))


def test_points_written_by_langchain_are_searchable_and_carry_their_permissions():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "lc", EMBED.dim)
    point = str(uuid.uuid4())
    client.upsert(
        "lc",
        points=[
            models.PointStruct(
                id=point,
                vector=EMBED("leave policy"),
                payload={"page_content": "leave policy", "metadata": {"allowed_groups": ["hr"]}},
            ),
            models.PointStruct(id=str(uuid.uuid4()), vector=EMBED("leave policy"), payload={"page_content": "x"}),
        ],
    )
    backend = QdrantBackend(client, "lc", EMBED)
    hits = PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve("leave", Subject("ann", ["hr"]), k=5).hits
    # The point id stands in for a wardowl id; the point without metadata is readable by nobody.
    assert [(h.id, h.text) for h in hits] == [(point, "leave policy")]


def test_add_keeps_the_document_id_and_delete_removes_by_it():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "ids", EMBED.dim)
    backend = QdrantBackend(client, "ids", EMBED)
    backend.add([Document("doc-1", "leave policy", {"allowed_groups": ["*"]}), Document("doc-2", "salary", {})])
    stored = client.retrieve("ids", [point_id("doc-1")], with_payload=True)[0]
    assert stored.payload[ID_KEY] == "doc-1"
    backend.delete(["doc-1"])
    assert [h.id for h in backend.search("leave policy salary", 5)] == ["doc-2"]


def test_changes_reach_only_the_point_that_holds_the_id():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "owned", EMBED.dim)
    backend = QdrantBackend(client, "owned", EMBED)
    u = str(uuid.uuid4())
    backend.add([Document(u, "leave policy", {"allowed_groups": ["hr"]})])
    # Another spelling of the same UUID is another document, with a point of its own.
    backend.add([Document(u.upper(), "salary bands", {"allowed_groups": ["hr"]})])
    # A point at victim's point id that holds another id: a collision, or a write by another tool.
    payload = {ID_KEY: "intruder", "page_content": "board minutes", "metadata": {"allowed_groups": ["board"]}}
    client.upsert("owned", points=[models.PointStruct(id=point_id("victim"), vector=EMBED("board"), payload=payload)])
    backend.set_metadata({"victim": {"allowed_groups": ["*"]}, u.upper(): {"allowed_groups": ["*"]}})
    backend.delete(["victim"])
    (held,) = client.retrieve("owned", [point_id("victim")], with_payload=True)
    assert held.payload == payload  # neither changed nor deleted
    found = {h.id: h.metadata for h in backend.search("leave salary board", 5)}
    hr, everyone = {"allowed_groups": ["hr"]}, {"allowed_groups": ["*"]}
    assert found == {u: hr, u.upper(): everyone, "intruder": payload["metadata"]}
    backend.delete([u.upper()])
    assert {h.id for h in backend.search("leave salary board", 5)} == {u, "intruder"}


def test_set_metadata_replaces_only_the_metadata_and_never_recreates_a_point():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "meta", EMBED.dim)
    backend = QdrantBackend(client, "meta", EMBED)
    backend.add([Document("a", "leave policy", {"allowed_groups": ["hr"], "team": "x"})])
    client.set_payload("meta", payload={"source": "drive"}, points=[point_id("a")])  # a key another tool wrote
    backend.set_metadata({"a": {"allowed_groups": ["eng"]}})
    (point,) = client.retrieve("meta", [point_id("a")], with_payload=True)
    assert point.payload == {
        ID_KEY: "a",
        "page_content": "leave policy",
        "metadata": {"allowed_groups": ["eng"]},  # no key of the old metadata survives
        "source": "drive",
    }
    backend.delete(["a"])
    backend.set_metadata({"a": {"allowed_groups": ["*"]}, "never-written": {"allowed_groups": ["*"]}})
    assert client.count("meta").count == 0


def test_points_written_by_another_tool_are_changed_by_their_exact_uuid():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "lc2", EMBED.dim)
    point = str(uuid.uuid4())
    payload = {"page_content": "leave policy", "metadata": {"allowed_groups": ["hr"]}}
    client.upsert("lc2", points=[models.PointStruct(id=point, vector=EMBED("leave policy"), payload=payload)])
    backend = QdrantBackend(client, "lc2", EMBED)
    backend.set_metadata({point.upper(): {"allowed_groups": ["*"]}})  # not the spelling results report
    assert [h.metadata for h in backend.search("leave", 5)] == [{"allowed_groups": ["hr"]}]
    backend.set_metadata({point: {"allowed_groups": ["*"]}})
    assert [(h.id, h.metadata) for h in backend.search("leave", 5)] == [(point, {"allowed_groups": ["*"]})]
    backend.delete([f"{{{point}}}"])
    assert len(backend.search("leave", 5)) == 1
    backend.delete([point])
    assert backend.search("leave", 5) == []


def test_named_vectors_and_search_params_are_passed_through():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "named", EMBED.dim, vector_name="text")
    backend = QdrantBackend(client, "named", EMBED, vector_name="text", search_params=models.SearchParams(exact=True))
    backend.add([Document("a", "leave policy", {"team": "x"}), Document("b", "salary", {"team": "y"})])
    assert [h.id for h in backend.search("leave", 5, In("team", ["x"]))] == ["a"]


def test_exact_search_is_declared_when_the_search_params_force_it():
    def backend(params, mode="vector"):
        return QdrantBackend(None, "c", EMBED, mode=mode, search_params=params)

    assert backend(models.SearchParams(exact=True)).exact_search is True
    assert backend(None).exact_search is False
    assert backend(models.SearchParams(hnsw_ef=128)).exact_search is False
    # indexed_only skips segments that are still being indexed, so not every point is scored.
    assert backend(models.SearchParams(exact=True, indexed_only=True)).exact_search is False
    assert backend(models.SearchParams(exact=True), mode="keyword").exact_search is False
    assert backend(None).supports_exact is True and backend(None, mode="keyword").supports_exact is False


class Recording:
    """A Qdrant client that records what it is asked, and answers with no points and a fixed count."""

    def __init__(self, count=0):
        self.counted = count
        self.calls = []

    def query_points(self, **request):
        self.calls.append(("query_points", request))
        # qdrant_client.models also exports fastembed's QueryResponse under that name; query_points
        # answers with the REST model.
        return rest_models.QueryResponse(points=[])

    def count(self, collection_name, **options):
        self.calls.append(("count", {"collection_name": collection_name, **options}))
        return models.CountResult(count=self.counted)


class AsyncRecording(Recording):
    async def query_points(self, **request):
        return super().query_points(**request)

    async def count(self, collection_name, **options):
        return super().count(collection_name, **options)


def test_an_exact_search_forces_exact_on_a_copy_of_the_search_params():
    acorn = models.AcornSearchParams(enable=True, max_selectivity=1.0)
    params = models.SearchParams(hnsw_ef=128, acorn=acorn, indexed_only=True)
    client = Recording()
    backend = QdrantBackend(client, "docs", EMBED, search_params=params)
    f = In("allowed_groups", ["hr"])
    backend.search("leave", 5, f)
    backend.search("leave", 5, f, exact=True)
    QdrantBackend(client, "docs", EMBED).search("leave", 5, f, exact=True)
    (_, approximate), (_, exact), (_, plain) = client.calls
    assert approximate["search_params"] is params and params.exact is not True  # ACORN, as given
    forced = exact["search_params"]
    assert (forced.exact, forced.indexed_only, forced.hnsw_ef, forced.acorn) == (True, False, 128, acorn)
    assert plain["search_params"] == models.SearchParams(exact=True)
    assert exact["query_filter"] == approximate["query_filter"] == filter_to_qdrant(f) and exact["limit"] == 5


def test_count_asks_qdrant_for_an_exact_count_under_the_same_filter():
    client = Recording(count=7)
    backend = QdrantBackend(client, "docs", EMBED)
    f = And(In("tenant", ["acme"]), In("allowed_groups", ["hr"]))
    assert backend.count(f, 3) == 7  # Qdrant counts them all, past the limit
    assert client.calls == [("count", {"collection_name": "docs", "count_filter": filter_to_qdrant(f), "exact": True})]
    assert backend.count(In("allowed_groups", []), 3) == 0 and len(client.calls) == 1  # nothing can match
    assert asyncio.run(backend.acount(f, 3)) == 7  # in a thread, with no async client
    keyword = QdrantBackend(client, "docs", mode="keyword", async_client=AsyncRecording(count=7))
    assert keyword.count(f, 3) is None and asyncio.run(keyword.acount(f, 3)) is None


def test_native_async_count_and_exact_search_send_what_the_sync_ones_send():
    sync, native = Recording(count=4), AsyncRecording(count=4)
    f = In("allowed_groups", ["hr"])
    backend = QdrantBackend(sync, "docs", EMBED)
    async_backend = QdrantBackend(None, "docs", EMBED, async_client=native)
    assert backend.count(f, 5) == asyncio.run(async_backend.acount(f, 5)) == 4
    assert asyncio.run(async_backend.acount(In("allowed_groups", []), 5)) == 0
    assert backend.search("leave", 5, f, exact=True) == asyncio.run(async_backend.asearch("leave", 5, f, exact=True))
    assert native.calls == sync.calls


class Stopped(QdrantBackend):
    """Returns one hit under a filter unless asked for an exact search, as an index that stopped early."""

    def search(self, query, k, filter=None, *, exact=False):
        hits = super().search(query, k, filter, exact=exact)
        return hits if exact or filter is None else hits[:1]


def test_short_results_are_told_apart_and_repaired(any_client):
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(any_client, name, EMBED.dim)
    groups = [["*"] if i % 3 == 0 else ["hr"] for i in range(12)]
    QdrantBackend(any_client, name, EMBED).add(
        [Document(f"d{i:02d}", f"leave report {i}", {"allowed_groups": g}) for i, g in enumerate(groups)]
    )
    bob = Subject("bob", ["eng"])  # may read the four chunks for everyone
    authorizer = MetadataACLAuthorizer()
    try:
        backend = QdrantBackend(any_client, name, EMBED)
        assert backend.count(In("allowed_groups", ["hr"]), 1) == 8
        reported = PermissionedRetriever(backend, authorizer).retrieve("leave", bob, 10)
        assert len(reported.hits) == 4 and (reported.shortfall, reported.complete) == ("unknown", False)
        counted = PermissionedRetriever(backend, authorizer, short_results="count")
        result = counted.retrieve("leave", bob, 10)
        assert (result.shortfall, result.complete) == ("exhausted", True)
        assert asyncio.run(counted.aretrieve("leave", bob, 10)) == result
        exact = QdrantBackend(any_client, name, EMBED, search_params=models.SearchParams(exact=True))
        result = PermissionedRetriever(exact, authorizer).retrieve("leave", bob, 10)
        assert len(result.hits) == 4 and (result.shortfall, result.complete) == ("exhausted", True)
        repaired = PermissionedRetriever(Stopped(any_client, name, EMBED), authorizer, short_results="exact")
        result = repaired.retrieve("leave", bob, 10)
        assert sorted(h.id for h in result.hits) == ["d00", "d03", "d06", "d09"]
        assert (result.shortfall, result.fallback, result.complete) == ("missed", "exact", True)
    finally:
        any_client.delete_collection(name)


def _legacy_point(doc_id, text, metadata):
    """A point as needtoknow 1.6 wrote it: the id under needtoknow_id, at a point id derived another way."""
    point = str(uuid.uuid5(uuid.UUID("5e3b1d7c-6b7a-4f0e-9a51-6f1d0c2b8e44"), doc_id))
    payload = {"needtoknow_id": doc_id, "page_content": text, "metadata": metadata}
    return models.PointStruct(id=point, vector=EMBED(text), payload=payload)


def test_a_collection_written_by_needtoknow_1x_is_refused_before_anything_is_written(any_client):
    # needtoknow 1.x kept the id under needtoknow_id at a point id derived another way, so a revocation
    # sent by chunk id reached no point, and nothing said so.
    name = f"wo_{uuid.uuid4().hex}"
    hr = {"allowed_groups": ["hr"]}
    params = models.VectorParams(size=EMBED.dim, distance=models.Distance.COSINE)
    any_client.create_collection(name, vectors_config=params)
    try:
        any_client.upsert(name, points=[_legacy_point("doc-42", "salary bands", hr)], wait=True)
        with pytest.raises(ValueError, match=r"needtoknow 1\.x.*upgrading"):
            create_collection(any_client, name, EMBED.dim)
        backend = QdrantBackend(any_client, name, EMBED)
        with pytest.raises(ValueError, match=r"needtoknow 1\.x"):
            backend.set_metadata({"doc-42": {"allowed_groups": []}})
        with pytest.raises(ValueError, match=r"needtoknow 1\.x"):
            backend.delete(["doc-42"])
        with pytest.raises(ValueError, match=r"needtoknow 1\.x"):
            backend.add([Document("doc-42", "salary bands", {"allowed_groups": []})])
        (stored,) = any_client.scroll(name, with_payload=True)[0]
        assert stored.payload["metadata"] == hr and any_client.count(name).count == 1  # nothing was written
        # Nothing to write, nothing checked; searching is not checked.
        backend.set_metadata({})
        backend.delete([])
        assert [h.metadata for h in backend.search("salary", 5)] == [hr]
    finally:
        any_client.delete_collection(name)


def test_current_collections_pass_the_layout_check(any_client):
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(any_client, name, EMBED.dim)
    try:
        create_collection(any_client, name, EMBED.dim)  # empty
        backend = QdrantBackend(any_client, name, EMBED)
        backend.add([Document("a", "leave policy", {"allowed_groups": ["hr"]})])
        foreign = {"page_content": "rota", "metadata": {"allowed_groups": ["hr"]}}  # as LangChain writes it
        point = models.PointStruct(id=str(uuid.uuid4()), vector=EMBED("rota"), payload=foreign)
        any_client.upsert(name, points=[point])
        # A chunk wardowl wrote that also holds a needtoknow_id, written by another tool, is wardowl's.
        any_client.set_payload(name, payload={"needtoknow_id": "a"}, points=[point_id("a")], wait=True)
        create_collection(any_client, name, EMBED.dim)
        fresh = QdrantBackend(any_client, name, EMBED)
        fresh.set_metadata({"a": {"allowed_groups": ["*"]}})
        assert {h.id: h.metadata for h in fresh.search("leave", 5)}["a"] == {"allowed_groups": ["*"]}
    finally:
        any_client.delete_collection(name)


async def _native_asearch(client, name, k, f):
    async_client = qdrant_client.AsyncQdrantClient(url=URL)
    try:
        return await QdrantBackend(client, name, EMBED, async_client=async_client).asearch("leave", k, f)
    finally:
        await async_client.close()


def test_a_point_is_never_reported_under_an_id_another_point_holds_where_add_writes_it(any_client):
    # A point written by another tool at the UUID u, and a point at another chunk's point id holding
    # "x", were both reported under the id of a wardowl chunk: one id for two points, which made the
    # reranker path raise and could hide the chunk across post-filtering rounds.
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(any_client, name, EMBED.dim)
    everyone = {"allowed_groups": ["*"]}
    u = str(uuid.uuid4())
    docs = [Document(u, "leave policy", everyone), Document("x", "leave rota", everyone)]
    docs += [Document(f"d{i}", f"leave note {i}", everyone) for i in range(4)]
    backend = QdrantBackend(any_client, name, EMBED)
    backend.add(docs)
    other_tool = {"page_content": "other tool", "metadata": everyone}
    copy = {ID_KEY: "x", "page_content": "copy", "metadata": everyone}
    shadows = [
        models.PointStruct(id=u, vector=EMBED("leave policy"), payload=other_tool),
        models.PointStruct(id=point_id("elsewhere"), vector=EMBED("leave rota"), payload=copy),
    ]
    any_client.upsert(name, points=shadows, wait=True)
    readable = In("allowed_groups", ["*"])
    try:
        wanted = {d.id: d.text for d in docs}
        for f in (None, readable):
            for exact in (False, True):
                hits = backend.search("leave policy rota", 6, f, exact=exact)
                # Every chunk once, with its own text, and k of them: the search asked for more.
                assert {h.id: h.text for h in hits} == wanted and len(hits) == 6
        assert {h.id: h.text for h in asyncio.run(backend.asearch("leave", 6))} == wanted
        reversing = PermissionedRetriever(backend, MetadataACLAuthorizer(), reranker=lambda q, hits: hits[::-1])
        assert sorted(h.id for h in reversing.retrieve("leave", Subject("ann"), k=6).hits) == sorted(wanted)
        if URL:  # a native async client needs the server: local mode keeps each client's data apart
            assert asyncio.run(_native_asearch(any_client, name, 6, readable)) == backend.search("leave", 6, readable)
        # The point at another chunk's point id is still reported under "x" once no chunk holds "x".
        backend.delete(["x"])
        assert {h.id: h.text for h in backend.search("leave rota", 6)}["x"] == "copy"
    finally:
        any_client.delete_collection(name)


def test_wrong_input_is_rejected():
    with pytest.raises(ValueError):
        QdrantBackend(None, "", EMBED)
    with pytest.raises(ValueError):
        create_collection(None, "c", 0)
    backend = QdrantBackend(qdrant_client.QdrantClient(":memory:"), "c", EMBED)
    with pytest.raises(ValueError, match="one embedding per document"):
        backend.add([Document("a", "t")], embeddings=[])
    assert backend.search("anything", 0) == []


# ---- a real Qdrant server ----------------------------------------------------------------------


@needs_server
def test_server_passes_the_conformance_kit():
    check_backend(_factory(qdrant_client.QdrantClient(url=URL)))


@needs_server
def test_native_async_search_matches_sync():
    client = qdrant_client.QdrantClient(url=URL)
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    QdrantBackend(client, name, EMBED).add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async_client = qdrant_client.AsyncQdrantClient(url=URL)
        try:
            backend = QdrantBackend(client, name, EMBED, async_client=async_client)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.close()

    async_result, sync_result = asyncio.run(run())
    client.delete_collection(name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups():
    client = qdrant_client.QdrantClient(url=URL) if URL else qdrant_client.QdrantClient(":memory:")
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim)
    backend = QdrantBackend(client, name, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    client.delete_collection(name)
    assert [h.id for h in result.hits] == ["mine"]


# ---- keyword search and hybrid search ----------------------------------------------------------


def test_bm25_terms_weigh_repeated_words_and_keep_every_word():
    indices, values = bm25_terms("The leave, the LEAVE policy")
    the, leave, policy = (zlib.crc32(w.encode()) for w in ("the", "leave", "policy"))
    assert indices == sorted({the, leave, policy})
    weights = dict(zip(indices, values, strict=True))
    assert weights[leave] == weights[the] > weights[policy] > 0
    assert bm25_terms("") == ([], [])


def test_mode_is_checked():
    with pytest.raises(ValueError):
        QdrantBackend(None, "c", mode="semantic")


@pytest.fixture(params=["local", "server"])
def any_client(request):
    if request.param == "server":
        if not URL:
            pytest.skip("set WARDOWL_TEST_QDRANT_URL to run against a Qdrant server")
        return qdrant_client.QdrantClient(url=URL)
    return qdrant_client.QdrantClient(":memory:")


def _keyword_factory(client, *, hybrid=False):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
        create_collection(client, previous[0], EMBED.dim, text_search=True)
        if hybrid:
            vector = QdrantBackend(client, previous[0], EMBED, text_search=True)
            vector.add(docs)
            return RankFusion(vector, QdrantBackend(client, previous[0], mode="keyword"))
        keyword = QdrantBackend(client, previous[0], mode="keyword")
        keyword.add(docs)
        return keyword

    return make


def test_keyword_mode_passes_the_conformance_kit(any_client):
    check_backend(_keyword_factory(any_client))


def test_hybrid_search_over_one_collection_passes_the_conformance_kit(any_client):
    make = _keyword_factory(any_client, hybrid=True)
    check_backend(make, examples=10)
    check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))


def test_keyword_mode_ranks_by_bm25_inside_the_filter(any_client):
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(any_client, name, EMBED.dim, text_search=True)
    keyword = QdrantBackend(any_client, name, mode="keyword")
    keyword.add(
        [
            Document("a", "leave policy for staff", {"allowed_groups": ["*"]}),
            Document("b", "leave leave leave payouts", {"allowed_groups": ["hr"]}),
            Document("c", "the incident runbook", {"allowed_groups": ["*"]}),
        ]
    )
    try:
        hits = keyword.search("leave", 5)
        assert [h.id for h in hits] == ["b", "a"] and hits[0].score > hits[1].score > 0
        assert [h.id for h in keyword.search("leave", 5, In("allowed_groups", ["*"]))] == ["a"]
        assert [h.id for h in keyword.search("the", 5)] == ["c"]  # every word is kept
        assert keyword.search("?! --", 5) == []
        with pytest.raises(ValueError):
            keyword.search(Query(vector=EMBED("leave")), 5)
        keyword.set_metadata({"b": {"allowed_groups": ["*"]}})
        assert [h.id for h in keyword.search("payouts", 5, In("allowed_groups", ["*"]))] == ["b"]
    finally:
        any_client.delete_collection(name)

"""MongoDB backend: Atlas Vector Search, with the permission filter inside `$vectorSearch`.

Install with `pip install "wardowl[mongodb]"`. Pass a pymongo `Collection` on MongoDB Atlas or on
a local Atlas deployment (the `mongodb/mongodb-atlas-local` image) and, for a native `asearch`, the
same collection from an `AsyncMongoClient`.

Permissions are matched on one array field, `wardowl_terms`, holding a type-tagged term for
every string or integer in the chunk's metadata, as the Elasticsearch and Milvus backends do. A
filter becomes a BSON document (`$in`, `$and`, `$or`) on that field, so values are data, never
query syntax, and `1` and `"1"` stay apart. Atlas Search indexes at most 8,181 characters of a
string and cuts a longer one short, while a filter names the whole value, so a term longer than
that is stored and searched as its SHA-256 digest instead: its chunk is found, and a value that is
only its first 8,181 characters does not match it. The metadata itself is stored as written, for
the retriever's re-check. `create_search_index` creates the vector search index with the vector and
the filter fields.

Atlas Vector Search indexes a write shortly after it happens, not at once. So `add` and `delete`
wait (by default) until the index shows the change: every write carries a fresh marker,
`wardowl_write`, and the wait ends when the index returns each chunk with its marker, which is
also when its new permission terms are searchable.

`MongoDBBackend(..., mode="keyword")` searches the text with Atlas Search (`$search`, BM25) under
the same permission filter, written with Atlas Search's own operators, in an index that
`create_text_index` creates. A backend given `text_index` waits for that index too.

`add` and `set_metadata` given a `revision` store it on each chunk, in `wardowl_revision`, and do
not replace a chunk stored with a newer one: they raise `StaleRevision` instead.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from typing import Any

from wardowl.backends._engines import _SEP, TERMS_FIELD, digest, tag, terms
from wardowl.backends._errors import StaleRevision, required, revision_number
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Hit, Query

__all__ = [
    "REVISION_FIELD",
    "TERMS_FIELD",
    "WRITE_FIELD",
    "MongoDBBackend",
    "create_search_index",
    "create_text_index",
    "filter_to_mongo",
    "filter_to_search",
    "permission_terms",
]

Embed = Callable[[str], Sequence[float]]

WRITE_FIELD = "wardowl_write"
"""A marker each `add` writes, so it can tell when the search index has caught up."""

REVISION_FIELD = "wardowl_revision"
"""The source document's revision, stored on each chunk by `add` and `set_metadata` given `revision`."""

_BATCH = 500
_MAX_CANDIDATES = 10_000  # Atlas Vector Search's limit for numCandidates (and so for limit)
_WORD = re.compile(r"\w")
# Below the longest string a vector-search filter field indexes whole, in Java characters (UTF-16 code
# units): it cuts longer ones short, and a filter naming the whole string misses it. Measured at 8,191
# on Atlas Local 8.0 and 8.3; the token type documents 8,181, which this keeps to.
_MAX_TERM = 8181


def _term(term: str) -> str:
    """`term`, or its digest when it is longer than `_MAX_TERM` UTF-16 code units."""
    units = len(term.encode("utf-16-le", "surrogatepass")) // 2
    return term if units <= _MAX_TERM else digest(term)


def _terms(doc_id: str, metadata: Mapping[str, Any]) -> list[str]:
    return [_term(t) for t in terms(doc_id, metadata)]


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `wardowl_terms` for a chunk with this metadata, for documents written by other tools."""
    return _terms("metadata", metadata)


def filter_to_mongo(f: Filter) -> dict[str, Any]:
    """Translate a filter into a `$vectorSearch` filter on the tagged terms.

    Raises `NothingMatches` when nothing can match, as `$vectorSearch` reads a missing filter as "match everything".
    """
    return required(_vector_filter(f))


def _vector_filter(f: Filter) -> dict[str, Any] | None:
    """The `$vectorSearch` filter, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        return {TERMS_FIELD: {"$in": [_term(tag(f.field, v)) for v in values]}} if values else None
    if isinstance(f, And):
        parts = [_vector_filter(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else {"$and": parts}
    if isinstance(f, Or):
        kept = [p for p in (_vector_filter(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"$or": kept}
    raise TypeError(f"not a filter: {f!r}")


def filter_to_search(f: Filter) -> dict[str, Any]:
    """Translate a filter into an Atlas Search operator on the tagged terms, for `$search`: `in` for
    each `In`, and `compound` for `And` and `Or`. Raises `NothingMatches` when nothing can match."""
    return required(_search_operator(f))


def _search_operator(f: Filter) -> dict[str, Any] | None:
    """The Atlas Search operator, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        return {"in": {"path": TERMS_FIELD, "value": [_term(tag(f.field, v)) for v in values]}} if values else None
    if isinstance(f, And):
        parts = [_search_operator(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else {"compound": {"filter": parts}}
    if isinstance(f, Or):
        kept = [p for p in (_search_operator(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"compound": {"should": kept, "minimumShouldMatch": 1}}
    raise TypeError(f"not a filter: {f!r}")


def _create_index(collection: Any, model: Any, name: str, wait: bool, timeout: float) -> None:
    database = collection.database
    if collection.name not in database.list_collection_names():
        database.create_collection(collection.name)
    collection.create_search_index(model)
    if not wait:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(index.get("queryable") for index in collection.list_search_indexes(name)):
            return
        time.sleep(0.5)
    raise TimeoutError(f"search index {name!r} was not queryable after {timeout} seconds")


def create_search_index(
    collection: Any,
    dim: int,
    *,
    name: str = "wardowl",
    vector_field: str = "embedding",
    similarity: str = "cosine",
    wait: bool = True,
    timeout: float = 120.0,
) -> None:
    """Create the vector search index `MongoDBBackend` uses, creating the collection if needed.

    The index holds the vector and, as filter fields, the permission terms, the write marker and
    `_id`. With `wait`, it returns once the index is queryable, or raises `TimeoutError`.
    """
    from pymongo.operations import SearchIndexModel

    definition = {
        "fields": [
            {"type": "vector", "path": vector_field, "numDimensions": dim, "similarity": similarity},
            {"type": "filter", "path": TERMS_FIELD},
            {"type": "filter", "path": WRITE_FIELD},
            {"type": "filter", "path": "_id"},
        ]
    }
    model = SearchIndexModel(definition=definition, name=name, type="vectorSearch")
    _create_index(collection, model, name, wait, timeout)


def create_text_index(
    collection: Any,
    *,
    name: str = "wardowl_text",
    content_field: str = "content",
    wait: bool = True,
    timeout: float = 120.0,
) -> None:
    """Create the Atlas Search index that `mode="keyword"` searches, creating the collection if needed.

    The index holds the text, with the `lucene.standard` analyzer, which keeps every word, and as
    tokens the permission terms, the write marker and `_id`. With `wait`, it returns once the index
    is queryable, or raises `TimeoutError`.
    """
    from pymongo.operations import SearchIndexModel

    definition = {
        "mappings": {
            "dynamic": False,
            "fields": {
                content_field: {"type": "string", "analyzer": "lucene.standard"},
                TERMS_FIELD: {"type": "token"},
                WRITE_FIELD: {"type": "token"},
                "_id": {"type": "token"},
            },
        }
    }
    model = SearchIndexModel(definition=definition, name=name, type="search")
    _create_index(collection, model, name, wait, timeout)


def _newer_than(revision: int) -> dict[str, Any]:
    """An aggregation expression, true for a stored chunk whose revision is newer than `revision`.

    It compares in BSON order, in which a missing field comes before every number, so a chunk
    stored without a revision is never newer. `add`, `set_metadata` and the look before them use
    this one expression, so they agree on which chunks are newer.
    """
    return {"$gt": ["$" + REVISION_FIELD, revision]}


def _unless_newer(document: dict[str, Any], revision: int) -> list[dict[str, Any]]:
    """An update pipeline that replaces the stored chunk with `document`, unless the stored one is
    newer than `revision`, in which case it keeps it exactly as it was. With `upsert`, a chunk not
    stored is created from `document`. `$literal` keeps the document data: a value that starts with
    `$` is stored as written, never read as a field path or an operator.
    """
    return [{"$replaceWith": {"$cond": [_newer_than(revision), "$$ROOT", {"$literal": document}]}}]


class MongoDBBackend:
    """Vector search on MongoDB Atlas Vector Search, with the permission filter applied inside the search.

    `embed` turns text into a vector (your embedding model). `index` names the vector search index.
    `num_candidates` is how many neighbours the approximate search considers (raised to `k`, at
    most 10,000): higher finds more of the true nearest chunks and takes longer. `exact=True`
    searches every chunk instead, and makes `exact_search` True in vector mode, so the retriever
    reports a short result as complete. `wait_timeout` bounds how long `add` and `delete` wait for the
    index. Pass `async_collection` (the same collection from an `AsyncMongoClient`) for a native
    `asearch`; otherwise async calls run `search` in a thread. Without `embed`, search with
    `Query(vector=...)` and pass `embeddings` to `add`.

    `mode="keyword"` searches the text with Atlas Search (BM25) instead, in the index named
    `text_index` (`"wardowl_text"` by default; see `create_text_index`), under the same
    permission filter. Scores are Atlas Search's, higher for better matches. Such a backend needs
    no `embed`: without one, `add` writes chunks with no vector, which keyword search still finds.
    A vector backend given `text_index` also waits for that index after each write, so writes
    through it are searchable by both halves of hybrid search when they return.
    """

    supports_filters = True

    def __init__(
        self,
        collection: Any,
        embed: Embed | None = None,
        *,
        mode: str = "vector",
        index: str = "wardowl",
        text_index: str | None = None,
        vector_field: str = "embedding",
        content_field: str = "content",
        metadata_field: str = "metadata",
        num_candidates: int = 100,
        exact: bool = False,
        wait_timeout: float = 60.0,
        async_collection: Any = None,
    ) -> None:
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        if mode == "keyword" and text_index is None:
            text_index = "wardowl_text"
        if not index or text_index == "":
            raise ValueError("index and text_index must be non-empty names")
        if num_candidates < 1 or wait_timeout <= 0:
            raise ValueError("num_candidates and wait_timeout must be positive")
        self.mode = mode
        self.collection = collection
        self.async_collection = async_collection
        self.embed = embed
        self.index = index
        self.text_index = text_index
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.num_candidates = num_candidates
        self.exact = exact
        self.wait_timeout = wait_timeout

    @property
    def exact_search(self) -> bool:
        """True for a vector backend created with `exact=True`: `$vectorSearch` then runs an exact
        (ENN) search, which scores every indexed vector the filter admits, so fewer than k hits
        means that the index holds no other matching chunk with a vector.

        Only what the index has caught up with counts. Writes through this backend wait for the
        index by default; a chunk written with `wait=False`, or by another tool, can be missing
        from a search made just after it, and a chunk without a vector is never in the vector
        index.
        """
        return self.mode == "vector" and bool(self.exact)

    # ---- writing -----------------------------------------------------------------------------

    def _document(self, doc: Document, vector: Sequence[float] | None, marker: str) -> dict[str, Any]:
        document: dict[str, Any] = {
            "_id": doc.id,
            self.content_field: doc.text,
            self.metadata_field: dict(doc.metadata),
            TERMS_FIELD: _terms(doc.id, doc.metadata),
            WRITE_FIELD: marker,
        }
        if vector is not None:
            document[self.vector_field] = [float(x) for x in vector]
        return document

    def add(
        self,
        documents: Iterable[Document],
        embeddings: Iterable[Sequence[float]] | None = None,
        *,
        wait: bool = True,
        revision: int | None = None,
    ) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given.

        With `wait` (the default), returns once the search index shows every chunk as written,
        or raises `TimeoutError` after `wait_timeout` seconds.

        With `revision`, the revision of the source document these chunks were read from, a chunk
        stored with a newer revision (in `wardowl_revision`) is not replaced, and the call raises
        `StaleRevision`. The call first looks for such chunks, and if it finds one, writes nothing.
        Each chunk is then compared and replaced in one atomic update, so a newer write that lands
        after that look is not replaced either; the call is not one transaction, though, so the
        other chunks are then written, and it raises once they are searchable. A chunk not stored
        has no revision, and is written. Without `revision`, the write is unconditional, as
        before, and replaces the whole document, stored revision included.
        """
        from pymongo import ReplaceOne, UpdateOne

        docs = list(documents)
        vectors: Sequence[Sequence[float] | None]
        if embeddings is None and self.embed is None and self.mode == "keyword":
            vectors = [None] * len(docs)  # a keyword-only backend without embed writes no vector
        else:
            vectors = document_vectors(docs, embeddings, self.embed)
        # The last version of each id wins, as it would in sequence; each id is then written once,
        # so every batch's marker stays on all of its chunks.
        latest = list({d.id: (d, v) for d, v in zip(docs, vectors, strict=True)}.values())
        number = None if revision is None else revision_number(revision)
        if number is not None:
            self._refuse_newer([d.id for d, _ in latest], number)
        batches: list[tuple[str, int]] = []
        refused: list[str] = []
        for start in range(0, len(latest), _BATCH):
            marker = uuid.uuid4().hex
            rows = [self._document(d, v, marker) for d, v in latest[start : start + _BATCH]]
            if number is None:
                self.collection.bulk_write([ReplaceOne({"_id": r["_id"]}, r, upsert=True) for r in rows], ordered=False)
                batches.append((marker, len(rows)))
                continue
            changes = [
                UpdateOne({"_id": r["_id"]}, _unless_newer(r | {REVISION_FIELD: number}, number), upsert=True)
                for r in rows
            ]
            result = self.collection.bulk_write(changes, ordered=False)
            # A kept chunk is left exactly as it was, so it is neither modified nor upserted.
            written = result.modified_count + result.upserted_count
            if written < len(rows):
                refused += self._newer([r["_id"] for r in rows], number)
            batches.append((marker, written))
        if wait and latest:
            first = latest[0][1]
            probe = [float(x) for x in first] if self.mode == "vector" and first is not None else None
            for marker, expected in batches:
                self._settle({WRITE_FIELD: marker}, {"in": {"path": WRITE_FIELD, "value": [marker]}}, expected, probe)
        if number is not None and refused:
            raise StaleRevision(refused, number)

    def set_metadata(
        self, metadata: Mapping[str, Mapping[str, Any]], *, wait: bool = True, revision: int | None = None
    ) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        With `wait` (the default), returns once the search index shows the new metadata, or raises
        `TimeoutError` after `wait_timeout` seconds.

        With `revision`, a chunk stored with a newer revision keeps its metadata, and the call
        raises `StaleRevision`, as `add` does: it writes nothing if it finds such a chunk before
        writing, and otherwise compares each chunk in the update that writes it. The chunks it
        writes store `revision`; without one, the stored revision is left as it is.
        """
        from pymongo import UpdateOne

        items = list(metadata.items())
        number = None if revision is None else revision_number(revision)
        if number is not None:
            self._refuse_newer([doc_id for doc_id, _ in items], number)
        batches: list[tuple[str, int]] = []
        refused: list[str] = []
        for start in range(0, len(items), _BATCH):
            marker = uuid.uuid4().hex
            part = items[start : start + _BATCH]
            changes: list[Any] = []
            for doc_id, new in part:
                fields = {self.metadata_field: dict(new), TERMS_FIELD: _terms(doc_id, new), WRITE_FIELD: marker}
                if number is None:
                    changes.append(UpdateOne({"_id": doc_id}, {"$set": fields}))
                else:
                    # The filter matches a chunk only when it is not newer, in the update that writes it.
                    unless_newer = {"_id": doc_id, "$expr": {"$not": [_newer_than(number)]}}
                    changes.append(UpdateOne(unless_newer, {"$set": fields | {REVISION_FIELD: number}}))
            result = self.collection.bulk_write(changes, ordered=False)
            if number is not None and result.matched_count < len(part):
                refused += self._newer([doc_id for doc_id, _ in part], number)
            if result.matched_count:
                batches.append((marker, result.matched_count))
        if wait and batches:
            probe = self._probe()
            for marker, expected in batches:
                self._settle({WRITE_FIELD: marker}, {"in": {"path": WRITE_FIELD, "value": [marker]}}, expected, probe)
        if number is not None and refused:
            raise StaleRevision(refused, number)

    def _newer(self, ids: Sequence[str], revision: int) -> list[str]:
        """The chunks among `ids` stored with a revision newer than `revision`, in the order given."""
        found: list[str] = []
        for start in range(0, len(ids), _BATCH):
            part = list(ids[start : start + _BATCH])
            query = {"_id": {"$in": part}, "$expr": _newer_than(revision)}
            newer = {d["_id"] for d in self.collection.find(query, {"_id": 1})}
            found += [i for i in part if i in newer]
        return found

    def _refuse_newer(self, ids: Sequence[str], revision: int) -> None:
        """Raise `StaleRevision`, before anything is written, if a chunk among `ids` is newer."""
        newer = self._newer(ids, revision)
        if newer:
            raise StaleRevision(newer, revision)

    def delete(self, ids: Iterable[str], *, wait: bool = True) -> None:
        """Delete chunks by id; with `wait`, returns once the search index no longer returns them."""
        selected = list(dict.fromkeys(ids))
        if not selected:
            return
        probe = self._probe() if wait else None
        self.collection.delete_many({"_id": {"$in": selected}})
        if wait:
            for start in range(0, len(selected), _MAX_CANDIDATES):
                part = selected[start : start + _MAX_CANDIDATES]
                self._settle({"_id": {"$in": part}}, {"in": {"path": "_id", "value": part}}, 0, probe)

    def _probe(self) -> list[float] | None:
        """A query vector for the exact searches that wait for the vector index, which only count
        matches; None for a keyword backend, which does not wait for a vector index."""
        if self.mode == "keyword":
            return None
        if self.embed is not None:
            return [float(x) for x in self.embed("wardowl")]
        # Without an embed function, any stored vector has the index's dimensions. With none
        # stored, the index has nothing to return and there is nothing to wait for.
        stored = self.collection.find_one({self.vector_field: {"$exists": True}}, {self.vector_field: 1})
        return [float(x) for x in stored[self.vector_field]] if stored else None

    def _count(self, flt: dict[str, Any], probe: list[float]) -> int:
        """How many chunks matching `flt` the vector search index returns, by an exact search."""
        stage: dict[str, Any] = {
            "index": self.index,
            "path": self.vector_field,
            "queryVector": probe,
            "exact": True,
            "limit": _MAX_CANDIDATES,
            "filter": flt,
        }
        return len(list(self.collection.aggregate([{"$vectorSearch": stage}, {"$project": {"_id": 1}}])))

    def _count_text(self, operator: dict[str, Any]) -> int:
        """How many chunks matching `operator` the text index returns."""
        pipeline = [
            {"$search": {"index": self.text_index, **operator}},
            {"$limit": _MAX_CANDIDATES},
            {"$project": {"_id": 1}},
        ]
        return len(list(self.collection.aggregate(pipeline)))

    def _settle(self, flt: dict[str, Any], operator: dict[str, Any], expected: int, probe: list[float] | None) -> None:
        """Wait until each index this backend uses returns `expected` chunks for the change."""
        if probe is not None:
            self._wait(partial(self._count, flt, probe), expected)
        if self.text_index is not None:
            self._wait(partial(self._count_text, operator), expected)

    def _wait(self, count: Callable[[], int], expected: int) -> None:
        deadline = time.monotonic() + self.wait_timeout
        while True:
            if count() == expected:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the search index did not catch up within {self.wait_timeout} seconds")
            time.sleep(0.2)

    # ---- searching ---------------------------------------------------------------------------

    def _pipeline(self, query: Query, k: int, f: Filter | None) -> list[dict[str, Any]] | None:
        if self.mode == "keyword":
            return self._keyword_pipeline(query, k, f)
        if k > _MAX_CANDIDATES:  # Atlas would answer with 10,000 chunks, as if no more matched
            raise ValueError(f"k={k} is more than one Atlas Vector Search returns ({_MAX_CANDIDATES:,})")
        stage: dict[str, Any] = {
            "index": self.index,
            "path": self.vector_field,
            "queryVector": query.embedded(self.embed),
            "limit": k,
        }
        if self.exact:
            stage["exact"] = True
        else:
            stage["numCandidates"] = min(max(self.num_candidates, k), _MAX_CANDIDATES)
        if f is not None:
            mongo_filter = _vector_filter(f)
            if mongo_filter is None:
                return None  # nothing can match
            stage["filter"] = mongo_filter
        projection = {self.content_field: 1, self.metadata_field: 1, "score": {"$meta": "vectorSearchScore"}}
        return [{"$vectorSearch": stage}, {"$project": projection}]

    def _keyword_pipeline(self, query: Query, k: int, f: Filter | None) -> list[dict[str, Any]] | None:
        text = query.keywords()
        if not _WORD.search(text):
            return None  # no word to match
        match = {"text": {"query": text, "path": self.content_field}}
        search: dict[str, Any] = {"index": self.text_index, **match}
        if f is not None:
            operator = _search_operator(f)
            if operator is None:
                return None  # nothing can match
            search = {"index": self.text_index, "compound": {"must": [match], "filter": [operator]}}
        projection = {self.content_field: 1, self.metadata_field: 1, "score": {"$meta": "searchScore"}}
        return [{"$search": search}, {"$limit": k}, {"$project": projection}]

    def _hits(self, documents: Iterable[Mapping[str, Any]]) -> list[Hit]:
        hits = []
        for d in documents:
            text = d.get(self.content_field)
            metadata = d.get(self.metadata_field)
            hits.append(
                Hit(
                    str(d.get("_id")),
                    text if isinstance(text, str) else "",
                    float(d.get("score", 0.0)),
                    # A document without a metadata object carries no permissions: the re-check denies it.
                    dict(metadata) if isinstance(metadata, Mapping) else {},
                )
            )
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        pipeline = self._pipeline(Query.of(query), k, filter)
        if pipeline is None:
            return []
        return self._hits(self.collection.aggregate(pipeline))

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_collection is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        pipeline = await asyncio.to_thread(self._pipeline, Query.of(query), k, filter)
        if pipeline is None:
            return []
        cursor = await self.async_collection.aggregate(pipeline)
        return self._hits(await cursor.to_list(None))

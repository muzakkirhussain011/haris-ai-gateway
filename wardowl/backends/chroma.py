"""Chroma backend: the permission filter becomes Chroma's `where` clause.

Install with `pip install "wardowl[chroma]"`. Pass a Chroma collection; this module never imports
chromadb itself.

How metadata is stored, and why
-------------------------------
wardowl does not use Chroma's array metadata. In chromadb 1.5.x (local mode), deleting a collection
leaves its array metadata behind and SQLite reuses record ids, so new records in other collections
come back with those arrays and `where` filters use them (chroma-core/chroma#7594). For permission
data that is a leak, so:

* string and integer values are stored as ordinary scalar fields (`team: "eng"`);
* each string or integer inside a list becomes its own boolean field
  (`allowed_groups: ["hr", "eng"]` becomes `allowed_groups::s:hr` and `allowed_groups::s:eng`);
* an exact JSON copy of the original metadata is stored under `wardowl_metadata`, and search
  results are rebuilt from that copy, never from what Chroma returns alongside it. The retriever's
  re-check therefore always sees the true permissions.

Field names, in metadata and in filters, may not contain `::`, and `wardowl_metadata` is reserved:
either would address a list key or the JSON copy rather than a field. Values other than strings
and integers (booleans, floats, nested objects) are kept in the JSON copy only, which matches the
reference semantics: no filter ever matches them.

How large a filter Chroma is sent, and why
------------------------------------------
Chroma 1.5 turns a `where` into one SQLite statement. Each `$and` and `$or` becomes a chain of
binary ANDs or ORs, one per clause, and SQLite refuses an expression more than 1,000 deep, so a
flat `$or` for a subject in about 1,000 groups failed every search. With 40,000 groups, a search
through chromadb 1.5.9's embedded client ended the Python process with SIGSEGV, without an
exception. So:

* every `$and` and `$or` holds at most 16 clauses, nested as a balanced tree, which keeps the
  expression shallow (see `_join`);
* a `where` is sent only while it stays within bounds far below what Chroma and SQLite refuse or
  crash on (see `_MAX_PARAMETERS`). `filter_to_where` raises `FilterTooLarge` beyond them, and
  `ChromaBackend.search` then sends only the part of the filter that fits, if any, and applies the
  whole filter to the candidates itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from wardowl.backends._errors import required
from wardowl.filters import And, Filter, In, Or, _is_value, _ordered, matches
from wardowl.types import Document, Hit, Query

__all__ = ["JSON_KEY", "ChromaBackend", "FilterTooLarge", "filter_to_where"]

Embed = Callable[[str], Sequence[float]]

JSON_KEY = "wardowl_metadata"
"""Reserved field holding the exact JSON copy of a chunk's metadata."""

_SEP = "::"
_LISTS = (list, tuple, set, frozenset)
_DEFAULT_BATCH = 1000  # records per write when the collection's client reports no maximum
_DEFAULT_FALLBACK = 1000  # candidates of a search whose filter is too large to send whole
_JOINS = ("$and", "$or")

# Chroma 1.5 renders a `where` as SQL in rust/segment/src/sqlite_metadata.rs (chroma-core/chroma,
# tag 1.5.9). `CompositeExpression::eval` starts from a bound `true` (`$and`) or `false` (`$or`)
# and folds in one child at a time, `Expr::expr(expr).or(child.eval())`, which SQLite parses as a
# left-deep chain: its depth grows by one per clause. A child that is itself an `$and` or `$or` is
# the right operand of its binary operator, which sea-query (0.32, `binary_expr`) writes in
# parentheses, so it stays a subtree of its own. A tree of nodes of at most `_FANOUT` clauses is
# therefore at most `_FANOUT` deep per level, and the levels grow with the logarithm of the number
# of clauses. Chroma's client refuses an `$and` or `$or` of fewer than two clauses.
_FANOUT = 16

# The bounds a `where` must keep to be sent. SQLite refuses an expression more than 1,000 deep and,
# by default, a statement with more than 32,766 parameters (https://sqlite.org/limits.html,
# SQLITE_MAX_EXPR_DEPTH and SQLITE_MAX_VARIABLE_NUMBER); Chroma binds every value as a parameter.
# Chroma's Rust core parses the JSON with serde_json, which refuses more than 128 levels of nesting
# (`remaining_depth: 128` in serde_json's de.rs); each `$and` or `$or` is two. With chromadb 1.5.9's
# embedded client, the flat `$or` of a subject in 10,000 groups (an expression 10,000 deep, about
# 30,000 parameters) was refused with an error, and that of a subject in 40,000 groups (2.66 million
# characters) ended the process with SIGSEGV, for a reason not established. Each bound below is
# about half or less of the limit it keeps clear of, and a third or less of what failed.
_MAX_DEPTH = 250
_MAX_PARAMETERS = 10_000
_MAX_NESTING = 32
_MAX_CHARACTERS = 500_000
# The depth of one comparison: Chroma writes it as `id IN (SELECT id FROM embedding_metadata WHERE
# key = ? AND key IS NOT NULL AND <value test>)`, about five levels; the rest is for the statement
# around the `where`.
_LEAF_DEPTH = 10


class FilterTooLarge(ValueError):
    """The filter's `where` is larger than wardowl sends to Chroma.

    Raised by `filter_to_where`. `ChromaBackend.search` does not raise it: it sends the part of the
    filter that fits, if any, and applies the whole filter to the candidates itself.
    """


def _flat_key(field: str, value: str | int) -> str:
    return f"{field}{_SEP}{'i' if isinstance(value, int) else 's'}:{value}"


def _join(op: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    """`parts` joined by `op` ("$and" or "$or"): a balanced tree of nodes of 2 to `_FANOUT` clauses.

    A flat node of n clauses makes Chroma's SQL n levels deep (see `_FANOUT`); this tree adds at
    most `_FANOUT` levels per level of nodes: about 50 in all for a thousand clauses, 70 for 50,000.
    """
    while len(parts) > _FANOUT:
        count = len(parts)
        nodes = -(-count // _FANOUT)  # the fewest nodes of at most _FANOUT clauses, as even as can be
        chunks = [parts[i * count // nodes : (i + 1) * count // nodes] for i in range(nodes)]
        parts = [chunk[0] if len(chunk) == 1 else {op: chunk} for chunk in chunks]  # one: only with a fan-out of 2
    return parts[0] if len(parts) == 1 else {op: parts}


class _Cost(NamedTuple):
    """Upper bounds on what Chroma 1.5's SQLite query makes of a `where`."""

    depth: int  # of the SQL expression
    parameters: int  # values bound into the statement
    nesting: int  # levels of $and and $or
    characters: int  # of the JSON that chromadb's client hands to its Rust core (`json.dumps(where)`)

    def fits(self) -> bool:
        return (
            self.depth <= _MAX_DEPTH
            and self.parameters <= _MAX_PARAMETERS
            and self.nesting <= _MAX_NESTING
            and self.characters <= _MAX_CHARACTERS
        )


def _leaf_cost(leaf: dict[str, Any]) -> _Cost:
    """The cost of one of the two comparisons `_where` writes: `$in` on a field, `$eq: True` on a list key."""
    test = next(iter(leaf.values()))
    values = test.get("$in")
    if values is None:  # the key, and True
        depth, parameters = _LEAF_DEPTH, 2
    elif isinstance(values[0], str):  # the key, and each value
        depth, parameters = _LEAF_DEPTH, 1 + len(values)
    else:  # integers are tested against the float column too: twice that, joined by one more OR
        depth, parameters = _LEAF_DEPTH + 1, 2 + 2 * len(values)
    return _Cost(depth, parameters, 0, len(json.dumps(leaf)))


def _node_cost(op: str, costs: Sequence[_Cost]) -> _Cost:
    """The cost of an `op` node over clauses of these costs. The chain Chroma writes holds one
    binary operator per clause and a bound `true` or `false`, and its first clause is the deepest."""
    return _Cost(
        depth=len(costs) + max(c.depth for c in costs),
        parameters=1 + sum(c.parameters for c in costs),
        nesting=1 + max(c.nesting for c in costs),
        characters=len(json.dumps({op: []})) + sum(c.characters for c in costs) + 2 * (len(costs) - 1),
    )


def _measure(where: dict[str, Any]) -> _Cost:
    op, body = next(iter(where.items()))
    return _node_cost(op, [_measure(c) for c in body]) if op in _JOINS else _leaf_cost(where)


def _within(where: dict[str, Any]) -> tuple[dict[str, Any], _Cost] | None:
    """`where` and its cost when it fits the bounds. Otherwise a weaker condition that every chunk
    `where` matches also meets, and that fits, or None when this finds none.

    A clause of an `$and` is such a condition, and so is an `$or` of one for each of its clauses.
    """
    op, body = next(iter(where.items()))
    if op not in _JOINS:
        cost = _leaf_cost(where)
        return (where, cost) if cost.fits() else None
    chosen: list[tuple[dict[str, Any], _Cost]] = []
    for child in body:
        part = _within(child)
        if op == "$or":
            # A chunk can match through any clause, so each needs a condition, and together they must fit.
            if part is None:
                return None
            chosen.append(part)
        elif part is not None and (not chosen or _node_cost(op, [c for _, c in [*chosen, part]]).fits()):
            # Each clause of an $and holds for every chunk it matches: keep, in order, those that fit together.
            chosen.append(part)
    if not chosen:
        return None
    if len(chosen) == 1:
        return chosen[0]
    cost = _node_cost(op, [c for _, c in chosen])
    if not cost.fits():
        return None  # an $or whose conditions do not fit together
    kept = [w for w, _ in chosen]
    same = len(kept) == len(body) and all(a is b for a, b in zip(kept, body, strict=True))
    return (where if same else {op: kept}), cost


def filter_to_where(f: Filter) -> dict[str, Any]:
    """Translate a filter into a Chroma `where` document.

    `In` becomes `$in` on the scalar field (one clause per value type, since Chroma's `$in` lists
    are single-typed) plus an equality test on each value's list key. `$and` and `$or` hold at most
    16 clauses each, nested as a balanced tree, since Chroma's SQL is one level deeper per clause.
    Raises `NothingMatches` when nothing can match, as Chroma reads an empty `where` as "match
    everything"; `ValueError` for a field name containing `::` or equal to `wardowl_metadata`,
    which would address the stored list keys or the JSON copy rather than a metadata field; and
    `FilterTooLarge` for a `where` beyond the bounds Chroma is sent: 10,000 bound SQL parameters
    (about 3,200 groups with short names), an SQL expression 250 deep, 32 levels of `$and` and
    `$or`, or 500,000 characters of JSON.
    """
    where = required(_where(f))
    cost = _measure(where)
    if not cost.fits():
        raise FilterTooLarge(
            f"the filter's where is too large to send to Chroma: {cost.parameters:,} SQL parameters "
            f"(at most {_MAX_PARAMETERS:,}), an expression {cost.depth} deep (at most {_MAX_DEPTH}), "
            f"{cost.nesting} levels of $and and $or (at most {_MAX_NESTING}), {cost.characters:,} "
            f"characters (at most {_MAX_CHARACTERS:,})"
        )
    return where


def _where(f: Filter) -> dict[str, Any] | None:
    """The Chroma `where` document, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field or f.field == JSON_KEY:
            raise ValueError(f"field {f.field!r} cannot be filtered in Chroma (contains '::', or is {JSON_KEY!r})")
        values = _ordered(f.values)
        clauses: list[dict[str, Any]] = []
        for kind in (int, str):
            same = [v for v in values if isinstance(v, kind)]
            if same:
                clauses.append({f.field: {"$in": same}})
        clauses += [{_flat_key(f.field, v): {"$eq": True}} for v in values]
        return _join("$or", clauses) if clauses else None
    if isinstance(f, And):
        found = [_where(c) for c in f.clauses]
        parts = [p for p in found if p is not None]
        return _join("$and", parts) if len(parts) == len(found) else None
    if isinstance(f, Or):
        parts = [p for p in (_where(c) for c in f.clauses) if p is not None]
        return _join("$or", parts) if parts else None
    raise TypeError(f"not a filter: {f!r}")


def _encode_metadata(doc_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    for key in metadata:
        if not isinstance(key, str) or not key or _SEP in key or key == JSON_KEY:
            raise ValueError(f"{doc_id}: metadata key {key!r} is not allowed (no '::', not {JSON_KEY!r})")
    try:
        stored: dict[str, Any] = {JSON_KEY: json.dumps(dict(metadata), sort_keys=True)}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{doc_id}: metadata must be JSON-serialisable: {exc}") from exc
    for key, value in metadata.items():
        if isinstance(value, _LISTS):
            for item in value:
                if _is_value(item):
                    stored[_flat_key(key, item)] = True
        elif _is_value(value):
            stored[key] = value
    return stored


def _decode_metadata(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    # Records written by something other than wardowl have no trusted copy: treat them as
    # carrying no permissions, so the re-check denies them.
    if not stored or not isinstance(stored.get(JSON_KEY), str):
        return {}
    metadata = json.loads(stored[JSON_KEY])
    return metadata if isinstance(metadata, dict) else {}


def _max_batch_size(collection: Any) -> int:
    """The most records Chroma accepts in one call, as the collection's client reports it, or
    `_DEFAULT_BATCH` when the collection does not say."""
    client = getattr(collection, "_client", None)
    ask = getattr(client, "get_max_batch_size", None)
    size: object = ask() if callable(ask) else None
    if isinstance(size, int) and not isinstance(size, bool) and size >= 1:
        return size
    return _DEFAULT_BATCH


class ChromaBackend:
    """Search a Chroma collection with the permission filter applied by Chroma.

    If `embed` is given, queries and added documents are embedded with it; otherwise the
    collection's own embedding function is used. A `Query` with a vector is searched with that
    vector. Scores are `1 - distance`, so create the collection with cosine distance for scores in
    [-1, 1].

    `add`, `delete` and `set_metadata` send at most `batch_size` records per call, since Chroma
    refuses larger batches. By default it is the client's own maximum (`get_max_batch_size()`,
    about 5,400 for a local client), or 1,000 when the collection does not report one. Each write is
    checked whole before the first batch is sent; a store that fails part way leaves the batches
    before it written.

    A filter whose `where` is beyond the bounds `filter_to_where` names, such as the filter of a
    subject in more than about 3,200 groups, is never sent whole: Chroma refuses a large one, and a
    larger one crashed the process. The search then sends only the part of it every match must meet
    and that fits (a tenant, say), or no `where` at all, asks for `fallback_candidates` chunks (or
    `k`, if more), and applies the filter to them itself. It never returns a chunk the filter
    excludes, but can return fewer than `k` hits, or none, though more chunks match.
    """

    supports_filters = True

    def __init__(
        self,
        collection: Any,
        embed: Embed | None = None,
        *,
        batch_size: int | None = None,
        fallback_candidates: int = _DEFAULT_FALLBACK,
    ) -> None:
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be positive")
        if fallback_candidates < 1:
            raise ValueError("fallback_candidates must be positive")
        self.collection = collection
        self.embed = embed
        self.batch_size = batch_size if batch_size is not None else _max_batch_size(collection)
        self.fallback_candidates = fallback_candidates

    def _batches(self, count: int) -> range:
        """The start of each batch of `count` records."""
        return range(0, count, self.batch_size)

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        docs = list(documents)
        if not docs:
            return
        vectors = None
        if embeddings is not None:
            vectors = [list(map(float, v)) for v in embeddings]
            if len(vectors) != len(docs):
                raise ValueError("one embedding per document is required")
        elif self.embed is not None:
            vectors = [list(map(float, self.embed(d.text))) for d in docs]
        # The last version of each id wins, as it would in sequence.
        keep = sorted({d.id: i for i, d in enumerate(docs)}.values())
        # Every record is encoded, and so checked, before the first batch is written.
        ids = [docs[i].id for i in keep]
        texts = [docs[i].text for i in keep]
        metadatas = [_encode_metadata(docs[i].id, docs[i].metadata) for i in keep]
        chosen = None if vectors is None else [vectors[i] for i in keep]
        for start in self._batches(len(keep)):
            end = start + self.batch_size
            records: dict[str, Any] = {
                "ids": ids[start:end],
                "documents": texts[start:end],
                "metadatas": metadatas[start:end],
            }
            if chosen is not None:
                records["embeddings"] = chosen[start:end]
            self._replace(records)

    def _replace(self, records: dict[str, Any]) -> None:
        # Delete, then add: a replaced chunk keeps none of its old metadata keys, whatever an
        # update would merge.
        self.collection.delete(ids=records["ids"])
        self.collection.add(**records)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        An update, which Chroma applies to stored records only: a chunk deleted meanwhile is not
        recreated, and its text and vector are never read and written back. Keys of the stored
        metadata that the new one lacks are removed (set to None), so no old list key survives.
        The read and the update are two calls, so an `add` between them can leave list keys the
        update does not know of; `search` checks each hit's JSON copy against the filter, so such a
        key never makes a chunk match.
        """
        encoded = {doc_id: _encode_metadata(doc_id, new) for doc_id, new in metadata.items()}
        names = list(encoded)
        for start in self._batches(len(names)):
            stored = self.collection.get(ids=names[start : start + self.batch_size], include=["metadatas"])
            found = list(stored["ids"])
            if not found:
                continue
            updates: list[dict[str, Any]] = []
            for doc_id, old in zip(found, stored["metadatas"], strict=True):
                new = encoded[doc_id]
                updates.append({**{key: None for key in old or {} if key not in new}, **new})
            self.collection.update(ids=found, metadatas=updates)

    def delete(self, ids: Iterable[str]) -> None:
        chosen = list(ids)
        for start in self._batches(len(chosen)):
            self.collection.delete(ids=chosen[start : start + self.batch_size])

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        q = Query.of(query)
        where = _where(filter) if filter is not None else None
        if k < 1 or (filter is not None and where is None) or self.collection.count() == 0:
            return []  # nothing asked for, nothing can match, or nothing stored
        fetch = k
        if where is not None:
            fitted = _within(where)
            if fitted is None or fitted[0] is not where:
                # Too large to send whole (see _MAX_PARAMETERS): search under the part every match
                # must meet that fits, or under none, and apply the filter to the candidates below.
                fetch = max(k, self.fallback_candidates)
            where = None if fitted is None else fitted[0]
        kwargs: dict[str, Any] = {"n_results": fetch, "include": ["documents", "metadatas", "distances"]}
        if where is not None:
            kwargs["where"] = where
        if q.vector is not None or self.embed is not None:
            kwargs["query_embeddings"] = [q.embedded(self.embed)]
        else:
            kwargs["query_texts"] = [q.keywords()]  # the collection's embedding function embeds it
        result = self.collection.query(**kwargs)
        rows = zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        )
        hits = [Hit(i, t or "", 1.0 - float(d), _decode_metadata(m)) for i, t, m, d in rows]
        if filter is not None:
            # Chroma matched its own fields, which can disagree with the JSON copy: an `add` landing
            # between `set_metadata`'s read and update leaves the list keys it wrote, which the
            # update does not know to remove. Only hits whose true metadata the filter matches count.
            # This is also where the filter of a search too large to send whole is applied.
            hits = [h for h in hits if matches(filter, h.metadata)]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:k]

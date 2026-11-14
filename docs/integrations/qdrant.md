# Qdrant

```bash
pip install "wardowl[qdrant]"
```

The permission filter becomes Qdrant's `query_filter`, so Qdrant returns the nearest points the
subject may read. Qdrant applies filters inside its HNSW search and plans restrictive filters with
its payload indexes, which suits large collections where a user may read only a small share.

## Set up

```python
from qdrant_client import QdrantClient

from wardowl.backends.qdrant import QdrantBackend, create_collection

client = QdrantClient(url="http://localhost:6333")  # or QdrantClient(":memory:") for tests
create_collection(client, "docs", dim=768)
backend = QdrantBackend(client, "docs", embed=embed)  # embed: str -> list[float], your embedding model
```

`create_collection(client, name, dim, keyword_fields=("allowed_users", "allowed_groups"))` creates
a cosine-distance collection if it does not exist, and keyword payload indexes on the permission
fields and on `wardowl_id`, which `set_metadata` and `delete` filter on. Without the indexes Qdrant
still filters correctly, but has to check every point; a collection in strict mode refuses filtered
updates on a field without an index, so index `wardowl_id` in collections you create yourself.

On a collection that already exists, `create_collection` first reads its first 16 points (in point
id order, payload keys `wardowl_id` and `needtoknow_id` only) and raises `ValueError`, creating no
index, if one of them holds `needtoknow_id` and no `wardowl_id`: a point that needtoknow 1.x wrote.
Such a point sits at a point id derived another way, so `set_metadata` and `delete` by chunk id
would not reach it, and a revocation would change nothing without saying so. The backend makes the
same check before its first `add`, `set_metadata` or `delete` that has something to write, and
writes nothing if it fails; once a check passes it does not check again. Write such a collection's
chunks into a new one ([Upgrading](../upgrading.md#2-index-your-chunks-again)). The check reads a
sample, so older points after the first 16 are not found; searches are not checked.

The default `keyword_fields` do not include `tenant`, the field
[`TenantAuthorizer`](../combining.md#tenant-isolation) filters on. In a collection that holds
several tenants, add it, so that Qdrant plans the tenant condition with an index:

```python
create_collection(client, "docs", dim=768, keyword_fields=("allowed_users", "allowed_groups", "tenant"))
```

A keyword index serves string tenant ids; for integer ids, create an integer index on
`metadata.tenant` yourself (`field_schema=models.PayloadSchemaType.INTEGER`). Qdrant can also
arrange its storage by tenant when you create the index yourself with
`field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True)`
([Qdrant's multitenancy guide](https://qdrant.tech/documentation/guides/multiple-partitions/)).

## How points are stored

`QdrantBackend` stores and reads points in the layout of LangChain's Qdrant integration: the text
under `page_content`, the metadata under `metadata`. A collection built with LangChain can therefore
be searched with permissions without re-indexing, as long as its metadata carries the permission
fields. Pass `content_key=` and `metadata_key=` for another layout, and `vector_name=` for a named
vector.

Qdrant point ids are unsigned integers or UUIDs. `add` derives a stable UUID from the SHA-256 of
every document id (`point_id`), a document id that is already a UUID included, and keeps the
original under `wardowl_id` in the payload. Search results report that id, or the point id for
points written by something else. A point without a `metadata` object carries no permissions, so
the re-check denies it.

One id never stands for two points in results. A point written by something else at the UUID `u`,
reported as `u`, and a chunk added with the id `u`, stored at `point_id(u)`, would both be reported
as `u`; so would a point holding `wardowl_id` `x` at a point id other than `point_id("x")`, next to
the chunk `x`. When an answer holds a point that is not at `point_id` of the id it would be reported
under, the search looks up that point id with one `retrieve` call. If the point there holds the id,
the other point is left out, and the search asks Qdrant again for as many more points as it left
out, so leaving points out never by itself makes a result shorter than k. When no chunk holds the
id where `add` writes it, the point is reported as before. A collection holding only points that
`add` wrote never needs the extra call.

`set_metadata` and `delete` select points with a filter that also requires `wardowl_id` to equal
the id asked for, so they never change or remove a point that holds another id. A point written by
something else has no `wardowl_id`: it is reached by its UUID written exactly as search results
report it (lower case, with hyphens). `set_metadata` sets only the payload's `metadata` key; the
text and any other payload keys stay as they are, and a point deleted meanwhile is not recreated.

```python
from wardowl import Document

backend.add([Document("salaries-1", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]})])
backend.delete(["salaries-1"])
```

## How filters are translated

| wardowl | Qdrant |
|---|---|
| `In("allowed_groups", ["hr", "eng"])` | `FieldCondition(key="metadata.allowed_groups", match=MatchAny(any=["eng", "hr"]))` |
| `In("level", ["a", 1])` | one `MatchAny` for the strings and one for the integers, under `should` |
| `And(...)` / `Or(...)` | `Filter(must=[...])` / `Filter(should=[...])` |
| `In(field, [])` | matches nothing: the backend returns no hits without calling Qdrant, and `filter_to_qdrant` raises `NothingMatches` |

For a list field, Qdrant matches when any element matches, which is the reference semantics of `In`.
Strings and integers are never mixed in one `MatchAny`, and an empty value list is handled before the
query is built rather than left to Qdrant's handling of empty lists. Field names used in filters
may not contain `.`, `[`, `]` or `"`, which Qdrant reads as path syntax; such a filter raises
`ValueError`, so retrieval fails closed.

The backend passes wardowl's [conformance kit](../extending.md#test-it-with-the-conformance-kit)
in CI against a real Qdrant server (v1.19.2) and against Qdrant's local mode.

## Search quality and async

`search_params=` takes a `qdrant_client.models.SearchParams`: `SearchParams(hnsw_ef=128)` searches
deeper for higher recall, `SearchParams(acorn=models.AcornSearchParams(enable=True,
max_selectivity=1.0))` turns on ACORN ([below](#filters-on-common-values)), and
`SearchParams(exact=True)` searches by brute force. With `exact=True` (and without
`indexed_only=True`, which skips segments still being indexed), the backend declares
`exact_search`, so a result shorter than k proves that the user may read no more and says
`complete=True`. Pass `async_client=AsyncQdrantClient(...)` for a native `aretrieve`; without it,
async calls run the sync search in a thread.

## Filters on common values

Qdrant plans each filtered search per segment, from an estimate of how many points the filter
admits. When it admits few, Qdrant searches them exactly, through the payload index. When it admits
many, Qdrant walks the HNSW graph and skips every neighbour that fails the filter: it is neither
scored nor followed. To keep the permitted points connected, Qdrant adds extra graph edges for the
values of payload fields indexed when the graph is built, per field, never per combination of
fields, and only for values within a size window: a value shared by too large a share of the
points gets no extra edges
([Indexing](https://qdrant.tech/documentation/concepts/indexing/),
[Search](https://qdrant.tech/documentation/search/search/),
[Filtered vector search with ACORN](https://qdrant.tech/articles/filtered-vector-search-acorn/)).

Permission filters often hit exactly that case. A tenant that holds most of the collection, or a
shared corpus tagged with every tenant or readable by everyone, makes the filter's values common,
so they get no extra edges and Qdrant searches the graph. If the points a user may read are few
near the question and cut off from each other in the graph, the search can miss them and return
other permitted chunks instead. In wardowl's acceptance run on Kaggle, where a shared knowledge base
carried every tenant's value, this is the likely reason Qdrant found only 0.56 of the true top 10,
searching with `hnsw_ef=128` and without ACORN. Misses that come back as k other chunks are
invisible to `complete`; a search that returns fewer than k says `complete=False`
([Concepts](../concepts.md#what-a-result-tells-you)).

What helps:

- **ACORN** (Qdrant 1.16 and later) explores the neighbours of filtered-out neighbours, per query,
  with no change to the index, at a cost in latency
  ([Qdrant 1.16](https://qdrant.tech/blog/qdrant-1.16.x/)). It is used only when the planner
  estimates that the filter admits at most `max_selectivity` of the points (0.4 by default; 1.0
  means always), so set it higher for permission filters that admit a large share:
  `SearchParams(hnsw_ef=128, acorn=models.AcornSearchParams(enable=True, max_selectivity=1.0))`.
- **Per-tenant graphs.** A keyword index on the tenant field with `is_tenant=True`, and the
  collection's `hnsw_config` set to `m=0` with `payload_m=16`, builds a graph per tenant and no
  global one ([Multitenancy](https://qdrant.tech/documentation/manage-data/multitenancy/)). Keep a
  corpus that every tenant shares in a collection of its own rather than tagging it with every
  tenant.
- **Exact search**, `SearchParams(exact=True)`, ranks every point the filter admits, at the cost of
  scoring every one of them. The backend then declares `exact_search`, so a result shorter than k
  says `complete=True`.
- **Exact search only when needed**: [`short_results`](#short-results) searches again exactly when
  a count shows that the graph search missed points the user may read.

## Short results

In vector mode the backend offers what [`short_results`](../concepts.md#short-results) uses:

- `count(filter, limit)` asks Qdrant for an exact count of the points the filter admits
  (`client.count(..., count_filter=..., exact=True)`). Qdrant counts them all; `limit` is not
  used. Points that hold only the keyword vector, in a collection created with `text_search=True`,
  are counted too, which can make a short result look missed but never makes one complete.
- `search(..., exact=True)` searches with a copy of `search_params` that sets `exact=True` and
  `indexed_only=False`; the rest, `hnsw_ef` and ACORN included, is kept, and Qdrant ignores it for
  an exact search.

```python
retriever = PermissionedRetriever(backend, authorizer, short_results="exact", max_exact_rows=10_000)
```

Misses that come back as k other chunks, which is how most of Qdrant's misses showed in the
acceptance run, are not short, so `short_results` does not see them. `recall_sample_rate` does: it
runs the exact search for a share of retrievals and reports how much of its top k came back
([Concepts](../concepts.md#short-results)). To compare ACORN with exact search, give
`short_results="widen"` a second backend on the same collection with ACORN on:

```python
acorn = models.SearchParams(hnsw_ef=128, acorn=models.AcornSearchParams(enable=True, max_selectivity=1.0))
wider = QdrantBackend(client, "docs", embed=embed, search_params=acorn)
retriever = PermissionedRetriever(backend, authorizer, short_results="widen", wider=wider)
```

## Keyword search and hybrid search

`mode="keyword"` ranks the chunks by BM25, under the same `query_filter`, with no model to run.
`create_collection(..., text_search=True)` adds a sparse vector, `wardowl_bm25`, with Qdrant's
IDF modifier. The backend writes each chunk's BM25 term weights there, and Qdrant multiplies in the
inverse document frequency of each word at search time, so the score is BM25 and it stays right as
the collection grows. One collection then serves both halves of [hybrid search](../hybrid.md):

```python
from wardowl import RankFusion

create_collection(client, "docs", dim=768, text_search=True)
vector = QdrantBackend(client, "docs", embed=embed, text_search=True)  # writes both vectors
keyword = QdrantBackend(client, "docs", mode="keyword")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Words are runs of letters, digits and underscores, lowercased, and every word is kept: there is no
stemming and no stop-word list. Each word's index in the sparse vector is its CRC-32, and its weight
is BM25's term-frequency part with k1 = 1.2, b = 0.75 and Qdrant's average length of 256 words.
`bm25_terms(text)` computes the same weights, for points written by other tools. Write chunks
through a backend with `text_search=True` (or `mode="keyword"`), or keyword search will not find
them. CI runs the conformance kit in keyword mode and fused with vector search, in Qdrant's local
mode and against Qdrant 1.19.2.

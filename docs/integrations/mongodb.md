# MongoDB Atlas Vector Search

```bash
pip install "wardowl[mongodb]"
```

The backend runs `$vectorSearch` with the permission filter as its pre-filter, so the k nearest
chunks the subject may read come back however few of the collection's chunks that is. It works on
MongoDB Atlas and on local Atlas deployments (the `mongodb/mongodb-atlas-local` image, or
`atlas deployments setup --type local`). CI runs wardowl's
[conformance kit](../extending.md#test-it-with-the-conformance-kit) against
`mongodb/mongodb-atlas-local:8.0.30`; Atlas itself serves the same `$vectorSearch` stage, but CI
does not test it.

```python
from pymongo import MongoClient

from wardowl.backends.mongodb import MongoDBBackend, create_search_index

collection = MongoClient(MONGODB_URI)["rag"]["chunks"]
create_search_index(collection, dim=768)  # waits until the index is queryable
backend = MongoDBBackend(collection, embed=embed)  # embed: str -> list[float]
backend.add(documents)  # waits until the index shows them
```

Each chunk is one document: `_id` (the document id), `content`, `metadata`, `embedding`, and two
fields wardowl maintains, `wardowl_terms` and `wardowl_write` (and `wardowl_revision`, for writes
given a [revision](#revisions)). `create_search_index`
creates the collection if needed and a `vectorSearch` index named `wardowl`, with the vector
(cosine similarity by default) and, as filter fields, the permission terms, the write marker and
`_id`.

## Writes become searchable shortly after

Atlas Vector Search indexes a write a moment after MongoDB stores it, not at once. A retrieval made
in between would not see a new chunk, and, worse for permissions, could still match a replaced
chunk by its old permission terms. wardowl's re-check reads the stored metadata, so such a
chunk is dropped rather than leaked, but `add` and `delete` close the gap by default:

- every `add` writes a fresh value of `wardowl_write` on each chunk, and returns once an exact
  search filtered on that value finds every chunk it wrote. That is also when their new permission
  terms are searchable.
- every `delete` returns once the index no longer returns the deleted ids.

Both raise `TimeoutError` after `wait_timeout` seconds (60 by default). Bulk ingestion that does not
need to read its own writes can pass `wait=False`.

## Revisions

`add` and `set_metadata` replace what is stored, whenever their data was read, so when an indexer
and a permission sync write the same document, a late write from data read before a revocation
undoes it ([One writer at a time for each document](../operations.md#one-writer-at-a-time-for-each-document)).
Run the writes for each document one after another. Pass `revision=`, a number your source raises
on every change to the document, to have an older write refused as well:

```python
from wardowl.backends import StaleRevision

backend.add(chunks, revision=7)  # the indexer, with the revision it read before the document
try:
    backend.set_metadata({cid: {**stored[cid], "allowed_users": []} for cid in chunk_ids}, revision=6)
except StaleRevision as refused:
    refused.ids, refused.revision  # the chunks stored with a newer revision, and 6
```

- The revision is stored on each chunk, in `wardowl_revision`, and a chunk stored with a newer one
  is not replaced. The call raises `StaleRevision` naming those chunks; with `wait`, it first waits
  for the chunks it did write, as usual.
- Before writing, the call looks for chunks stored with a newer revision. If it finds one, it
  writes nothing, chunks not stored before included.
- Each chunk is then compared and written in one atomic update: for `add`, an update pipeline
  (`$replaceWith` with `$cond`) that keeps a newer chunk as it was, with `upsert` for chunks not
  stored; for `set_metadata`, a filter that matches only a chunk that is not newer. A newer write
  that lands after the look is therefore never replaced either. The call is not one transaction,
  though: in that case the chunks that are not newer are written, then the call raises.
- A chunk written at the same revision is replaced, so a retry succeeds. A chunk that is not stored
  has no revision: `add` writes it, and `set_metadata` ignores it, as without a revision.
- A write without `revision` is not compared with anything, as before. `set_metadata` then leaves
  the stored revision as it is; `add` replaces the whole document, so it removes it. Once a
  document's writers pass revisions, have every one of them pass it.
- Read the revision before the data you write, never after: a revision read after the data can be
  newer than the data it labels.

CI runs these cases against `mongodb/mongodb-atlas-local:8.0.30`, including a newer write that the
look misses. It does not run two writers at once.

## Tuning

- **`num_candidates`** (default 100) is how many neighbours the approximate search considers. It
  is raised to `k` when `k` is larger, and Atlas caps it at 10,000. Higher finds more of the true
  nearest chunks and takes longer. MongoDB suggests about 20 times the number of results. Atlas
  Vector Search returns at most 10,000 chunks, so a vector search with `k` above that raises
  `ValueError` rather than return 10,000 as if no more matched.
- **`exact=True`** compares the query with every chunk that passes the filter instead: exact
  results, at a higher cost on large collections.
- For a native `aretrieve`, pass `async_collection=` the same collection from an
  `AsyncMongoClient`; otherwise async calls run in a thread.

## How permissions are indexed

The backend uses the same design as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed)
and [Milvus](milvus.md#how-permissions-are-indexed): one array field holds a type-tagged term for
every string and integer in the chunk's metadata, so `1` and `"1"` stay apart and booleans, floats,
nested lists and objects never match. A filter becomes `$in` on that field, joined with `$and` and
`$or`. It is a BSON document, so a group name or user id is always a value and never query syntax.
An `In` with no values matches nothing without a call to MongoDB; `filter_to_mongo` and
`filter_to_search` raise `NothingMatches` for a filter that can match nothing, since a search
without a filter matches everything. Only one field needs indexing, whatever your metadata looks
like.

A vector-search index cuts a long string short when it indexes a filter field, while a filter names
the whole value. The limit is not documented for this field type: it measured 8,191 characters on
Atlas Local 8.0 and 8.3, and the `token` field type documents 8,181 (Java characters, so one outside
the Basic Multilingual Plane counts twice). A term longer than 8,181, field name and value together,
is therefore stored and searched as its SHA-256 digest. Its chunk is found like any other, and a
value that is only the first part of a longer one does not match it.
`permission_terms` writes the same form.

## Collections written by other tools

A document without `wardowl_terms` is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the field in your ingestion pipeline:

```python
from wardowl.backends.mongodb import permission_terms

document["wardowl_terms"] = permission_terms(document["metadata"])
```

## Keyword search and hybrid search

`mode="keyword"` searches the chunks' text with Atlas Search (`$search`, BM25), under the same
permission filter. Atlas Search keeps text in an index of its own, which `create_text_index`
creates: the text with the `lucene.standard` analyzer, which keeps every word, and the permission
terms, the write marker and `_id` as tokens. One collection then serves both halves of
[hybrid search](../hybrid.md):

```python
from wardowl import RankFusion
from wardowl.backends.mongodb import create_text_index

create_search_index(collection, dim=768)
create_text_index(collection)  # the "wardowl_text" index
vector = MongoDBBackend(collection, embed=embed, text_index="wardowl_text")
keyword = MongoDBBackend(collection, mode="keyword")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Atlas Search also indexes writes a moment after they happen. A backend given `text_index` waits for
that index as well, so write through the vector backend above and both searches see each change
when `add`, `set_metadata` or `delete` returns. The filter is written with Atlas Search's own
operators (`in` on the permission terms, `compound` for `And` and `Or`), so values stay data, and
the query's text is the `text` operator's query, separate from the filter. Scores are Atlas
Search's BM25, higher for better matches. A keyword backend without `embed` writes chunks with no
vector, which keyword search finds and vector search does not. CI runs the conformance kit against
Atlas Local in keyword mode and fused with vector search.

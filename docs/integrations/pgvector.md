# PostgreSQL + pgvector

```bash
pip install "wardowl[pgvector]"
```

Needs PostgreSQL with pgvector 0.8 or newer. The permission filter runs inside the SQL query, so
the database returns the nearest chunks the subject may read, however few of the table's rows that
is.

## Set up

```python
from psycopg_pool import ConnectionPool

from wardowl.backends.pgvector import PgVectorBackend, create_schema

pool = ConnectionPool("postgresql://localhost/app")
create_schema(pool, dim=768)
backend = PgVectorBackend(pool, embed=embed)  # embed: str -> list[float], your embedding model
```

`create_schema(pool, dim, table="wardowl_chunks", hnsw=True)` creates, if they do not exist:

- the `vector` extension;
- a table with `id text PRIMARY KEY`, `content text`, `metadata jsonb`, `embedding vector(dim)`
  and `revision bigint` (see [Revisions](#revisions));
- a GIN index on `metadata` (`jsonb_path_ops`), which serves the permission filter;
- an HNSW index on `embedding` with cosine distance (pass `hnsw=False` to create it yourself, for
  example after a bulk load).

To use an existing table, give it the first four columns and pass `table=`; it needs `revision`
only for writes given a revision. A table created by wardowl 2.1 or earlier has no `revision`
column, and `create_schema` does not change an existing table: add it with
`ALTER TABLE wardowl_chunks ADD COLUMN revision bigint` before passing a revision. Without it, such
a write fails with PostgreSQL's error that the column does not exist.

## Write and delete

```python
from wardowl import Document

backend.add([Document("salaries-1", "Salary bands for 2026 ...", {"allowed_groups": ["hr"], "doc_id": "salaries"})])
backend.add(documents, embeddings=vectors)  # if you computed the embeddings already
backend.delete(["salaries-1"])
```

`add` inserts or replaces by id, so re-indexing a document after its permissions change is the
same call.

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

- Each chunk is compared and written by one statement: `INSERT ... ON CONFLICT (id) DO UPDATE ...
  WHERE stored.revision IS NULL OR stored.revision <= EXCLUDED.revision` for `add`, and an `UPDATE`
  that keeps a newer row's metadata for `set_metadata`. PostgreSQL holds the row's lock while it
  compares, so a newer write still in progress is waited for and compared with what it commits.
- The whole call is one transaction. If any chunk is stored with a newer revision, the call raises
  `StaleRevision` naming those chunks, and writes nothing, chunks not stored before included.
  A chunk written at the same revision is replaced, so a retry succeeds.
- The chunks are written in the order of their ids, so two calls over the same chunks do not
  deadlock.
- A chunk that is not stored has no revision to compare with: `add` writes it, and `set_metadata`
  ignores it, as without a revision.
- A write without `revision` is not compared with anything, as before, and leaves the stored
  revision as it is. Once a document's writers pass revisions, have every one of them pass it.
- Read the revision before the data you write, never after: a revision read after the data can be
  newer than the data it labels.

## How the filter becomes SQL

Each value in an `In` becomes two JSON containment tests, one for a scalar field and one for a
list field:

```sql
metadata @> '{"allowed_groups": "hr"}'::jsonb OR metadata @> '{"allowed_groups": ["hr"]}'::jsonb
```

`And` and `Or` become `AND` and `OR`. Values are bound parameters, never interpolated. Containment
keeps strings, numbers and booleans apart, which matches the reference semantics, and the GIN index
serves it. jsonb compares numbers by value, though, so `{"level": 1.0}` contains `{"level": 1}`,
where the reference semantics match integers only. An integer value therefore also requires the
stored number, or an element of the stored list, to read as that integer:

```sql
EXISTS (SELECT 1 FROM jsonb_path_query(metadata -> 'level', 'lax $[*]') AS e(v) WHERE v::text = ANY('{1}'))
```

The text of a jsonb number keeps its scale (`1.0` reads `1.0`), so a float never matches. It does
not keep an exponent: `json.dumps` writes the float `1e16` as `1e+16`, which jsonb would store as
the integer `10000000000000000`. So `add` and `set_metadata` write every whole float with its
fraction (`10000000000000000.0`), in lists and nested objects too, and floats come back as floats.
This matters for large ids from a pandas integer column with a missing value, which holds them as
floats. Metadata keys must be strings: `add` and `set_metadata` refuse others with `ValueError`,
since JSON would turn the key `1` into `"1"`, which a filter on the field `"1"` would then match.

## Iterative index scans

An HNSW index normally returns a fixed number of nearest rows (`hnsw.ef_search`, 40 by default)
and the filter is applied to those. With a selective filter, few or none of them pass, and the
query returns fewer than k rows. pgvector 0.8 added iterative index scans, which keep scanning
until enough rows pass, or until a limit stops them. The backend turns them on for each query
(`SET LOCAL hnsw.iterative_scan`):

| `iterative_scan=` | Behaviour |
|---|---|
| `"relaxed_order"` (default) | Keeps scanning; results can be slightly out of order, so the backend re-sorts them by distance |
| `"strict_order"` | Keeps scanning in exact distance order; slower |
| `None` | Off, as before pgvector 0.8: an index scan returns at most `hnsw.ef_search` rows, with or without a filter |

Results are ordered by distance, then by id, so equal scores come back in a stable order. Scores
are cosine similarity, `1 - cosine distance`.

An iterative scan stops at whichever of two limits it reaches first:

| Setting | Default | What it limits |
|---|---|---|
| `hnsw.max_scan_tuples` | 20,000 | Graph elements the scan visits (approximate, and not applied to the initial scan) |
| `hnsw.scan_mem_multiplier` | 1 | The scan's memory, as a multiple of `work_mem` (4 MB by default in PostgreSQL) |

The scan works outwards from the query through the graph. When the user may read only a few chunks
and none of them is among the elements visited before a limit is reached, the query returns fewer than
k rows, or none, and pgvector gives no sign that it stopped early. In wardowl's acceptance run on
Kaggle, least-privileged users who could read several hundred chunks got no rows at all for
questions far from those chunks. `PgVectorBackend` does not declare `exact_search`, so wardowl
reports such a short or empty result as `complete=False`
([Concepts](../concepts.md#what-a-result-tells-you)), and can find out why and repair it
([Short results](#short-results)).

For users who may read little, raise the limits. The backend sets them for each search, with
`SET LOCAL` in the search's transaction, so other users of the connection pool keep their own:

```python
backend = PgVectorBackend(pool, embed, max_scan_tuples=100_000, scan_mem_multiplier=4, work_mem="64MB")
```

| Argument | Sets | Values |
|---|---|---|
| `max_scan_tuples` | `hnsw.max_scan_tuples` | 1 to 2,147,483,647 |
| `scan_mem_multiplier` | `hnsw.scan_mem_multiplier` | 1 to 1000 |
| `work_mem` | `work_mem`, which also sizes the query's sorts | kilobytes (64 or more), or text with a unit, such as `"64MB"` |

`None`, the default for each, leaves the server's setting, which you can also set where PostgreSQL
settings live, for example `ALTER DATABASE app SET hnsw.max_scan_tuples = 100000`. pgvector's README
suggests raising `hnsw.scan_mem_multiplier` when raising `hnsw.max_scan_tuples` does not improve
recall. A higher limit costs time only on the searches that need it, where the filter admits few
rows near the question. pgvector's other remedies for filters that admit few rows are a partial
index per filter value, for a few distinct values, and partitioning, for many
([pgvector: Filtering](https://github.com/pgvector/pgvector#filtering),
[Iterative index scans](https://github.com/pgvector/pgvector#iterative-index-scans)).

## Short results

A higher limit makes an empty result rarer; it cannot prove that a short one is complete. For that,
the backend offers what [`short_results`](../concepts.md#short-results) uses, in vector mode:

- `count(filter, limit)` counts the rows the filter matches, and stops at `limit`:
  `SELECT count(*) FROM (SELECT 1 FROM <table> WHERE <filter> LIMIT <limit>) AS matching`. The
  metadata index serves the filter, as it does in a search.
- `search(..., exact=True)` runs the same query with `SET LOCAL enable_indexscan = off`, so the
  HNSW index, which is what stops early, is not used: PostgreSQL reads every row the filter
  matches, through the metadata index or a scan of the table, and sorts them by distance. It
  returns the true nearest rows the subject may read, at the cost of computing the distance to
  each of them.

```python
retriever = PermissionedRetriever(backend, authorizer, short_results="exact", max_exact_rows=10_000)
result = retriever.retrieve("What is our acquisition plan?", subject)
result.shortfall, result.fallback  # ("missed", "exact"): the index stopped early; the exact search did not
```

`max_exact_rows` bounds the rows the filter matches, not the plan PostgreSQL chooses: if it
misjudges a selective filter, it may still read the whole table for the exact search. Check the
plan with `EXPLAIN` for your largest readable sets. In keyword mode, `count` returns `None`, since a
full-text search ranks only the rows that contain the query's words, and `supports_exact` is
False.

To compare `"exact"` with a deeper approximate search, give `short_results="widen"` a second backend
on the same table with higher limits:

```python
wider = PgVectorBackend(pool, embed, max_scan_tuples=1_000_000, scan_mem_multiplier=16, ef_search=400)
retriever = PermissionedRetriever(backend, authorizer, short_results="widen", wider=wider)
```

## Search depth

HNSW is approximate: how many of the true nearest neighbours a search finds depends on
`hnsw.ef_search`, the size of the candidate list it keeps (40 by default, 1 to 1000). Higher finds
more and takes longer. On a million chunks at the default, the index's top 10 held 0.895 of the
exact top 10 (see [Benchmarks](../benchmarks.md#at-a-million-chunks-under-load)).

```python
backend = PgVectorBackend(pool, embed, ef_search=100)
```

The backend sets it with `SET LOCAL` in each search's transaction, like the iterative scan, so
other users of the connection pool keep their own setting. `None` (the default) leaves the server's
value. Measure the recall and latency of a few values on your own data before choosing one.

## Keyword search

`mode="keyword"` searches the text with PostgreSQL full-text search instead of the vector, under
the same permission condition, for [hybrid search](../hybrid.md) with `RankFusion`:

```python
create_schema(pool, dim=768, text_search="english")
keyword = PgVectorBackend(pool, None, mode="keyword", text_search="english")
```

The query is parsed with `websearch_to_tsquery` (quoted phrases, `or`, `-word`) in the
`text_search` configuration, and results are ranked by `ts_rank_cd`. `create_schema(...,
text_search=...)` adds a GIN index on `to_tsvector('<configuration>', content)`; the backend writes
the configuration into the query as a literal, so PostgreSQL uses that index (CI checks the plan).
Use the same configuration name in both places. A keyword backend reads the table; write chunks
with a vector backend, since every row stores its embedding.

## Timeouts

Nothing bounds a search by default: a statement waiting for a lock, or a connection to a database
that stopped answering, holds the retrieval for as long as that lasts. In wardowl's fault
experiment, a database behind a connection that never answered held retrievals for the pool's
5-second timeout, or for longer than 10 seconds when the connection was already open. Bound each
layer:

| Where | Setting | What it bounds |
|---|---|---|
| The statement | `PgVectorBackend(statement_timeout=2.0)` (seconds) | Each search and count, on the server, time waiting for a lock included. Sent as `SET LOCAL statement_timeout` in the statement's transaction, like the scan settings, so other users of the pool keep their own. 0.001 to 2,147,483.647 seconds. |
| Connecting | `connect_timeout=5` in the connection string (libpq) | Opening a connection, per host address (2 seconds at least) |
| A network that stops answering | `tcp_user_timeout=5000` (milliseconds, Linux) and `keepalives_idle`, `keepalives_interval`, `keepalives_count` (libpq) | Data sent but never acknowledged, and an idle connection whose peer is gone |
| A free connection | `ConnectionPool(timeout=...)` (psycopg_pool, 30 seconds by default) | Waiting for the pool to hand out a connection (`PoolTimeout`) |

```python
pool = ConnectionPool(
    "postgresql://localhost/app?connect_timeout=5&tcp_user_timeout=5000&keepalives_idle=10"
    "&keepalives_interval=5&keepalives_count=3",
    timeout=5,
    check=ConnectionPool.check_connection,  # a connection that died while idle is not handed out
)
backend = PgVectorBackend(pool, embed, statement_timeout=2.0)
```

A statement that times out raises `SearchError` from the retriever, with the driver's
`QueryCanceled` as its cause, and the retrieval returns nothing. The server keeps the statement's
time, so `statement_timeout` cannot help when the statement never reaches it, or its answer never
comes back. `tcp_user_timeout` and keepalives catch a network that drops packets, but not a peer,
such as a proxy, that accepts and acknowledges everything and never answers: for that, bound the
whole retrieval in your application, for example with `asyncio.wait_for` around `aretrieve`.

## Async

Pass an `AsyncConnectionPool` as `async_pool=` and `aretrieve` uses a native async search.
Without one, async calls run `search` in a thread.

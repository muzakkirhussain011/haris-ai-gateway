---
description: How to upgrade wardowl from 2.1 to 3.0, and from needtoknow 1.x to wardowl 2.0. Covers each change that can break existing code, and what to change.
---

# Upgrading

A major version can change behaviour that code written for the one before relies on. This page
lists those changes, newest first, with what to change. The full list of changes is in the
[changelog](changelog.md).

## From 2.1 to 3.0

wardowl 3.0.0 is a major version because several fixes change documented behaviour: what a
failing vector store raises, what `complete` says about a short result, what `SpiceDBAuthorizer`
returns when its lookup finds nothing, when Redis answers are trusted, and which thread observers
run in under `aretrieve`. The steps below take them one at a time. 2.1.x keeps getting security
fixes for six months after 3.0.0; the
[security policy](https://github.com/muzakkirhussain011/wardowl/blob/main/SECURITY.md#supported-versions)
gives the date.

### 1. Install 3.0

```bash
pip install --upgrade "wardowl==3.0.*"   # with the same extras, e.g. "wardowl[pgvector,redis]==3.0.*"
```

The extras, the Python versions and the minimum versions of the store clients are the same as in
2.1. Nothing needs indexing again, except chunks with very long permission values on Redis or
MongoDB (step 8).

### 2. Catch `SearchError` where you caught the store's exceptions

In 2.1, a failure in the backend's search reached the caller of `retrieve` and `aretrieve` as the
store client's own exception (from psycopg, redis-py, the Qdrant client and so on), with its text,
which can name hosts. In 3.0 a failure in the backend's search or count raises
`wardowl.SearchError`, a `RuntimeError` with the fixed message "search failed" or "count failed",
and the store client's exception as its `__cause__`. Only `InvalidQuery`, which says the query
itself cannot be searched, is still raised as it is. Any other `ValueError` or `TypeError` from a
backend becomes a `SearchError` too, such as a field name the store cannot filter on, or a `k`
above what one query returns (10,000 for Elasticsearch, OpenSearch, MongoDB and Pinecone, 1,000
for Cassandra), which reaches the store only when `max_candidates` is raised above that.

```python
# 2.1
try:
    result = retriever.retrieve(question, subject)
except psycopg.OperationalError:
    return answer_without_documents()

# 3.0
from wardowl import SearchError

try:
    result = retriever.retrieve(question, subject)
except SearchError as exc:
    if isinstance(exc.__cause__, psycopg.OperationalError):
        return answer_without_documents()
    raise
```

Monitoring changes with it. `RetrievalEvent.error` and the OpenTelemetry attribute `error.type`,
on spans and metrics, say `SearchError`; `RetrievalEvent.cause` and the span attribute
`wardowl.error.cause` name the store client's exception. An alert that matched the client's
exception in `error.type` should match `SearchError`. The HTTP service still answers such a
failure with 500, and the MCP tool still ends with an error that carries none of the store's text.

### 3. Expect `complete=False` for short results

In 2.1, a pre-filtered search that returned fewer hits than were asked for, none included, gave
`complete=True` when the re-check removed nothing, and so did a post-filtered search inside an
authorizer's scope whose candidates ran out. In 3.0 both give `complete=False` unless the result
proves that nothing else matches: the backend declares `exact_search` (`InMemoryBackend`,
`FaissBackend` with a flat index or an ID map around one, and `QdrantBackend` in vector mode whose
`search_params` set `exact=True` without `indexed_only`), or, for a pre-filtered result, a count
under `short_results` shows no more matching chunks than came back. A short pre-filtered result
also says why in `shortfall`: `"unknown"` under the default `short_results="report"`. An
approximate index can stop early under a selective filter, and pgvector did, for users who could
read hundreds of chunks, in an acceptance run on Kaggle; 2.1 reported those empty results as
complete. Under the default `short_results="report"`, the hits returned are the same as in 2.1;
only `complete` differs.

So users who may read fewer than `k` chunks now get `complete=False` on approximate stores, in the
result, in `RetrievalEvent`, in the HTTP service's `complete` field, and in `OpenTelemetryMetrics`
as `wardowl.retrievals` with `wardowl.outcome = incomplete`. If you alert on those, or treat
`complete=False` as a failure, expect more of them. To tell a user who may read little from an
index that stopped early, use `short_results` on a backend that can count, `PgVectorBackend` or
`QdrantBackend` in vector mode:

```python
retriever = PermissionedRetriever(
    PgVectorBackend(pool, embed),
    authorizer,
    short_results="exact",  # count a short result; after a miss, search again exactly
    max_exact_rows=10_000,  # the default: above it, the first hits stay (fallback="over_limit")
)
result = retriever.retrieve(question, subject, k=5)
if not result.complete:
    log.info("short result: shortfall=%s fallback=%s", result.shortfall, result.fallback)
```

`short_results="count"` only counts: a count no larger than the hits makes the result complete
(`shortfall="exhausted"`), a larger one says the index missed chunks (`shortfall="missed"`).
`"exact"` also searches again, exactly, after a miss, and `"widen"` searches again with `wider`,
the same store with larger search settings. [Short results](concepts.md#short-results) describes
what each costs; `wardowl.short_results` counts short results by `wardowl.shortfall` and
`wardowl.fallback`.

### 4. SpiceDB: an empty lookup gives no filter

In 2.1, `SpiceDBAuthorizer.filter_for` turned a LookupResources answer with no document into a
filter that matches nothing, and the retrieval returned nothing. LookupResources answers as a
stream with no end marker, so a stream cut off before its first line looked the same. In 3.0 it
returns no filter for such an answer. With `strategy="auto"`, the default, the retriever then
post-filters: it searches without a filter and CheckBulkPermissions decides each candidate, up to
`max_candidates` of them for a subject who reads nothing. With `strategy="prefilter"`, the
retrieval raises `ValueError` ("strategy='prefilter' needs an authorizer that returns a filter"),
as it already did for a lookup that reached `lookup_limit`. Code that calls `filter_for` itself
gets None where it got an `In` with no values.

```python
# Pre-filter when the lookup finds documents, post-filter when it finds none:
retriever = PermissionedRetriever(backend, authorizer)  # strategy="auto"

# Or take an empty lookup as "nothing is readable", as 2.1 did:
authorizer = SpiceDBAuthorizer(url, token=key, resource_field="doc_id", trust_empty_lookup=True)
```

With `trust_empty_lookup=True`, a cut-off stream again returns nothing, without an error; it never
returns a chunk the subject may not read.

### 5. Redis: RESP2 answers need the server's timeout policy

In 2.1, `RedisBackend` took a RESP2 answer as it came. Under the Query Engine's default
`search-on-timeout` policy, `return`, a search that runs out of time answers with the results found
so far; a RESP3 answer then carries a warning, on which the backend already raised, and a RESP2
answer carries nothing. In 3.0, the first time a search is answered in RESP2, the backend reads the
policy (`CONFIG GET search-on-timeout`, or `FT.CONFIG GET ON_TIMEOUT` on Redis Stack) and raises
`RuntimeError`, which the retriever raises as `SearchError`, unless it is `fail`. A server that
refuses both commands counts as not `fail`. Until it reads `fail`, it asks again on each RESP2
answer.

This affects a client that speaks RESP2, such as one created with `protocol=2`. Any one of these
keeps searches working:

```python
client = redis.Redis.from_url(url, protocol=3)  # RESP3 answers carry the timeout warning

# or set search-on-timeout fail in the server's configuration
# (FT.CONFIG SET ON_TIMEOUT FAIL on Redis Stack), so a timed-out search raises there

# or accept RESP2 answers without asking, for a server whose policy the client may not read:
backend = RedisBackend(client, "chunks", embed, check_timeout_policy=False)
```

`check_timeout_policy=False` takes RESP2 answers as 2.1 did, partial ones included. An answer that
is neither a RESP2 list nor a RESP3 map, from a client that parses it into an object of its own,
now raises `TypeError` (a `SearchError` from the retriever) whatever `check_timeout_policy` says:
use a client whose `execute_command` returns the answer as a list or a mapping.

### 6. Observers run in a worker thread under `aretrieve`

In 2.1, `aretrieve` called its observers on the event loop. In 3.0 it calls them, required ones
first, in a worker thread of the event loop's default executor (the pool that
`create_app(max_workers=...)` sets, when it is set), and waits for them before it returns or
raises. Several retrievals can call one observer from several threads at once. Context variables
are copied into the thread, but no event loop runs there, so an observer that calls
`asyncio.get_running_loop()` raises, which is logged and counted in `observer_failures`; a required
observer that raises makes a retrieval that found its hits raise `AuditError`. `retrieve` still
calls observers in the caller's thread.

Make each observer thread-safe, and hand anything for the event loop to it with
`loop.call_soon_threadsafe`:

```python
import asyncio
import threading


class ErrorCounter:
    """Counts failed retrievals; a lock, since aretrieve calls it from several threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.errors = 0

    def __call__(self, event) -> None:
        if event.error:
            with self.lock:
                self.errors += 1


class ToQueue:
    """Puts each event on an asyncio.Queue that the event loop reads."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        self.loop = asyncio.get_running_loop()  # create it while the loop runs

    def __call__(self, event) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
```

`JsonLinesAudit`, `OpenTelemetryObserver` and `OpenTelemetryMetrics` need no change.

### 7. Backends of your own, and subclasses of `InMemoryBackend`

The retriever and the conformance kit now read optional backend members by name: `exact_search`,
`supports_exact`, `count` and `acount`.

- `InMemoryBackend` declares `exact_search = True`, so the retriever reports its short results as
  complete, and `check_backend` checks it as exact. A subclass that drops hits on purpose, to stand
  in for an approximate index in tests, must say so:

  ```python
  class LossyBackend(InMemoryBackend):
      exact_search = False
  ```

- `check_backend` checks a backend that declares `exact_search = True` as exact, whatever `exact=`
  says: it must return every matching document, up to `k`, or stop declaring it.
- Where a backend has `count`, or declares `supports_exact = True`, the kit checks that `count`
  counts what the filter matches and that `search(..., exact=True)` returns it. A backend with a
  member of one of these names that means something else fails the kit: rename the member.
- An observer may define `observer_failed(observer, event, error)`, which the retriever calls for
  each observer that raised. Rename a method of that name that means something else.

### 8. Other changes to check

| If you use | What changed | What to do |
|---|---|---|
| `except ValueError` before `except AuthorizationError`, with `OpenFGAAuthorizer` or `SpiceDBAuthorizer` | A subject id or group the service cannot take raises `UnsupportedSubject`, which is an `AuthorizationError` and a `ValueError` | Catch `AuthorizationError` first |
| `RedisBackend` or `MongoDBBackend` with very long permission values | A permission term longer than the store indexes whole (over 2,048 bytes of field name and value in Redis, over 8,181 UTF-16 code units in MongoDB) is stored and searched as its SHA-256 digest; chunks 2.1 wrote with such values are found once written again, and were not found in 2.1 either | Write those chunks again with `add` or `set_metadata`; for documents written by other tools, compute `wardowl_terms` with 3.0's `permission_terms` |
| A fixed schema for `RetrievalEvent` or for `JsonLinesAudit` lines | Events and lines gain `shortfall`, `fallback`, `shortfall_ms`, `sampled_recall` and `recall_sample` | Allow the new fields |
| `CachingAuthorizer` over `SpiceDBAuthorizer` without `fullyConsistent`, coming from 2.1.0 | After `invalidate()`, answers are passed on uncached for `source_staleness` seconds, 11 by default for that authorizer, so more requests reach SpiceDB then; replicas still on 2.1.0 that share the cache do not hold back (2.1.1 has the same change) | Repeat any `invalidate()` made during a rolling upgrade once every replica runs 3.0: until then, an answer a 2.1.0 replica cached during the hold is used by 3.0 replicas too, for up to `ttl`. Upgrading from 2.1.1 avoids it |
| `ChromaBackend` with subjects in very many groups, coming from 2.1.0 | A filter beyond Chroma's bounds is applied by the backend to `fallback_candidates` chunks, so it can return fewer than `k` hits; `filter_to_where` returns nested `$and` and `$or` and raises `FilterTooLarge` beyond the bounds (2.1.1 has the same change) | Raise `fallback_candidates` if such subjects need more; catch `FilterTooLarge` where you call `filter_to_where` yourself |
| `revision=` on a pgvector table created by 2.1 or earlier | Writes given a `revision` need a `revision bigint` column, which `create_schema` adds only to new tables | `ALTER TABLE <table> ADD COLUMN revision bigint` before passing a revision |

The other additions, `short_results`, `recall_sample_rate`, `required_observers`, `revision`,
`retrieval_timeout`, `max_workers`, `total_timeout` and `statement_timeout`, change nothing until
you set them. The MCP tool now also declares itself not destructive and idempotent.

## From needtoknow 1.x to wardowl 2.0

wardowl 2.0.0 is the first release under the new name. It is a major version because several
security fixes change behaviour that 1.x code may rely on. The full list is in the
[changelog](changelog.md). This section lists what to change, in order.

### 1. Install the new package

```bash
pip uninstall needtoknow
pip install wardowl            # with the same extras, e.g. "wardowl[pgvector,openfga]"
```

Then rename the imports: `needtoknow` becomes `wardowl`, `NeedToKnowRetriever` becomes
`WardowlRetriever`, and environment variables named `NEEDTOKNOW_*` become `WARDOWL_*`. The default
cache namespace and the metric names change in the same way.

### 2. Index your chunks again

The fields the backends store are renamed: `needtoknow_terms`, `needtoknow_id` and
`needtoknow_metadata` become `wardowl_terms`, `wardowl_id` and `wardowl_metadata`. Qdrant and
Weaviate also derive every point id from the chunk id's SHA-256 now, a UUID chunk id included.
Write your chunks into a new collection, index or table with wardowl, switch the application to it,
then drop the old one.

On some stores, an object left in the 1.x layout misbehaved without an error: Qdrant reported point
UUIDs as chunk ids, and `set_metadata` and `delete` by a chunk id that is not a UUID reached no
point, so a revocation changed nothing; Elasticsearch and OpenSearch found nothing under a
pre-filter; and Chroma denied every chunk. From wardowl 3.1 these raise `ValueError` instead,
pointing here, and write nothing:

| Store | What is checked | When |
|---|---|---|
| Qdrant | The collection's first 16 points: one holding `needtoknow_id` and no `wardowl_id` | `create_collection` on an existing collection, and a backend's first `add`, `set_metadata` or `delete` |
| Elasticsearch, OpenSearch | The index mapping: `needtoknow_terms` mapped and `wardowl_terms` not | `create_index` on an existing index |
| Chroma | Each record a search returns: `needtoknow_metadata` and no `wardowl_metadata` | Every search (raised from `retrieve` as `SearchError`) |

Other stores, and objects these checks do not read (Qdrant points after the first 16, an
Elasticsearch index used without calling `create_index`), are not checked.

### 3. Check the changes that can affect your code

| If you use | What changed | What to do |
|---|---|---|
| `CachingAuthorizer(shared=...)` | `namespace` is required | Give each kind of authorizer its own namespace |
| A shared cache during the upgrade | Cache keys changed | Call `invalidate()` once every replica runs 2.0, or allow for `ttl` |
| `SpanRedactor` | A chunk without its spans field is hidden whole (`required=True`) | Keep the spans field on every chunk; `set_metadata` replaces metadata, so include it |
| The LangChain, LlamaIndex or Haystack retrievers | They return no chunk metadata unless named | Pass `metadata_fields=[...]` with the fields your prompts need |
| LlamaIndex's `PermissionNodePostprocessor` | Kept nodes are copies with no metadata unless named, in their relationships too | Pass `metadata_fields=[...]` with the fields your prompts need |
| Haystack with an agent (`ComponentTool`) | A model can no longer set `subject`: the tool does not offer it, and a value sent anyway is refused | Give the tool the subject from the agent's state with `inputs_from_state={"subject": "subject"}`, or use `BoundWardowlRetriever` with a `subject_provider` |
| Haystack's `WardowlRetriever` in a pipeline | `subject` accepts a `Subject` or a function returning one, and is typed to accept any value | Name the sockets in `pipeline.connect`, such as `"retriever.query_embedding"`; a connection by component name alone is ambiguous |
| The conformance kit (`check_backend`) | `asearch` must refuse, with `ValueError`, every filter `search` refuses | Make the async path translate filters the way the sync path does |
| `Subject` | Attributes are read-only copies; id and groups must be strings | Build a new `Subject` instead of changing one |
| The public filter translators (`filter_to_*`) | A filter that matches nothing raises `NothingMatches` instead of returning `None` or `{}` | Catch `wardowl.backends.NothingMatches` and return no results |
| Chroma's `NEVER` constant | Removed | Catch `NothingMatches` instead |
| A large `k` | `k` above `max_candidates`, or above what the store returns in one query, raises `ValueError` | Lower `k`, or raise `max_candidates` |
| `In(field, "12")` | Strings, bytes and mappings are refused as the list of values | Pass a list: `In(field, ["12"])` |
| `PermissionedRetriever(audit_key=...)` | Keys shorter than 16 bytes are refused, and a key that is not bytes raises `TypeError` | Use a key of 16 bytes or more, such as `secrets.token_bytes(32)` |
| `CasbinAuthorizer(group_subjects=True)` | Groups are sent as `group:` + the group | Name groups with the prefix in your policies |
| `GoogleDriveACL` | Grants with an expiry are ignored by default; a deleted or unshared file gives `Access()` | Pass `expiring_grants=True` to count them until they expire, and sync again before `access_expires_at` |
| The HTTP authorizers | `ssl_context` or an `Authorization` header over `http://` raises `ValueError`; `timeout` bounds the whole call except the DNS lookup | Use `https://`, or `allow_insecure=True` on a network you trust; review your timeouts |
| `OpenFGAAuthorizer` | Subject ids and groups with `:`, `#`, `*` or whitespace raise `AuthorizationError` | Map such ids before they reach the authorizer |
| `PineconeBackend` | Writes wait up to 30 seconds for searches to see them | Pass `wait=None` for the old behaviour |
| `PgVectorBackend` | `In("level", [1])` no longer matches a stored `1.0`; metadata keys must be strings | Store integers as integers |
| `wardowl.server` | Bodies over 256 KiB answer 413; vectors hold at most 4,096 numbers; `NaN` answers 422 | Raise `max_body_bytes` or `max_vector_length` if you need more |
| `OIDCAuthenticator` | Tokens must name their key (`kid`); the key's own algorithm is used | Check that your identity provider sets `kid` |
| The `server` or `langchain` extra | Needs PyJWT 2.14 or later, langchain-core 1.3.3 or later | Upgrade those packages |

### 4. Run the conformance kit

If you wrote your own backend or authorizer, run `wardowl.testing.check_backend` or
`check_authorizer` against it. The kit now checks the async paths and data that stores tend to
confuse, such as `"1"` next to `1`, so it can find problems the 1.x kit missed.

### needtoknow on PyPI

needtoknow is archived on PyPI. Its last release, 2.0.0, has no code: it installs wardowl, and
`import needtoknow` raises an error that points here. A requirement below 2.0 still installs
needtoknow 1.6.0, which gets no security fixes; the fixes are in wardowl 2.0.0.

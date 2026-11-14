# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). From 1.0 on, the public API is stable.

## [Unreleased]

### Documentation

- What a rise in `violations` can mean. Concepts, the quickstart and the README said it means the
  store ignored the filter, and the runbook looked only for a store bug; it also rises when another
  tool leaves the permission terms stale on the stores that keep them, when MongoDB's index has not
  caught up with a write, for Chroma records without wardowl's JSON copy (a collection kept from
  needtoknow 1.x among them), when an authorizer's filter selects more than its `check` allows, and
  when a permission changes between the filter and the check. The runbook lists each cause with how
  to tell it and what to do, and shows how to find the rejected chunks; the production page's
  failure table and its identity checklist (ids and groups are compared exactly, never lowercased)
  are updated too. The security model and the
  Milvus, Redis and LanceDB pages say that terms are recomputed in the same write as the metadata,
  and what stale terms do; the extending guide says that `check_backend` cannot see other writers,
  and that a backend should return at most `k` hits. The Google Drive page shows how to enforce
  `access_expires_at` inside wardowl, with its cost, says that no backend lists chunks by
  metadata, that `with_access` needs the authorizer's field names, how tenants and case work, and
  that OpenFGA refuses the default `domain:` groups (use `domain_prefix="domain/"`) and SpiceDB
  refuses email ids; the OpenFGA page and ADR 0006 say the same. The OPA, Cerbos, Cedar and Casbin
  pages say how to keep tenants apart, Combining that a tenant from `OIDCAuthenticator` is a
  string, the MCP page which group names permission services refuse, and the pgvector and
  Cassandra pages what very large filters cost.

## [3.0.0] - 2026-11-14

A major release, because several fixes change documented behaviour that 2.1 code may rely on: a
failure in the vector store raises `SearchError` instead of the store client's own exception,
`complete` is False for a short result from an approximate index, `SpiceDBAuthorizer` gives no
filter when its lookup finds nothing, `RedisBackend` refuses RESP2 answers unless the server fails
searches that run out of time, and `aretrieve` calls observers in a worker thread. It adds
`short_results`, which tells why a pre-filtered result came back short and can search again,
`recall_sample_rate`, `revision` on pgvector and MongoDB writes, `required_observers` for an audit
log that must record every retrieval, and time bounds for the HTTP service, the MCP tool and the
HTTP authorizers. The two security fixes, with the additions and the Chroma fix they need, were
released in 2.1.1 and 2.0.2 too.
[Upgrading](https://muzakkirhussain011.github.io/wardowl/upgrading.html) lists what to change.

### Changed

- A failure in the backend's search or count, the searches `short_results` runs again included,
  raises `SearchError` with a fixed message, "search failed" or "count failed", and the store
  client's exception as its `__cause__`. It reached the caller as the client's own exception, with
  its own text, which can name hosts (a Qdrant error carries the raw response), while agent
  frameworks show a tool's error to the model: the guarantee the retriever states for authorizer
  failures now holds for the store too. `InvalidQuery` from a backend is still raised as it is;
  any other `ValueError` or `TypeError` a backend raises is now a `SearchError` too, such as a `k`
  above what Elasticsearch, OpenSearch, MongoDB or Pinecone (10,000) or Cassandra (1,000) return in
  one query, which only a `max_candidates` above that limit lets through, or a field name the store
  cannot filter on. Code that caught a store client's exceptions, or such a `ValueError`, around
  `retrieve` should catch `SearchError` and read `__cause__`. `SearchError` is a `RuntimeError`, not
  an `AuthorizationError`. `RetrievalEvent.error`, and `error.type` in OpenTelemetry, now say
  `SearchError`; `RetrievalEvent.cause`, and the span attribute `wardowl.error.cause`, name the store
  client's exception. Alerts that matched the store client's exception in `error.type` should match
  `SearchError`. The paper's fault experiment X7 found it: 28,125 failures under store faults
  surfaced as psycopg, psycopg-pool, qdrant-client and JSON exceptions.
- `RetrievalResult.complete` is no longer True for a short or empty result from an approximate
  search. A pre-filtered result with fewer hits than were asked for, an empty one included, is
  complete only when the backend declares exact search, or when `short_results` (see Added) has
  the backend count no more chunks matching the filter than came back; post-filtering that runs
  out of candidates inside a scope is complete only when the backend declares exact search. The
  acceptance run on Kaggle found pgvector returning no rows at all to least-privileged users who
  could read several hundred chunks, because its iterative index scan stopped at
  `hnsw.max_scan_tuples` or at its memory limit, and wardowl reported those empty results as
  complete. That contradicted the field's documented meaning, "False when wardowl knows
  results may be missing"; this corrects it. Users who may read fewer than k chunks now get
  `complete=False` on approximate stores under the default `short_results="report"`, which cannot
  tell them apart from an index that stopped early. Nothing else changes: the same searches run,
  the same hits come back, and the safety guarantees are unchanged. Post-filtering without a filter
  still reads a short candidate list as the end of the store. Alerts on `complete=False`, or on
  `wardowl.outcome = incomplete`, see these results too; to tell them apart, pass
  `short_results="count"` on a backend that can count (pgvector and Qdrant in vector mode), which
  makes a result complete when the count shows the user may read no more, or `"exact"` or
  `"widen"` to search again after a miss.
- Backends can declare `exact_search = True` when `search` ranks every chunk that matches the
  filter, so a result with fewer than k hits proves nothing else matches. It is optional and read
  with `getattr`, not a required member of the `SearchBackend` protocol, so existing backends still
  satisfy it. `InMemoryBackend` declares it, and `FaissBackend` does when its index is flat (the
  default) or an ID map around a flat index. `RankFusion` does not, even over exact backends: it
  leaves out chunks whose metadata differs between backends after each has filled its list, so a
  short fused list proves nothing. `QdrantBackend` declares it when its `search_params` force exact
  search (see Added). No other built-in backend declares it, so their short results are reported
  with `complete=False`. A subclass of `InMemoryBackend` that drops hits on purpose, to stand in
  for an approximate index, must now set `exact_search = False`: otherwise the retriever reports
  its short results as complete, and `check_backend` checks it as exact.
- `check_backend` checks a backend that declares `exact_search` as exact, whatever `exact=` says,
  since the retriever takes its short results as proof that nothing else matches. A backend of your
  own that declares it must return every matching document up to k, or stop declaring it. Where a
  backend has `count` or declares `supports_exact` (see Added), the kit checks those too, so a
  backend with a member of either name that means something else fails the kit: rename it.
- `SpiceDBAuthorizer.filter_for` returns no filter when LookupResources finds no document, so the
  retriever post-filters and CheckBulkPermissions decides each candidate. The stream has no end
  marker, so one cut off before its first line by a proxy or a fault read as "nothing is readable",
  and retrieval returned nothing without an error: under X7's fault that emptied SpiceDB's answers,
  4,746 retrievals did. Subjects who truly read nothing now cost a post-filtered search, and under
  `strategy="prefilter"` they raise `ValueError`, as a truncated lookup already did; pass
  `trust_empty_lookup=True` to keep the earlier behaviour, or use `strategy="auto"`. Code that
  calls `filter_for` itself gets None for such a subject, where it got an `In` with no values.
  OpenFGA's ListObjects is unchanged: its answer is one JSON document, which a cut would break, and
  a deadline-cut list already gives no filter.
- `OpenFGAAuthorizer` and `SpiceDBAuthorizer` raise `UnsupportedSubject` (see Added) for a subject
  id or group they cannot send, where OpenFGA raised a plain `AuthorizationError` and SpiceDB's
  requests failed with one after its HTTP 400 (see Fixed). It is an `AuthorizationError`, so
  retrieval still fails closed and `except AuthorizationError` still catches it, but it is also a
  `ValueError`: an `except ValueError` placed before `except AuthorizationError` around `retrieve`
  now catches it. Catch `AuthorizationError` first.
- `RedisBackend` no longer takes a RESP2 answer on trust. Under the Query Engine's default
  `search-on-timeout` policy, `return`, a search that runs out of time answers with the results
  found so far; a RESP3 answer then carries a warning, on which the backend already raised, but a
  RESP2 answer carries nothing, and the retriever reported such a result as complete whenever it
  held k hits (M14 in the readiness review). The first time a search is answered in RESP2, the
  backend now reads the policy (`CONFIG GET search-on-timeout`, or `FT.CONFIG GET ON_TIMEOUT` on
  Redis Stack) and raises `RuntimeError`, which the retriever reports as `SearchError`, unless it
  is `fail`; a server that refuses both commands counts as not `fail`. It asks again on each RESP2
  answer until it reads `fail`. Clients that speak RESP3, as redis-py 8.1 does by default, and
  servers set to `fail`, as the Redis page has advised for production, see no change. Otherwise
  set `search-on-timeout fail`, connect with `protocol=3`, or pass `check_timeout_policy=False`.
  An FT.SEARCH answer that is neither a RESP2 list nor a RESP3 map, as from a client that parses it
  into its own object, raises a `TypeError` that names its type, whatever `check_timeout_policy`
  says: pass a client whose `execute_command` returns the answer as a list or a mapping.
- `aretrieve` calls the observers in a worker thread of the event loop's default executor, and waits
  for them before it returns or raises, as before. They ran on the event loop, so an audit sink that
  blocked on its stream (a full disk, a network mount, a collector that stopped reading) stopped
  every request the HTTP service or the MCP server was serving until the write returned. Now it
  delays only the request whose event it writes. The event is still built when the retrieval ends,
  so its timings do not include the wait for a thread, and a call cancelled while its event is on
  the way stops waiting, while the event is still delivered. Observers must be thread-safe, as they
  already had to be for `retrieve` called from several threads; `JsonLinesAudit` and the
  OpenTelemetry observers are. Context variables are copied into the thread, but no event loop
  runs there: an observer that hands events to the loop, to an `asyncio.Queue` say, must do it with
  `loop.call_soon_threadsafe`. `retrieve` still calls observers in the caller's thread. A
  retrieval closed without finishing (`GeneratorExit`) still calls them in place, since it can
  await nothing more. Required observers (see Added) run there too, first, and `observer_failures`
  is counted under a lock.
- The MCP server's search tool now sets all four MCP tool hints explicitly: besides read-only and
  closed world, not destructive and idempotent, so a host does not have to assume a default for any
  of them.

### Added

- `revision=` on `add` and `set_metadata` of `PgVectorBackend` and `MongoDBBackend`, and
  `wardowl.backends.StaleRevision`. Every backend keeps the write that lands last, so when an
  indexer and a permission sync write the same document, an indexer that read the document before a
  revocation and writes after the sync wrote it puts the revoked access back, and nothing corrects
  it until the document changes again (found in an internal review). With `revision`, the source
  document's revision read before its data, a chunk stored with a newer revision is not replaced and
  the call raises `StaleRevision`, naming those chunks. pgvector compares and writes each chunk in
  one statement under its row lock (`INSERT ... ON CONFLICT ... DO UPDATE ... WHERE`, and an
  `UPDATE` that keeps a newer row), in one transaction per call, so a refused call writes nothing.
  MongoDB stores the revision in `wardowl_revision`, looks for newer chunks before writing (and
  writes nothing when it finds one), then compares and writes each chunk in one atomic update; a
  newer write that lands after the look is kept, while the other chunks of the call are written. The
  comparison is per chunk, so a chunk that is not stored is written whatever its revision. It is
  optional: without `revision`, both backends write exactly as before. `create_schema` creates new
  tables with a nullable `revision bigint` column and leaves existing tables alone; add the column
  to a table created by 2.1 or earlier with `ALTER TABLE ... ADD COLUMN revision bigint` before
  passing a revision. CI tests both against real servers, including a newer pgvector write that
  commits while a stale one waits for its row. Chroma has no write that compares and writes in one
  step, so `ChromaBackend` takes no revision.
- `ChromaBackend(fallback_candidates=...)`, how many chunks a search reads when its filter is too
  large to send to Chroma whole (1,000 by default), and `wardowl.backends.chroma.FilterTooLarge`,
  the `ValueError` that `filter_to_where` raises for such a filter. See Fixed.
- `PermissionedRetriever(short_results=...)` decides what happens when a pre-filtered search on an
  approximate index returns fewer hits than were asked for. `"report"` (the default) returns them
  with `complete=False`, as `complete` now reports them (see Changed), and runs nothing more.
  `"count"` asks the backend how many
  chunks match the filter: no more than came back proves that the user may read no more, and the
  result is complete; more proves that the index missed some. `"exact"` also searches again,
  exactly, under the same filter, when the count shows a miss and is at most `max_exact_rows`
  (10,000 by default; `None` for no bound), so a large readable set never sets off a large scan.
  `"widen"` searches again with `wider`, the same store with larger search settings, and keeps the
  best hits of both searches. The search run again is checked again with the authorizer, together
  with the first search's hits it did not return, so a backend that ignored the filter either time
  is counted in `violations`; an error in the count or the search fails the retrieval. A backend
  that cannot count or search exactly keeps the first hits. The acceptance run on Kaggle found
  pgvector returning nothing to users who could read about 600 chunks; with `"exact"`, such a
  result is counted, and searched again exactly over those few hundred chunks.
- `RetrievalResult.shortfall` says why a pre-filtered search came back short: `"exhausted"` (the
  user may read no more), `"missed"` (the index missed chunks the user may read) or `"unknown"`
  (approximate, and nothing was counted). `RetrievalResult.fallback` says what `short_results` did:
  `"exact"`, `"widened"`, `"over_limit"` (above `max_exact_rows`) or `"unsupported"` (the backend
  cannot count, or cannot search exactly). Both are None otherwise. `RetrievalEvent` carries both,
  and `shortfall_ms`, the time spent on a short result; `OpenTelemetryObserver` records them as
  span attributes, and `OpenTelemetryMetrics` counts short results in `wardowl.short_results`.
- `PermissionedRetriever(recall_sample_rate=...)` (0 by default): for that share of pre-filtered
  retrievals on a backend that can search exactly, the exact search runs too, and
  `RetrievalResult.sampled_recall` reports how much of its top k came back, which `complete` cannot
  see. The exact hits are never returned. `OpenTelemetryMetrics` records it in the
  `wardowl.sampled_recall` histogram. The exact search is bounded by `max_exact_rows`, as
  `short_results="exact"` is: the backend counts the chunks the filter matches first, and above the
  bound, or on a backend that cannot count, the exact search does not run. A sample never fails
  the retrieval: if its count or exact search raises (a store timeout on a broad readable set, for
  one), the error is logged on the `wardowl` logger and the retrieval returns its hits.
  `RetrievalResult.recall_sample` and `RetrievalEvent.recall_sample` say what happened to the
  sample: `"measured"`, `"over_limit"`, `"unsupported"` or `"failed"`. `OpenTelemetryObserver`
  records it as `wardowl.recall_sample`, and `OpenTelemetryMetrics` counts samples by it in
  `wardowl.recall_samples`. The readiness review found that a failed or timed-out sample raised
  `SearchError` and discarded the permitted hits the retrieval had already found (M16).
- Backends can offer `count(filter, limit)` (and `acount`), and declare `supports_exact = True`
  when `search` and `asearch` accept `exact=True`. Both are optional and read with `getattr`; the
  retriever passes `exact` only to a backend that declares it, so existing backends work unchanged.
  `check_backend` checks both where they are there: an exact search must return what the filter
  matches, and a count must count it.
- `PgVectorBackend(max_scan_tuples=..., scan_mem_multiplier=..., work_mem=...)` set pgvector's
  iterative scan limits and `work_mem` with `SET LOCAL` in each search's transaction, as
  `ef_search` and `iterative_scan` are. In vector mode, `count` counts the rows a filter matches,
  stopping at the limit, and `search(..., exact=True)` searches with `SET LOCAL enable_indexscan =
  off`, so every row the filter matches is ranked.
- `QdrantBackend.exact_search` is True for a vector backend whose `search_params` set `exact=True`
  (without `indexed_only`). In vector mode, `count` asks Qdrant for an exact count, and
  `search(..., exact=True)` searches with a copy of `search_params` that sets `exact=True` and
  `indexed_only=False`. ACORN is set through `search_params`, as before.
- `total_timeout=...` on `KetoAuthorizer`, `SpiceDBAuthorizer`, `OpenFGAAuthorizer` and
  `CerbosAuthorizer`, in seconds: one deadline for all the requests of one `check` or
  `filter_for`. These authorizers send a large check as several requests, one after another, and
  `timeout` bounds each request alone, so a check could take its number of requests times
  `timeout`: for Keto with `group_subjects=True`, 1,000 candidates for a subject in 20 groups is
  2,100 requests. Once `total_timeout` runs out, the request in flight is abandoned, nothing more
  is sent, and the call raises `AuthorizationError` caused by a `TimeoutError`. A request it cut
  short counts as neither a success nor a failure for a `CircuitBreaker`, which gains `release()`
  for that. None, the default, sets no bound, as before. Each authorizer's page now gives its
  number of requests per check.
- `wardowl.UnsupportedSubject`, raised by an authorizer when its permission service cannot represent
  the subject's id or one of its groups, before anything is sent. It is an `AuthorizationError`, so
  code that catches that and retrieval behave as before (retrieval fails closed), and a
  `ValueError`, since the subject is at fault rather than the service.
- `CachingAuthorizer(source_staleness=...)`: how many seconds old the data behind the wrapped
  authorizer's answers can be. By default it is the authorizer's `max_staleness`, an optional
  attribute an authorizer declares when it can answer from stale data, or 0 when it declares none.
  `SpiceDBAuthorizer.max_staleness` follows its consistency (11 seconds, or 0 with
  `fullyConsistent`; `max_staleness=` sets it), `AllOf` and `AnyOf` declare the largest of their
  parts', and `CachingAuthorizer.max_staleness` is its `ttl` plus `source_staleness`, for a cache
  that wraps another. See Security for what the cache does with it.
- `wardowl.SearchError`, raised when the backend's search or count fails (see Changed).
- `PgVectorBackend(statement_timeout=...)`, in seconds: PostgreSQL cancels a search or count that
  runs longer, time waiting for a lock included, and the retrieval raises `SearchError`. It is set
  with `SET LOCAL statement_timeout` in the statement's transaction, like the scan settings; `count`
  now runs in a transaction of its own for it. None, the default, sets nothing.
- `SpiceDBAuthorizer(trust_empty_lookup=True)` takes a LookupResources answer with no document as
  "nothing is readable", as before; see Changed.
- `PermissionedRetriever(required_observers=[...])`, for an audit log that a control depends on.
  A required observer receives each event before `observers`, and when it raises after a retrieval
  that found its hits, the retrieval raises `wardowl.AuditError` and returns nothing; the other
  observers then receive an event with `error="AuditError"` and no chunk ids. `AuditError` is a
  `RuntimeError` with a fixed message and the observer's exception as its `__cause__`. A retrieval
  that failed for another reason still raises its own error. Without `required_observers` nothing
  changes: delivery to `observers` stays best-effort.
- `PermissionedRetriever.observer_failures` counts the times an observer has raised, and each raise
  is still logged on the `wardowl` logger. An observer that defines
  `observer_failed(observer, event, error)` hears of every observer that raised; with it,
  `OpenTelemetryMetrics` counts them in `wardowl.observer_failures`, by observer and `error.type`.
  The readiness review found that a `JsonLinesAudit` on a full disk left only a log line per
  retrieval, with no metric to alert on and no way to stop returning chunks that were not
  recorded (M17).
- `RedisBackend(check_timeout_policy=False)` accepts RESP2 answers without reading the server's
  timeout policy, as before; see Changed.
- `create_app(retrieval_timeout=...)` and `add_search_tool(retrieval_timeout=...)`, in seconds,
  bound each retrieval of the HTTP service and the MCP tool. A retrieval that takes longer is
  cancelled, answers 503 or ends with a tool error, and returns nothing; the `wardowl` logger
  records the timeout, and the retrieval's event records `CancelledError`. Neither waited for a
  retrieval's end before: a store or permission service that never answered held the request
  indefinitely, while `/healthz` kept answering. A synchronous call already running in a worker
  thread cannot be stopped and runs on to its own client's timeout. None, the default, sets no
  bound, as before.
- `create_app(max_workers=...)` replaces the event loop's default executor, while the application's
  lifespan runs, with a thread pool of that many threads. Every synchronous store, authorizer,
  reranker and redactor call of `aretrieve`, `CachingAuthorizer`'s Redis calls and the observers run
  in that executor, whose default size, CPython's, bounded the service's concurrency without being
  set or documented. None, the default, leaves the executor alone, as before. The HTTP service page
  documents both settings, and that `/healthz` reports liveness only.

### Security

- `CachingAuthorizer.invalidate()` no longer lets a permission service that is itself behind refill
  the cache with a decision from before the change. After `invalidate()`, the next retrieval asked
  the service, which could still answer from a stale snapshot (SpiceDB under `minimizeLatency`,
  Cerbos before reloading a policy), and the cache kept that answer for its whole `ttl`. In the
  paper's experiment X7, all 40 SpiceDB revocations with `invalidate()` took the full 30-second
  `ttl` (a median 30.04 s, against a stated 5.5 s), 39 of 40 on a second replica, and Cerbos's took
  as long. Now, for `source_staleness` seconds after `invalidate()`, answers for the subject (or
  for everyone) are passed on and cached nowhere: not in the process, and not in the shared cache,
  whose epoch carries the end of the hold so that every replica holds back too. The bound after
  `invalidate()` is the service's own staleness when `source_staleness` covers it, plus `local_ttl`
  on other replicas. With `source_staleness` 0, the default for every authorizer but
  `SpiceDBAuthorizer` without `fullyConsistent`, nothing changes. Replicas of earlier versions
  sharing the cache during an upgrade do not see the hold.
- `ChromaBackend` no longer sends Chroma a filter that can crash the application. With chromadb
  1.5.9's embedded client, the first search by a subject in 40,000 groups ended the Python process
  with a segmentation fault, without an exception or a message, in 5 of 5 processes (the filter-size
  experiment for the paper). Anyone whose filter grows that large, through groups that users can
  create and join themselves or an authorizer that lists that many values, could stop the
  application for every user with one search. The cause inside Chroma is not established, and the
  smallest filter that crashes was not measured (10,000 groups gave an error instead); a Chroma
  server on a single node runs the same code. wardowl now sends a `where` only within bounds well
  below anything Chroma refused or crashed on: 10,000 SQL parameters, an expression 250 deep, 32
  levels of `$and` and `$or`, and 500,000 characters. Larger filters are applied by the backend
  itself (see Fixed).

### Fixed

- `ChromaBackend` answers subjects in 1,000 groups or more. Chroma 1.5 writes each `$or` as one
  binary OR per clause, and SQLite refuses an expression more than 1,000 deep, so the flat `$or`
  wardowl sent made every search by such a subject fail with
  `Expression tree is too large (maximum depth 1000)`. The conformance campaign and the filter-size
  experiment for the paper both found it at 1,000 values. Each `$and` and `$or` now holds at most
  16 clauses, nested as a balanced tree, which Chroma keeps nested: about 50 levels deep for 1,000
  groups instead of 1,000. A filter beyond the bounds under Security (with `MetadataACLAuthorizer`,
  a subject in more than about 3,200 groups with short names) is no longer sent whole: the search
  sends the part of it every match must meet, such as a tenant, if that fits, or no `where`; reads
  `fallback_candidates` chunks (1,000, or `k` if more); and applies the filter to them itself. It
  never returns a chunk the filter excludes, but can return fewer than `k` hits though more chunks
  match, which the retriever reports as `complete=False`. `filter_to_where` returns the nested form
  and raises `FilterTooLarge` beyond the bounds. CI searches real Chroma for subjects in 1,001 and
  in 40,000 groups, and checks that Chroma reads nested trees exactly as the reference semantics.
- `OpenFGAAuthorizer` no longer sends a request OpenFGA refuses whole with HTTP 400, which surfaced
  as `AuthorizationError("OpenFGA batch-check returned HTTP 400")`. A subject in more than 100
  groups (OpenFGA takes at most 100 contextual tuples per check or list), a user longer than 512
  bytes, a group longer than 256 characters, or an id or group with a control character now raises
  `UnsupportedSubject` up front, saying which limit it broke; ids with `:`, `#`, `*` or whitespace
  raise it too, in place of a plain `AuthorizationError`. To check a subject in more groups, store
  its memberships in OpenFGA and pass `contextual_groups=False`. The paper's conformance campaign
  found it: 119 of its 140 OpenFGA runs failed with that error, at the rates these limits predict
  for its subjects (one in five is in 300 groups).
- `SpiceDBAuthorizer` refuses a subject id that is not a SpiceDB object id (1 to 1024 letters,
  digits and `/_|-=+`), and with `group_subjects=True` such a group name, with `UnsupportedSubject`
  before anything is sent. Such a request failed closed before as well, but only after SpiceDB's
  HTTP 400. The conformance campaign's users `zoë` and `7.0` made 117 and 124 of 140 SpiceDB runs
  fail that way.
- `RedisBackend` and `MongoDBBackend` find chunks whose metadata holds a long value. The Redis Query
  Engine indexes at most 4,096 bytes of a TAG value, and a MongoDB vector-search filter field at
  most 8,191 characters of a string; both cut a longer one short at indexing, while a filter names
  the whole value, so a pre-filtered search missed such chunks, and a value made of the first part
  of a longer one matched it (the retriever's re-check dropped those). A permission term longer than that, field
  name and value together (over 2,048 bytes in Redis, whose terms are hex-encoded, and over 8,181
  UTF-16 code units in MongoDB), is now stored and searched as its SHA-256 digest. Terms within the
  limits are unchanged, so nothing needs re-indexing except chunks with such long values, which were
  not found before either: write them again with `add` or `set_metadata` to make them findable. The
  conformance campaign found it: 26 Redis and 19 MongoDB runs with
  values of 3,001 to 40,000 characters missed matching chunks.

### Documentation

- Compliance, operations, production and the security model say that delivery to `observers` is
  best-effort, how to make an audit log required with `required_observers`, and what to alert on
  when events are lost or recall samples fail. The compliance page offered the event stream as the
  audit control without saying that a sink that cannot write loses events (M17). The Redis page
  describes the RESP2 check and `check_timeout_policy`.
- The writes for one source document must happen one after another. Operations has a section on
  it, with what `revision` does on pgvector and MongoDB and that the other backends have no such
  check; the Google Drive page shows how a late re-index undoes a revocation that the changes feed
  never reports again, and how to pass Drive's `version` as the revision (Google does not document
  whether a change of sharing raises it, and wardowl's tests do not run against Drive, so the page
  says to check). The threat model adds T18, and the security model, revocation, production
  checklist, runbook, extending guide and the pgvector, MongoDB and Chroma pages say the same.
- A 47-second film on the documentation's front page, linked from the README: what wardowl does,
  shown with an AI assistant that many companies share.
- The README opens with the problem, the runnable quickstart (which a new test runs, checking that
  it prints what its comments say) and two results from the FiQA benchmark, linked to the results
  file. The package metadata, shown on PyPI from this release, adds keywords for more stores
  and permission systems and the `Framework :: AsyncIO` and `Framework :: FastAPI` classifiers.
- Corrected what the documentation said about filtered approximate search. Concepts said that
  pgvector's iterative index scans keep a selective filter from cutting results short; they stop
  at `hnsw.max_scan_tuples` (20,000 by default) or at `work_mem` × `hnsw.scan_mem_multiplier` of
  memory (4 MB by default), so a selective filter can still return fewer than k rows, or none. The
  pgvector page names both limits and how to raise them. The Qdrant page explains why filters on
  common values, such as a tenant shared by most points or a shared corpus readable by everyone,
  can lose recall in Qdrant's filtered HNSW search, and what helps: ACORN with `max_selectivity`,
  per-tenant graphs (`is_tenant` with `m=0` and `payload_m`), and exact search. Concepts, the
  security model, the threat model, operations, production, hybrid search and the store pages
  describe `complete` as it now behaves.
- Concepts has a section on short results: what `short_results` and `recall_sample_rate` do, what
  they cost, and how the guarantees hold when a search runs again. It credits OpenSearch, which
  since 3.5 falls back to an exact search inside the engine when an approximate filtered k-NN
  search returns fewer than k results. The pgvector page describes the scan-limit arguments, the
  count and the exact search; the Qdrant page describes `exact_search`, the count, the exact
  search and ACORN as a wider search. The extending guide and operations describe the new backend
  members, event fields, span attributes and metrics.
- Corrected SpiceDB's revocation bound. The adapter and its page said a revoked permission keeps
  granting access under `minimizeLatency` for up to the quantization interval, 5 seconds by
  default. SpiceDB documents the interval plus 10% of it (5.5 s), and X7 measured up to 8.7 s, with
  revoked documents reappearing after they had gone in 28 polls. The SpiceDB page, Revocation and
  consistency, operations, the security model and the threat model give the bound with and without
  `invalidate()`, and the acceptance scenario waits for the declared `max_staleness`.
- The pgvector page has a section on timeouts: `statement_timeout`, and libpq's `connect_timeout`,
  `tcp_user_timeout` and keepalives, and the pool's `timeout`, with what each bounds and what none
  of them does.
- The OpenFGA page says to raise `OPENFGA_REQUEST_TIMEOUT` together with the ListObjects deadline,
  since OpenFGA refuses to start when the deadline is the longer of the two. The Kaggle acceptance
  run did exactly that: it set a 20-second deadline and left the request timeout at its default, so
  OpenFGA never started and its configuration was reported as a start-up timeout. The run now sets
  the request timeout as well.
- Corrected how the README, the FAQ and the site's description for search engines summed up
  wardowl. They said it applies every listed permission system inside the vector store's search.
  The filter goes inside the search only when the permission system can express one (OpenFGA,
  SpiceDB, Cerbos, access lists, and OPA when its policy returns one); Cedar, Casbin and Ory Keto
  check each candidate after the search, as the integrations tables already said.
- The published benchmark results are now those of re-executions on 2026-11-07 and 2026-11-08,
  each of the same notebook at the same commit as the run it replaces. The results files, the
  Benchmarks page, the README and the pages that quote them give the new numbers; no wardowl
  strategy leaked in any of them. Where a statement no longer held, it now says what the
  re-execution measured: OpenSearch's first throughput setting ran at full speed (it was under 1
  request a second in two of three runs before), and Elasticsearch's quantized default index took
  more disk than `int8_hnsw` and `hnsw` rather than less. The Benchmarks page also no longer links a
  LanceDB tuning run that does not exist, or lists Redis as not measured at a million chunks.

## [2.1.1] - 2026-11-13

A security release from the 2.1 branch. It carries two fixes listed above under [3.0.0] and nothing else: `ChromaBackend` no longer sends Chroma a filter that can crash the process ([GHSA-pxx6-228g-4fw5](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-pxx6-228g-4fw5)), and `CachingAuthorizer.invalidate()` no longer lets a permission service that is itself behind refill the cache ([GHSA-3f2r-rrm4-mhwj](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-3f2r-rrm4-mhwj)), with `SpiceDBAuthorizer.max_staleness` and `CachingAuthorizer(source_staleness=...)`, which that fix needs. `RetrievalResult.complete` keeps its 2.1 meaning there. The full notes are on the [release page](https://github.com/muzakkirhussain011/wardowl/releases/tag/v2.1.1).

## [2.0.2] - 2026-11-13

The same two security fixes for the 2.0 branch, without 2.1.0's batched Chroma writes. The 2.0 line does not carry two security fixes of 2.1.0 that change behaviour (`SpiceDBAuthorizer`'s `group_subjects` default, and the text of the exception behind an `AuthorizationError` in its message); its notes say how to get them. The full notes are on the [release page](https://github.com/muzakkirhussain011/wardowl/releases/tag/v2.0.2).

## [2.1.0] - 2026-10-24

Fixes found by real-world acceptance scenarios, run against real servers, a real identity provider
and real models: a SpiceDB setting that let a banned user read through their group, a permission
service's error text reaching models, Ory Keto's batch limit, stale redaction spans failing every
retrieval, Chroma's batch limit, and the example gateway's streaming and OpenAI content parts. The
scenarios join the project and run weekly against the release on PyPI. SpiceDB's
`group_subjects` and Keto's `batch_size` change their defaults; see Security and Fixed.

### Added

- Acceptance scenarios (`acceptance/`): the applications a company builds with wardowl, run from
  the documentation against real servers, a real identity provider (Keycloak), real embedding and
  reranking models, and a local LLM, with every result checked against an oracle of who may read
  what that does not use wardowl. They cover 14 stores, seven permission systems, LangChain,
  LlamaIndex, Haystack and MCP, the HTTP service, day-2 operations with servers stopped mid-traffic,
  redaction and reranking, a team's own store and policy, and the example gateway: 3,912 checks,
  run weekly against the release on PyPI. A Kaggle notebook runs the assistant at scale on GPUs.

- `wardowl.InvalidQuery`, a `ValueError` for a query that cannot be searched through its own fault,
  raised by `Query.embedded`, `Query.keywords`, the retriever's checks of `k`, Vespa's keyword mode
  and `FaissBackend` (a query vector whose length is not `dim`). Code that catches `ValueError` keeps
  working; a backend of your own can raise it.
- `OIDCAuthenticator(ssl_context=...)` fetches the identity provider's keys with that context, for a
  provider whose certificate a private CA signed, instead of a process-wide `SSL_CERT_FILE`.
- `InMemoryBackend.delete(ids)`, as every other backend has; ids not stored are ignored.
- `ChromaBackend(batch_size=...)`.

### Security

- Example gateway (`examples/gateway`): the audit record links the records of one question by an
  HMAC-SHA256 under a key derived from `RAMPART_KEY_SECRET` (`question_hmac`) instead of an unkeyed
  SHA-256 (`question_sha256`), which anyone who could read the log could reverse when the only
  unknown in a question was a masked phone or ID number, by hashing every candidate. `rampart
  ingest` and `rampart verify-audit` now need `RAMPART_KEY_SECRET` (or `RAMPART_DEMO=1`), as
  `rampart serve` already did. A request from a disabled principal is now audited, without anything
  it carried, and counted as `account_disabled`; it was refused without a record, like a request
  with an unknown key. Every read of the audit log, through `/v1/audit`, `/v1/audit/verify` or the
  dashboard, is audited too.

- `SpiceDBAuthorizer` checks only the user by default (`group_subjects=False`, was `True`), and
  SpiceDB follows the group memberships it stores. With groups checked as subject sets of their own,
  a chunk was allowed when the user or any group had the permission, so under an exclusion such as
  `permission view = viewer - banned` a banned user still read a document granted to their group
  (the group is not banned), and under an intersection a grant through a group was refused. If your
  group memberships live only in the token and your permissions neither exclude nor intersect, pass
  `group_subjects=True` to keep the old behaviour; otherwise write the memberships to SpiceDB
  (`group:<name>#member@user:<id>`). A real-SpiceDB test checks that a banned member of a permitted
  group is denied.
- `AuthorizationError` messages are fixed and never carry the text of the exception behind them,
  which stays the error's `__cause__` (and so in logged tracebacks; `RetrievalEvent.cause` still names
  its type). The retriever wrapped an authorizer's or redactor's exception as
  `authorizer failed: <its text>`, so a failure such as
  `could not reach opa.internal:8181 as svc-wardowl (password hunter2)` reached a model through
  Haystack's `Agent`, which shows tool errors to it, and through LangChain and LlamaIndex agents.
  The same holds for the LlamaIndex postprocessor, the HTTP client the permission services share
  (which now names the exception's type, not its text), Casbin, Cedar, Cerbos `roles`, OPA's filter,
  SpiceDB's `caveat_context` and the OIDC key fetch.

### Fixed

- Example gateway: `/v1/chat/completions` accepts a message's `content` as OpenAI's list of text
  parts, joined with newlines, as well as a string; it refused the list with 422, so SDKs and chat
  UIs that send it got no answer. The 4,000-character limit applies to the joined text, a message
  holds at most 50 parts, and a part that is not text (an image, audio, a file) is refused with 422
  naming its type.
- Example gateway: `stream: true` on `/v1/chat/completions` answers in OpenAI's server-sent events
  (`chat.completion.chunk` objects, a last chunk with `finish_reason` and the `rampart` block, a
  usage chunk with `stream_options.include_usage`, then `data: [DONE]`). It was ignored and
  answered with one JSON body, which streaming clients could not read. The answer is checked in
  full first and then streamed in pieces, so the policy, masking, audit and metrics are those of
  any other answer, and a denied request is the usual 403.
- Example gateway: denied ingests and audit reads are counted in `rampart_policy_denials_total`, as
  `ingest_not_allowed` and `audit_not_allowed`; only denied questions were, although the reason
  codes existed. The dashboard's denials by reason count every action too.
- Example gateway: Compose passes the gateway's settings through, with the gateway's defaults.
  `RAMPART_LLM_API_KEY`, needed for a model server that wants a key, `RAMPART_MAX_BODY_BYTES`, the
  embedding model and dimension and the tuning settings could not be set. `RAMPART_LOCAL_MODEL`
  makes the `local-llm` profile pull a smaller chat model and serve it under the name the policy
  approves. `RAMPART_DEFAULT_MODEL` is gone: nothing read it, since the policy chooses the model.

- `KetoAuthorizer` sends 10 tuples per batch check by default (`batch_size=10`, was 100). Keto
  refuses more than `limit.max_batch_check_size` tuples, 10 unless configured, with HTTP 400, so with
  both defaults every check of more than 10 chunks failed, closed. Raise both together for larger
  batches. The real-Keto CI tests now send more than 10 tuples.
- `SpanRedactor` hides a chunk whole when one of its spans ends past the text, as when the text was
  shortened after its spans were written, as it does for a `sha256` mismatch. It used to raise, so
  every retrieval that reached the chunk failed, for its readers too. Spans that are wrong whatever
  the text (not two integers with `0 <= start < end`, and the other malformed cases) still fail the
  retrieval.
- `ChromaBackend` writes in batches of at most `batch_size` records: the client's own maximum
  (`get_max_batch_size()`, about 5,400 for a local client) by default, or 1,000 when the collection
  reports none. `add`, `delete` and `set_metadata` sent everything in one call, which Chroma refuses
  above its maximum. Every record is checked before the first batch is written.
- An open `CircuitBreaker` raises `AuthorizationError` caused by the new
  `wardowl.authorizers.CircuitOpenError`, so `RetrievalEvent.cause` is `"CircuitOpenError"`. It had
  no cause, which monitoring could not tell from a malformed answer.
- The HTTP service answers 422, echoing nothing, for a query it cannot search as sent (the new
  `wardowl.InvalidQuery`): `k` over the retriever's `max_candidates`, only a vector for a keyword
  backend, only text for a backend without an embed function, or a vector of the wrong length for
  `FaissBackend`. These answered 500. Other errors remain server faults and answer 500.

### Documentation

- Concepts says what each strategy sends to the store: post-filtering searches inside the
  authorizer's scope when it has one, and for an `AllOf` whose parts all give filters that scope is
  the whole filter, even with `strategy="postfilter"`. The authorizer table lists every built-in
  authorizer. Combining and the Chroma page's speed advice say the same.
- Combining shows `TenantAuthorizer`'s `attribute` as it is (`None`, meaning the field's name) and
  how one chunk serves several tenants through a list-valued `tenant` field.
- Vespa: `namespace` only names document paths, a search covers the whole schema, and tenants are
  kept apart by the permission filter or by a schema each. Redis: each index needs its own prefix,
  and clients need socket timeouts. Qdrant: index `tenant` in multi-tenant collections. Cassandra:
  `create_table` does not create the keyspace. Chroma, Elasticsearch and OpenSearch: searches are
  approximate, with what tunes their recall.
- Hybrid search lists Vespa's keyword mode, and Your own vectors covers LanceDB, FAISS, Vespa,
  Cassandra and Pinecone and what a query vector of the wrong size raises.
- Redaction warns that a span's `sha256` of a short value can be recovered by guessing, so the
  spans must not reach callers.
- Extending says `check_authorizer` proves consistency, not a correct policy, and shows a test of
  known cases.
- SpiceDB explains why `group_subjects=True` fails under an intersection or an exclusion, and what
  to do instead. Casbin says what a custom `request` skips and must do itself. Keto and OpenFGA show
  how to keep tenants apart in one server. OPA recommends files or bundles over policies uploaded
  through its API.
- Operations, Security and the integrations overview name all five HTTP authorizers, and the shared
  cache's Redis client gets timeouts. Revocation covers Keto, Cedar and Casbin.
- HTTP service: mapping a real provider's tokens, with Keycloak's `sub`, group paths, admin-only
  claims and audience mapper. LangChain: a per-call `subject=` wins over `for_subject`. Haystack:
  what a model can set on the agent tool, `inputs_from_state`, and `outputs_to_state` for getting
  the documents back. MCP: a stdio server for a signed-in user. Agent frameworks: the `mcp` 2 client
  takes its header on an `httpx2` client, and the MCP adapters for LangChain, LlamaIndex and
  Haystack.

- Example gateway: the README says exactly which requests are audited (not `/v1/me`, nor requests
  refused before the policy is asked with 401, 413 or 422), how the policy chooses the model and how
  to change it or serve a smaller one under the approved name, and how to stream. The quickstart
  waits for the models before ingesting (`docker compose wait ollama-pull`), uses
  `docker compose exec -T`, and says the model server needs the models pulled. It gives the status
  and body of an over-limit request (422) and of a disabled principal (403,
  `{"detail": "account disabled"}`), lists the `admin` demo principal, and explains that the express
  delivery question gets no answer because the vendor FAQ is a single chunk, quarantined whole. The
  threat model covers the keyed fingerprint, audited audit reads and disabled principals, and the
  limit on content parts.

- The Keto guide explains the batch limit and how to raise it; the example configuration keeps
  Keto's default on purpose.
- The HTTP service guide lists every status code, 500 included, and shows `ssl_context`.
- The SpiceDB guide describes the new default and why `group_subjects=True` must not be used with
  permissions that exclude or intersect; the README and the review checklist say the same.
- The security model, threat model, operations guide, runbook and Haystack guide describe fixed
  error messages and the `CircuitOpenError` cause; the redaction guide says which spans hide a chunk
  and which fail the retrieval; the Chroma guide describes batched writes.
- `OpenSearchBackend`'s docstring said to set `ef_search` in the index settings; that applies to the
  Faiss engine only, and with the default Lucene engine the query's `k` sets the search breadth.
- The README's Redis example sets `socket_timeout` and `socket_connect_timeout`, so a Redis that
  stops answering fails the search instead of holding it.
- `InMemoryBackend.add` says why it takes no `embeddings`: it matches words and keeps no vectors.

## [2.0.1] - 2026-10-24

The wardowl logo arrives, on PyPI, in the README and in 3D on the documentation's front page, and
the build tools in the lock file move past seven advisories.

### Documentation

- The README, PyPI page and documentation carry the wardowl logo, a midnight and gold owl guarding a
  document, with a favicon and a social card. The documentation's front page shows it in 3D: the owl
  watches, and every ten seconds the redaction bar on its document lifts for a grant or stays for a
  denial. The model loads after the page, from a 1.5 MB file, and the still image stays for reduced
  motion, saved data or browsers without WebGL.
- The security policy, the upgrade guide and the changelog describe needtoknow as it now is on
  PyPI: archived, with a last release that only installs wardowl.

### Security

- The lock file moves filelock to 4.2.4 and setuptools to 84.2.1, past GHSA-2g95-wg33-c5x2,
  GHSA-567q-5qvw-pf92, GHSA-9vhv-jxww-xfm5, GHSA-crv4-6mhj-j5jh, GHSA-jqc4-5cvw-7w85,
  GHSA-v4x8-3wr5-3wq8 and GHSA-xmj3-9gf7-h52w. Both are build and test tools; the package does not
  depend on them.

## [2.0.0] - 2026-10-24

The first release named wardowl: until 1.6.0 the project was needtoknow. It adds the Pinecone
backend and the fixes from the project's own security audit of every module, and it is a major
release because several of those fixes change behaviour that 1.x code may rely on.
[Upgrading](https://muzakkirhussain011.github.io/wardowl/upgrading.html) lists what to change.

### Changed

- **The project is renamed wardowl.** Install it with `pip install wardowl` and import `wardowl`. The
  repository and the documentation moved to `github.com/muzakkirhussain011/wardowl` and
  `muzakkirhussain011.github.io/wardowl` (the old repository address redirects). Everything named after the
  project changes with it: classes (`NeedToKnowRetriever` is now `WardowlRetriever`), environment
  variables (`NEEDTOKNOW_*` is now `WARDOWL_*`), the default cache namespace and metric names, and the
  field names the backends store (`needtoknow_terms`, `needtoknow_id` and `needtoknow_metadata` are
  now `wardowl_terms`, `wardowl_id` and `wardowl_metadata`), so collections written by needtoknow
  must be indexed again. needtoknow is archived on PyPI; its last release, needtoknow 2.0.0,
  has no code and only installs wardowl.

- Shared-cache keys changed, so entries written by an earlier version are not read after an
  upgrade, and during a rolling upgrade an `invalidate()` made by one version does not clear the
  other's entries: call `invalidate()` once the upgrade is done, or allow for `ttl`.
- The operations guide's `set_metadata` example merges the stored metadata instead of replacing it
  with the changed permission alone.

- The public filter translators raise the new `wardowl.backends.NothingMatches` (a `ValueError`) for
  a filter that can match nothing, instead of returning `None` or `{}`, which store clients read as
  no filter at all: `filter_to_query` (Elasticsearch, OpenSearch, Redis), `filter_to_where`
  (Chroma), `filter_to_qdrant`, `filter_to_mongo`, `filter_to_search`, `filter_to_expression`
  (Milvus), `filter_to_weaviate`, `filter_to_sql` (LanceDB), `filter_to_yql` and
  `filter_to_pinecone`. Chroma's `NEVER` constant is removed. The backends still return no hits for
  such a filter without asking the store.
- A search with `k` above what the store returns in one query raises `ValueError` instead of
  returning that many hits as if no more matched: 10,000 for Elasticsearch, OpenSearch, MongoDB
  Atlas Vector Search and Pinecone, and 1,000 for Cassandra.
- `PineconeBackend` waits 30 seconds by default (`wait=30.0`, was `None`) for a search to see each
  write, so a revoked permission or a deleted chunk is out of searches when the call returns.
  `wait=None` keeps the old behaviour.

- Haystack: `WardowlRetriever`'s `subject` input also accepts a function returning the `Subject`,
  called on every run. Because the input accepts any type, a connection to the component by
  component name alone is ambiguous: name the sockets, as in
  `pipeline.connect("embedder.embedding", "retriever.query_embedding")`.

### Added

- `PineconeBackend` (the `pinecone` extra): dense vector search on a Pinecone index through its
  data plane API (2026-07), with the permission filter as the query's metadata filter: `$in` on
  hex-encoded, type-tagged terms, joined with `$and` and `$or`, so Pinecone applies it inside the
  search. `wait=` makes writes wait until a search sees them, for serverless indexes' eventual
  consistency; ids that Pinecone cannot store are kept under their SHA-256; the API key never
  appears in errors. Tested with the conformance kit against a Pinecone serverless index.

### Security

- `CachingAuthorizer(shared=...)` requires an explicit `namespace`. Shared-cache keys held only the
  namespace, subject, kind, action and chunk, and the namespace defaulted to `"wardowl"`, so two
  caching authorizers sharing one Redis answered for each other:
  `AllOf(CachingAuthorizer(TenantAuthorizer(), shared=r), CachingAuthorizer(OpenFGAAuthorizer(...), shared=r))`
  skipped OpenFGA. A process also refuses, with `ValueError`, a second kind of authorizer under a
  Redis client (or `SharedCache`) and namespace it already uses.
- Cache keys are exact. Their parts were joined with NUL and no lengths, values were encoded with
  `json.dumps(default=repr)`, and this process's keys compared subjects with `==`, so `{1}` and
  `"{1}"`, the metadata keys `1` and `"1"`, lists and tuples, the attributes `1`, `True` and `1.0`,
  and an action and chunk id containing NUL could share a cached decision. Keys are now digests of a
  length-prefixed, type-tagged encoding, and a subject or chunk holding a value of a type that has
  no exact encoding is not cached: the authorizer is asked every time.
- A shared-cache entry whose expiry is NaN or infinite is a miss. `float()` accepted both, and a NaN
  expiry gave a local copy that never expired. Non-finite or non-positive lifetimes are never
  stored, and `ttl` and `local_ttl` must be finite.
- `SpanRedactor` hides the whole text of a chunk without its spans field (`required=True` by
  default; `[]` still means nothing to hide, and `required=False` restores the old behaviour). A
  missing field showed the chunk in full, and `set_metadata`, which replaces the metadata, dropped
  the field whenever permissions were updated without it. A span may now carry `sha256`
  (`wardowl.redaction.span_digest` of the text it covers); a chunk whose text no longer matches is
  hidden whole.
- Haystack: `BoundWardowlRetriever` takes the subject from `subject_provider`, a function such as
  one reading a contextvar the web layer sets, and has no `subject` input. `WardowlRetriever` takes
  the subject as a run input, so `ComponentTool` offered it to an agent's model, which could search
  as anyone; agents must use the bound component.
- The LangChain, LlamaIndex and Haystack retrievers return no chunk metadata unless named in
  `metadata_fields`, as the HTTP service and the MCP tool already did. They passed every key,
  `allowed_users`, `allowed_groups` and `tenant` included, into the documents a model, prompts,
  callbacks and traces receive. LangChain documents keep `relevance_score`.
- `Subject.attributes` is a deep copy that cannot be changed (read-only dicts, tuples, frozensets),
  and a subject's id and groups must be strings. Changing the attributes of a subject already used
  as a cache key, directly or through a list the caller still held, could hand a later subject the
  decision cached for the original.

- `FaissBackend.add` keeps only the last document of an id repeated in one call. Each copy used to
  get its own slot, and the earlier copies stayed in the index, out of reach of `delete` and
  `set_metadata`, so searches kept returning them with their old metadata after a revocation.
- `QdrantBackend` and `WeaviateBackend` derive every point or object id from the SHA-256 of the
  chunk id (`point_id`), a chunk id that is already a UUID included. A UUID used to be kept as it
  was, so another spelling of it (upper case, braces, `urn:uuid:`), or an id chosen to equal the
  UUID derived from another, landed on the same point. `set_metadata` and `delete` now act only on a
  point whose stored `wardowl_id` is the id asked for (Qdrant: a filter on the payload; Weaviate: the
  property, checked before each update and required by each delete), so they never change or remove
  a chunk that holds another id. A point or object written by another tool, without `wardowl_id`, is
  reached by its UUID written exactly as search results report it. Collections must be indexed
  again, as the rename already requires.
- `PineconeBackend` stores a chunk id that starts with `sha256-` under its SHA-256, like ids Pinecone
  cannot hold, so no chunk id can share a record with another's hashed id. `set_metadata` and
  `delete` fetch the records first (`GET /vectors/fetch`) and leave alone any record that holds
  another chunk id.
- `MilvusBackend.delete` and `set_metadata` select rows with `id in {ids}` and the ids as a filter
  parameter. pymilvus's `get(ids=...)` and `delete(ids=...)` write string ids into the expression
  between quotes, so an id such as `a','b` selected the chunks `a` and `b` as well.
- `set_metadata` never writes a whole chunk back. Milvus updates only the metadata and terms with a
  partial update (Milvus 2.6.2 or newer), LanceDB with an update-only merge of those columns, Chroma
  with an update (removing stale list fields in the same call), and Qdrant sets only the payload's
  metadata key. Before, a chunk deleted while `set_metadata` ran could be inserted again with its
  old text, and a concurrent `add` could have its new text replaced by the old.
- `CassandraBackend.set_metadata` uses plain writes, like `add` and `delete`, instead of a
  lightweight transaction. Cassandra orders writes to a row by timestamp, and the transaction's
  Paxos timestamp does not order with the client timestamps of plain writes, so a permission change
  could be applied over a later one. Ids are looked up first, so an id that is not stored still
  creates no row; a chunk deleted between the lookup and the update is left without text or vector,
  which an ANN search never returns.
- `PgVectorBackend` matches an integer value only where the stored number reads as that integer, as
  the reference semantics do: jsonb compares numbers by value, so `In("level", [1])` used to match
  `{"level": 1.0}`. Metadata keys that are not strings are refused, since JSON turned the key `1`
  into `"1"`, which a filter on the field `"1"` then matched.
- Chroma's `filter_to_where` refuses field names that contain `::` or equal `wardowl_metadata`,
  which addressed the stored list fields or the JSON copy rather than a metadata field.

- `CedarAuthorizer` no longer leaves out metadata values Cedar cannot hold. A float, None or an
  integer beyond 64 bits used to vanish from a chunk's attributes, so a `forbid ... when { resource
  has x && ... }` stopped applying and a `permit` could let the chunk through (a `sensitivity` of
  `3.0` from a store that returns every number as a float, say). Integers, and floats without a
  fraction, now become Cedar Longs; a chunk whose metadata holds any other such value, directly or
  in a list or record, is denied without asking Cedar and counted with the evaluation errors in the
  log; a subject attribute of that kind raises `AuthorizationError`.
- `CasbinAuthorizer(group_subjects=True)` sends each group as `group_prefix` + the group
  (`"group:eng"`; the prefix defaults to `"group:"` and cannot be empty). Groups used to be sent as
  the bare subject, so a group named like a user or a role (`"admin"`, `"ceo@example.com"`) got that
  user's or role's access. Policies that grant to groups must now name them with the prefix, and a
  subject id that starts with the prefix raises `AuthorizationError`.
- `GoogleDriveACL` and `drive_access` no longer count a Drive grant that has an expiry time. Such a
  grant was kept without its expiry, so the copy on the chunks outlived the share. Pass
  `expiring_grants=True` to count it until it expires: `Access.expires_at` is then the earliest
  expiry among the grants that count, and `with_access` writes it to the chunks as
  `access_expires_at` (UTC, to the second), so the file can be synced again by then. A later sync
  without one removes the field.
- `GoogleDriveACL.access` returns `Access()`, readable by nobody, for a file that is gone or no
  longer visible to the credentials (HTTP 404, or 403 with the reason `notFound` or
  `insufficientFilePermissions`). It used to raise, so a deleted or unshared file kept its old
  grants for good. Rate limits, server errors and every other error are still raised, keeping the
  grants until a sync succeeds.
- The HTTP authorizers (OPA, OpenFGA, SpiceDB, Cerbos, Keto) never use a kept-alive connection in a
  process other than the one that opened it. A worker forked after a request (gunicorn `--preload`
  with a request at start-up, for example) used to share its parent's socket, and the processes
  could read each other's permission answers.
- `OpenFGAAuthorizer` refuses, with `AuthorizationError`, a subject id or group name that is empty or
  contains `:`, `#`, `*` or whitespace. They are written into OpenFGA's `type:id` strings, where
  `bob#manager` would be a userset and `*` the wildcard. Keto, SpiceDB and Cedar send ids in
  separate fields and are unaffected.
- The HTTP authorizers refuse `ssl_context` with an `http://` url, where it was silently ignored,
  and refuse an `Authorization` header (SpiceDB's `token`, for one) over `http://` unless the url
  names this machine and no proxy applies. Pass `allow_insecure=True` to send it over a network you
  trust. Both raise `ValueError` when the authorizer is created.
- The HTTP authorizers' `timeout` now bounds a whole call except the DNS lookup of the host:
  connecting to each of its addresses, the TLS handshake, sending, every read of the answer, and any
  retry. The standard library has no timeout for the lookup, so the system resolver's own settings
  bound it, and the time it takes counts against `timeout`. It used to apply to each socket operation, so a server that sent its
  answer slowly could hold a retrieval for much longer. Answers larger than the new
  `max_response_bytes` (10 MiB by default) are refused with `AuthorizationError`.

- `wardowl.server.create_app` runs a synchronous authenticator, such as `OIDCAuthenticator`, in a
  worker thread, and awaits an async one. It used to call it on the event loop, so one slow fetch of
  the identity provider's keys stalled every request the service was handling. The MCP search tool
  does the same for a synchronous `subject_for`.
- `OIDCAuthenticator` keeps the provider's key set itself instead of in `PyJWKClient`'s per-key
  cache, which never expired. The set is fetched with a 5-second `timeout` (it was 30) and kept for
  `lifespan` (300 seconds), so a key the provider rotates out stops verifying within that time. A
  token naming a key that is not in the set fetches the set again at most once every `cooldown` (30
  seconds), and until then is refused at once, so tokens with made-up key ids can no longer make the
  service fetch the keys on every request. A key endpoint that fails answers 503 and is not asked
  again before the cooldown ends. `lifespan`, `cooldown` and `timeout` are new keyword arguments.
- `OIDCAuthenticator` verifies a token with its key's own algorithm, which must be one of
  `algorithms`, rather than the algorithm the token's header names. A token whose header names
  another algorithm than its key's (ES256 over an RSA key, say), a shared-secret key in the
  published set, a key set with no usable keys, and any other PyJWT error now answer 401 or 503
  instead of an internal server error with a traceback in the log. A token must name its key (`kid`).
- `wardowl.server` refuses a request body over `max_body_bytes` (256 KiB by default) with 413 before
  it is parsed or the caller is authenticated, by its `Content-Length` and by the bytes received.
  A `vector` holds at most `max_vector_length` numbers (4,096 by default), and `NaN` and infinities
  are refused with 422; they used to reach the retriever and fail with a 500.
- The MCP search tool's `query` holds at most `max_query_length` characters (4,000 by default), a
  new argument of `add_search_tool`.
- The `server` extra needs PyJWT 2.14 or later (it allowed 2.10.0, whose issuer check matched
  substrings, CVE-2024-53861; 2.14.0 also fixes the later advisories in the code `wardowl.server`
  uses), and the `langchain` extra needs langchain-core 1.3.3 or later (it allowed 1.0 to 1.2.4,
  open to serialization injection in `dumps` and `loads`, CVE-2025-68664, and releases before the
  fixes for the later prompt template and `load()` advisories).
- The Lock workflow resolves in a job with a read-only token and no stored credentials, never builds
  a source distribution, and refuses a version uploaded less than 7 days ago unless it was named in
  `upgrade`; only a second job, which runs no package code, can push, and it starts CI on the
  commit it pushes. The Pinecone tests, the only ones that receive an API key, run in a job of their
  own with only the packages they need and pytest's plugin autoloading off, on `main` or by hand,
  never for a pull request.
- Example gateway (`examples/gateway`): `/v1/chat/completions` accepts at most 20 messages of at
  most 4,000 characters, and both question endpoints cap model and collection names; the email
  pattern and the markdown image rule, which run before the policy decides, now take linear time
  (they were quadratic on inputs such as `a.a.a.a...`). API keys are stored as HMAC-SHA256 digests
  under a secret the gateway holds (`RAMPART_KEY_SECRET`) instead of scrypt hashes, so a request
  with a made-up key no longer costs 16 MiB and tens of milliseconds before authentication; existing
  principals files must be hashed again with `rampart key-hashing` and `rampart hash-key`. The demo
  principals, whose keys are published, are refused unless `RAMPART_DEMO=1`; the image no longer
  contains a principals file, and Compose mounts it and publishes the API on `127.0.0.1` only.
  Prometheus metrics moved from `/metrics` on the API to their own port (9464, not published by
  Compose), and policy denials are counted by fixed reason codes, so a caller's model name can no
  longer add label values.

- `CedarAuthorizer` no longer leaves out metadata keys that start with `__` or are not strings. A
  chunk holding `{"__restricted": true}` lost the key, so `forbid ... when { resource has
  "__restricted" }` never applied and a broad `permit` let the chunk through. Cedar's JSON reserves
  only `__entity`, `__extn` and `__expr`, for its escapes, so other keys that start with `__` now
  reach Cedar (with `schema`, declare them like any other attribute). A chunk whose metadata holds
  a reserved key or a key that is not a string, at any depth, is denied without asking Cedar and
  counted in the log, like a value Cedar cannot hold; a subject attribute holding one raises
  `AuthorizationError`. The entity references wardowl writes itself, and the `entities` you pass,
  are unchanged.

- `PgVectorBackend` writes a float without a fraction with one (`10000000000000000.0`), in lists and
  nested objects too. `json.dumps` writes every float of magnitude 1e16 or more with an exponent
  (`1.2345678901234568e+16`), which jsonb stores as an integer, so the value came back as an int and
  `In("owner", [12345678901234568])` matched it, in SQL and in the retriever's re-check, though
  `matches` does not. A pandas integer column with a missing value holds such floats. Rows written
  before keep the integer until they are written again with `add` or `set_metadata`.
- `FaissBackend` keeps its own copy of each chunk's metadata and returns copies, as
  `InMemoryBackend` does. `add` kept the caller's `Document`, `set_metadata` a shallow copy of the
  dict, and `search` returned the stored dict, so
  `hits[0].metadata["allowed_groups"].append("eng")`, or a change to a dict after passing it in,
  granted `eng` from the next search.
- `ChromaBackend.search` drops a hit whose metadata, rebuilt from the JSON copy, the filter does not
  match. `set_metadata` reads the stored fields and updates them in two calls, so an `add` between
  them could leave list fields the update did not remove, and the `where` filter matched the chunk
  on a group its metadata no longer granted.
- `WeaviateBackend.set_metadata` and `delete` count an object as written by another tool only when
  it has no string `wardowl_id`, as search results do, not when the id is empty. A chunk stored with
  the id `""` counted as another tool's, so a call naming its object's UUID as the chunk id changed
  or removed it.
- `VespaBackend` refuses the chunk ids `.` and `..` with `ValueError` in `add`, `set_metadata` and
  `delete`, before sending any request. httpx drops such a path segment, so the request went to
  another path, where a 404 passed for "not stored": a `delete` or `set_metadata` of `..` changed
  nothing and returned as if done. Vespa's `/document/v1` refuses these ids percent-encoded too.

- Haystack: a model calling `WardowlRetriever` through `ComponentTool` can no longer choose the
  subject. `ComponentTool` offered `subject` as a tool parameter, and it passes on every argument a
  model sends, even one it does not offer, converting it with pydantic, which built a `Subject` from
  a JSON object. The `subject` input is now typed `Callable[[], Subject] | Any`: `ComponentTool`
  leaves out an input whose type holds a function, and a value a model sends anyway arrives as
  JSON, which the component refuses with nothing retrieved. In an agent, give the tool the subject
  from the agent's state with `inputs_from_state={"subject": "subject"}`. `BoundWardowlRetriever`
  is unchanged.
- LlamaIndex's `PermissionNodePostprocessor` returns copies of the nodes it keeps, with only the
  metadata keys named in the new `metadata_fields`, none by default, in their relationships too, as
  the retrievers already did. It returned the nodes unchanged, and LlamaIndex's default
  `MetadataMode.LLM` put `allowed_users`, `allowed_groups` and `tenant` into prompts and traces. The
  nodes passed in are not changed, and the authorizer still sees all the metadata.

- Example gateway (`examples/gateway`): a request body over `RAMPART_MAX_BODY_BYTES` (1 MiB by
  default) is refused with 413 before it is parsed or the caller is authenticated, by its
  `Content-Length` and by the bytes received. Neither the app nor uvicorn set a limit, so a 1 GB
  POST to `/v1/ask` was read and parsed as JSON before the API key was checked. A principal marked
  `disabled` is refused with 403 where its key is checked, before its question is masked and
  scored; only the policy refused it before, after that work. The phone pattern and the "send data
  out" injection rule, which run before the policy decides, now take linear time (a run of 4,000
  zeros took most of a second, and `send.to.` repeated over 4,000 characters about a second): a
  phone number starts only where a number starts, so digits glued to a preceding digit or `+` are
  no longer read as one, and the rule's address starts only where a run of address characters
  starts, with at most 64 characters before the `@` and 63 in the domain label. Compose pins
  every image by digest (`ollama/ollama` was `latest`), and Dependabot keeps those pins current.

### Fixed

- `max_candidates` bounds every retrieval: a `k` above it raises `ValueError` (pre-filtering searched
  for, and re-checked, any `k`), the rerank depth is capped at it, and post-filtering checks no more
  candidates than the fetch it asked for, whatever the backend returns.
- `In(field, "12")` raises `TypeError` instead of holding the values `"1"` and `"2"`; so do bytes
  and a mapping.
- `CachingAuthorizer` no longer caches a `filter_for` or `scope_for` answer that is not `None` or a
  well-formed filter, as documented.
- `aretrieve` runs the redactor in a thread, not on the event loop.
- `PermissionedRetriever(audit_key=...)` refuses a key shorter than 16 bytes, as `RedisCache` does
  for `signing_key`.
- `InMemoryBackend` stores and returns copies of the metadata: a caller changing a returned hit's
  metadata, or the dict it added, changed the permissions of the stored chunk.
- The conformance kit checks the async paths: every `asearch` result against `search` (ids, order,
  text, metadata and scores, with and without a filter), retrieval through `aretrieve` as well as
  `retrieve`, and an authorizer's `acheck`, `afilter_for` and `ascope_for` against the sync methods.
  `check_authorizer` empties every `CachingAuthorizer` before each call, so a cache cannot hide an
  authorizer whose answers depend on the batch.
- The conformance kit's data holds what stores tend to confuse: `"1"` next to `1`, case and Unicode
  normalisation variants, and field names with a dot or letters outside ASCII next to nested
  objects. A backend may refuse a filter on such a field name with `ValueError`.

- `VespaBackend.search` raises when the result does not cover every document (`coverage.full`
  false, or `coverage.degraded` set), instead of returning partial results as complete.
- `RedisBackend.search` raises when a RESP3 reply carries the Query Engine's timeout warning, which
  marks partial results. The Redis guide now recommends `search-on-timeout fail`, the only
  protection for RESP2 clients.

- The email pattern in the redaction guide could take quadratic time on a long run of word
  characters and dots; it now starts a match only where such a run starts.

- `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without sending it, a chunk whose resource id
  the service would refuse: for OpenFGA an id with `:`, `#`, a space or a control character, the id
  `*`, or one that makes the object (`document:<id>`) longer than 256 characters; for SpiceDB
  anything but 1 to 1024 letters, digits and `/_|-=+`. One such id used to make the service refuse
  the whole request, so every retrieval that found that chunk returned nothing, for every user.

- `ElasticsearchBackend` and `OpenSearchBackend` send `allow_partial_search_results=false` with
  every search, and raise `RuntimeError` for an answer that reports a failed shard
  (`_shards.failed` above 0) or `timed_out: true`. Such an answer is HTTP 200 and carries only the
  other shards' hits, which were returned as complete.

- `PermissionedRetriever(audit_key=...)` raises `TypeError` for a key that is not `bytes` or
  `bytearray`. A string passed the length check, then made every event fail to build: the error was
  logged, no observer received an event, and retrieval went on unaudited.
- `RedisCache(signing_key=...)` raises `TypeError` for a key that is not `bytes` or `bytearray`, for
  the same reason: a string passed the length check, then failed every read and write of the
  shared cache.
- A shared-cache entry whose expiry is an integer too large for a float is a miss. `float()` raised
  `OverflowError`, which escaped the read, so every retrieval for that subject failed.
- The conformance kit requires `asearch` to refuse, with `ValueError`, every filter `search`
  refuses. When `search` refused a filter on an odd field name such as `team.name`, the kit skipped
  `asearch`, so an async path that dropped the filter and returned everything went unseen.

### Documentation

- The OPA, OpenFGA, SpiceDB and Cerbos pages say what `recheck=False` requires: the filter alone
  decides, and `In` matches a list field by any element (and Cerbos's translated `in` and
  `hasIntersection` match a single value), while `check` denies those shapes. Every chunk must store
  each field in the shape the policy assumes; the default re-check drops the others as violations.
  `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without asking, a chunk whose resource field is
  not one string.

- The Cassandra page says when a search falls back to one ANN query filtered in the process (a
  filter that needs more than `max_queries` ANDs, such as a subject in more than 254 groups at the
  default), that the search can then return fewer than `k` hits, or none, though more chunks match,
  with nothing in the result saying so, and how to avoid it: raise `max_queries`, send fewer groups,
  or put a term every match needs, such as a tenant, in the filter.
- The Cassandra page says the vector index is approximate, so a search can now and then return
  fewer than `k` hits when more match, and the conformance tests check Cassandra as an approximate
  index. The filter stays exact.

## [1.6.0] - 2026-10-23

Vespa and Apache Cassandra join the stores, each tested with the conformance kit against a real
server in CI. Vespa applies the permission filter inside its nearest-neighbor and BM25 search;
Cassandra applies it in the `WHERE` of each ANN query, with every value bound as a parameter.

### Added

- `CassandraBackend` (the `cassandra` extra): vector search on Apache Cassandra 5.0 with Storage-Attached
  Indexes. The filter becomes an OR of ANDs on type-tagged terms in an indexed `set<text>`; each AND
  is one ANN query with bound `CONTAINS` parameters, run concurrently, and the best `k` are kept; past
  `max_queries`, one query on the terms every match needs is filtered in the process.
  `set_metadata` uses a lightweight transaction so an id that is not stored creates no row;
  `create_table` creates the table and indexes. Tested with the conformance kit against Cassandra
  5.0.9. Writes go 4 at a time (`write_concurrency`), as Cassandra 5.0's vector index accepts few
  concurrent writes, and a refused write is retried once.
- `VespaBackend` (the `vespa` extra): nearest-neighbor or BM25 search on Vespa through its HTTP APIs,
  with the permission filter in the YQL query as conditions on hex-encoded, type-tagged terms, so
  Vespa applies it inside the search and no metadata value can change the query. HNSW or exact
  search; `schema(dim)` writes the schema and `examples/vespa` is a complete application package.
  Tested with the conformance kit against Vespa 8.763.13, in vector, keyword and hybrid search.

## [1.5.0] - 2026-10-23

Applications in any language can now use wardowl through an HTTP service that takes the user
from a verified OIDC token. Redaction hides the parts of a permitted chunk a user may not see,
before a reranker or a model reads it. Ory Keto and Casbin join the permission systems, and FAISS
joins the stores, with exact pre-filtering in the process.

### Added

- `FaissBackend` (the `faiss` extra): vector search with FAISS in the process. The permission filter is
  matched against the chunks' metadata and passed to FAISS as an ID selector, so pre-filtering is
  exact. Exact search by default, or any trained index that supports ID selectors; `add`, `delete`,
  `set_metadata`, and `save` and `load` to a directory. Passes the conformance kit.
- `CasbinAuthorizer` (the `casbin` extra): decisions from the Casbin enforcer you already use, in the
  process, with `batch_enforce`. Requests are `(subject id, object, action)`, or your own for ABAC
  models; `group_subjects=True` also asks with each of the subject's groups, for allow-only models.
  An enforcer that raises or answers with anything but booleans raises `AuthorizationError`. Tested
  against the reference answer on generated corpora and with the conformance kit.
- `KetoAuthorizer`: relationship checks against Ory Keto (`POST /relation-tuples/batch/check`), with no
  extra dependency. Each chunk's document is a Keto object; `group_subjects=True` also checks the
  subject's groups as subject sets. Keto has no reverse lookup, so the retriever post-filters. A
  result carrying an error, a malformed answer or a wrong number of results raises
  `AuthorizationError`. Tested against Keto 26.2.0 and with the conformance kit.
- An HTTP service, `wardowl.server` (the `server` extra), so applications in any language get
  permission-aware retrieval: `create_app(retriever, authenticate)` serves `POST /v1/retrieve`,
  `GET /healthz` and an OpenAPI description. The subject comes from the verified bearer token
  alone; `OIDCAuthenticator` verifies JWTs against the identity provider's published keys
  (asymmetric algorithms only, issuer, audience, expiry, an optional tenant claim). Unknown request
  fields are refused, a failed permission check answers 503 without details, and hits carry no
  metadata unless named.
- Redaction: `PermissionedRetriever(redactor=...)` hides parts of the chunks a subject may read, after
  the permission check and before a reranker or a model sees them. `SpanRedactor` hides spans listed
  in a chunk's metadata, each with its own readers; `PatternRedactor` hides text matching patterns
  unless the subject is in an exempt group. A redactor may change text and metadata but not which
  hits are returned, and one that fails makes the retrieval raise `AuthorizationError`.

## [1.4.0] - 2026-10-22

Nine of the ten stores now serve both halves of hybrid search under the permission filter: keyword
search (BM25) arrives on Qdrant, Milvus, MongoDB Atlas, Weaviate, Redis and LanceDB (Chroma has
none). Cedar joins OPA, OpenFGA, SpiceDB and Cerbos as a permission system, evaluated in the
process. Permissions can be synced from Google Drive. Operators get the cause of each failure, cache
statistics and an incident runbook; security reviewers get a threat model, the invariants with the tests that hold them, and a
compliance mapping. Several fail-closed gaps found by review are fixed, and from this release every
package carries SLSA build provenance.

**Python 3.11 or newer is required.** Python 3.10 reaches its end of life on 2026-10-31, and its
last dependency releases (Pillow 12.3 among them) no longer receive security fixes.

### Added

- `CedarAuthorizer` (the `cedar` extra): Cedar policies evaluated in the process with cedarpy. The
  principal carries the subject's id, groups and attributes, with its groups as parent entities;
  each chunk is a resource with its metadata as attributes; `entities` adds hierarchies. Cedar skips
  a policy whose evaluation errors, a `forbid` included, so a chunk whose evaluation reports an
  error is denied. No pre-filter: the retriever post-filters. Compared with the reference answer
  on generated corpora and checked with the conformance kit.
- `mode="keyword"` on the Qdrant backend: BM25 under the same `query_filter`, with no model. A
  collection created with `create_collection(..., text_search=True)` gets a sparse vector with
  Qdrant's IDF modifier; the backend writes each chunk's BM25 term weights there (the new
  `bm25_terms`), and Qdrant applies the inverse document frequency at search time. A vector
  backend given `text_search=True` writes both vectors, so one collection serves both halves of
  hybrid search. Tested with the conformance kit, alone and fused with vector search, in local
  mode and against Qdrant 1.19.2.
- `mode="keyword"` on the MongoDB backend: Atlas Search's BM25 (`$search`) on the text, under the
  same permission filter written with Atlas Search's own operators, in an index that the new
  `create_text_index` creates, so one collection serves both halves of hybrid search. A backend
  given `text_index` also waits for that index after each write. Tested with the conformance kit,
  alone and fused with vector search, against Atlas Local 8.0.30.
- `RetrievalEvent.cause`, and the span attribute `wardowl.error.cause`: the type of the exception
  behind a failure, such as `TimeoutError` or `ConnectionRefusedError` for a permission service that
  is slow or unreachable, and `None` for an error status or a malformed answer. The HTTP authorizers
  now keep the connection error as the cause of the `AuthorizationError` they raise after retrying.
- `CachingAuthorizer.stats()`: a `CacheStats` with answers served from this process and from the
  shared cache, answers the authorizer was asked for, shared cache failures, and entries held, for
  watching the hit rate and the shared cache. The operations guide lists the new alerts.
- Documentation for security review: a threat model of the library (assets, trust boundaries,
  seventeen threats with their mitigations, remaining risks and tests), the security invariants S1 to
  S10 with the tests that hold each one, and a guide to revocation and consistency, from bounded
  staleness to strict revocation.
- Tests of tenant and context isolation against an oracle written without the library's filter
  code: tenants that share user ids, group names and document ids (including look-alike tenant
  values such as `"acme"`, `"Acme"`, `"acme"` spelt with a Cyrillic a, `1` and `"1"`) through one shared cache;
  concurrent threads and asyncio tasks for different subjects, every returned chunk checked; and a
  cancelled retrieval.
- `OpenFGAAuthorizer(consistency=...)`: `"HIGHER_CONSISTENCY"` makes OpenFGA skip its cache for every
  check and list, so a tuple just written counts at once; `"MINIMIZE_LATENCY"` is OpenFGA's default.
  Tested against OpenFGA 1.22 in each mode.
- `create_index(..., algorithm="FLAT")` on the Redis backend: an exact vector index, for indexes small
  enough to compare every vector. HNSW stays the default; the docs now say that Redis re-adds a
  chunk's vector to the graph when `set_metadata` changes it, so a small, often-changed HNSW graph
  can miss a chunk.
- `mode="keyword"` on the Weaviate backend: Weaviate's BM25 on the text, under the same permission
  filter as its vector search, with no embedding function needed, so one collection serves both
  halves of hybrid search with `RankFusion`. Tested with the conformance kit, alone and fused with
  vector search, against Weaviate 1.39.12.
- `mode="keyword"` on the Redis backend: the Query Engine's BM25 on the text, under the same
  permission filter, in an index created with `create_index(..., content_field="content")`, so one
  index serves both halves of hybrid search. Each word of the query is sent as a query parameter,
  never as query syntax. `create_index` now creates indexes without stop words. Tested with the
  conformance kit, alone and fused with vector search, against Redis 8.10.2.
- `mode="keyword"` on the Milvus backend: BM25 on a sparse vector Milvus derives from the text,
  under the same permission filter and parameters, in a collection created with
  `create_collection(..., text_search=True)`, so one collection serves both halves of hybrid
  search. Tested with the conformance kit, alone and fused with vector search, against Milvus 3.0.2.
- `mode="keyword"` on the LanceDB backend: LanceDB's full-text index (BM25) on the text, under the
  same permission filter, applied before ranking, so one table serves both halves of hybrid search.
  `create_indexes(table, content_field="content")` builds the index, keeping every word; rows
  added after it are found by a scan until `table.optimize()`. Tested with the conformance kit,
  alone and fused with vector search, with LanceDB 0.40.
- Permission sync from Google Drive. `wardowl.sync.google_drive.GoogleDriveACL` reads who may
  read a Drive file, in My Drive or a shared drive, through your Drive v3 service, and
  `wardowl.sync.with_access` writes the answer onto the file's chunks for
  `MetadataACLAuthorizer`, keeping their other metadata. Users and groups map to their addresses,
  a domain to the group `"domain:<domain>"` and anyone to everyone when findable in search.
  Grants that do not open the file's content grant nothing: sharing by link alone (unless
  `link_sharing=True`), deleted accounts, expired grants, view grants such as a limited-access
  folder's metadata view, inherited grants on limited-access items, and grants a shared drive's
  settings shut out (members only, domain users only). `domains=` leaves out grants outside your
  organization's domains. `wardowl.sync.Access` is the source-neutral answer that later
  connectors return too.
- Documentation for operators and reviewers: an incident runbook (a document seen by the wrong
  person, rising `violations`, `AuthorizationError`, a failing or untrusted shared cache, an
  advisory affecting your version), a starting point for security reviewers, the configurations CI
  tests and what it does not, and a FastAPI service in `examples/fastapi_rag` that binds the caller
  from authentication. The threat model adds T17: in keyword search, BM25 statistics count chunks
  the subject may not read, so scores and order can hint at what they contain.
- CI runs the library tests on macOS and Windows as well as Linux, and fails when branch coverage
  drops below 90% for the package or 95% for the code that decides access. The release check
  installs each release from PyPI and runs the tests taken from its attested sdist.
- Benchmarks: the load benchmark checks every chunk returned under concurrent load, `--repeat`
  reports the median and range of several throughput runs, and the permissions benchmark adds a
  baseline that enforces the same permissions with PostgreSQL row-level security, as a role that
  does not bypass it.

### Fixed

- A retrieval cancelled while it ran (an asyncio task cancelled, a `KeyboardInterrupt`) produced no
  `RetrievalEvent`, though every call is meant to produce one. Observers now receive it, with
  `error="CancelledError"` (or the exception's name) and nothing returned; the exception is raised
  as before.
- `OpenFGAAuthorizer.filter_for` no longer treats a ListObjects answer that may have been cut short
  by the server's deadline as complete. OpenFGA stops at `OPENFGA_LIST_OBJECTS_DEADLINE` (3 s by
  default) and sends what it found without saying so; a call that took `list_objects_deadline`
  seconds or more (new, default 3) now gives no filter, so the retriever post-filters. Such a list
  never let anyone read more, but it could hide documents they may read while the result reported
  `complete=True`.

### Security

- The example gateway (`examples/gateway`) stores API keys as scrypt hashes (n=16384, r=8) with a
  random salt per principals file, instead of unsalted SHA-256, so a leaked principals file costs
  one scrypt per guessed key. `rampart new-key` issues random keys, `rampart key-hashing` starts a
  file with a new salt, and SHA-256 entries are refused rather than accepted as a fallback.
- `recheck=False` no longer trusts the backend completely. The authorizer is still not asked a second
  time, but each pre-filtered hit is matched against the filter in the process (no network call),
  so a backend that ignores or mistranslates the filter cannot leak; such hits are dropped and
  counted in `violations`, as with `recheck=True`. A custom backend with `recheck=False` must now
  return each chunk's metadata as stored, as the conformance kit already requires.
- `CerbosAuthorizer` refuses a subject with an attribute named `groups`. It was merged into the
  principal's attributes after the subject's own groups, so it replaced them in every decision.
- Filter parsing, and the pgvector, LanceDB and Redis translations that write query text, are
  fuzzed with ClusterFuzzLite (Atheris) on pull requests and every week. The fuzz target checks
  that parsing either round-trips or raises `ValueError`, that pgvector binds every field name and
  value as a parameter, and that LanceDB and Redis queries carry only hex-encoded terms. The test
  suite runs the same checks on generated filters.

## [1.3.3] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.3.2, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.
- A retrieval event that cannot be built is logged and dropped, instead of replacing the
  retrieval's result or error, and query fingerprints accept a lone surrogate.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.3.3, or allow for `ttl`.

## [1.3.2] - 2026-10-19

Completes the 1.3.1 fix for `CachingAuthorizer.invalidate()`. Upgrade if you use a shared cache.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- With a shared cache, `invalidate()` could still leave the revoked decision in its own process for
  up to `local_ttl` (1 second by default): a retrieval running during the call could copy the old
  entry from the shared cache after the process dropped its own entries and before the new epoch
  was written. The process now drops its entries again once the new epoch is in place. Found by the
  1.3.1 revocation test under concurrent traffic; a test now forces a retrieval into that moment.

## [1.3.1] - 2026-10-19

A security fix for permission caching. Upgrade if you use `CachingAuthorizer`, and above all if
you call `invalidate()` to revoke access.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused. Tests revoke a permission under concurrent retrievals,
  with one and three replicas, and check that no retrieval starting after `invalidate()` returns
  sees the document (other replicas within `local_ttl`).

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field, so a revocation hook that knew only the user id
  (`invalidate(Subject("bob"))`) cleared nothing for `Subject("bob", ["eng"], {"tenant": "acme"})`.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.3.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError`. Before,
  a deep enough document from a policy engine or a shared cache recursed until Python raised
  `RecursionError`. The shared cache treats such an entry, and any entry too deep to decode, as a
  miss.

## [1.3.0] - 2026-10-17

Retrieval as production RAG is built now, with permissions applied at every step. Hybrid search
fuses keyword and vector results under the same permission filter (BM25 on Elasticsearch and
OpenSearch, full-text search on pgvector). A reranker orders the results after the permission
check, so a cross-encoder or a hosted rerank API never sees a chunk the user may not read. Searches
can use vectors you computed, from an embedding service or another modality. AI agents get an MCP
search tool whose permissions come from the verified access token, never from the model, and
Haystack pipelines get a retriever component. Four more stores: MongoDB Atlas Vector Search,
Weaviate, Redis and LanceDB. `set_metadata` changes a document's permissions without embedding it
again, and the conformance kit checks it, like everything else, against every store.

Python 3.10 is still supported. It reaches its end of life on 2026-10-31, after this release, so
under the support policy the first minor release after that date, 1.4, drops it, not 1.3 as the
1.2.0 notes said.

### Added

- `set_metadata({chunk_id: metadata})` on every built-in backend: replaces stored chunks' metadata
  without embedding them again, so a document's permission change reaches search without the cost
  of re-embedding. The whole metadata is replaced (no old permission key survives, including where
  the store's own partial updates would merge), ids that are not stored are ignored, and it returns
  once searches see the change, waiting for MongoDB's search index. The conformance kit checks it
  for any backend that has it, against each real store in CI. Guide: "Changing a document's
  permissions" in Operations.
- Reranking after the permission check: `PermissionedRetriever(..., reranker=..., rerank_depth=50)`
  finds the top `rerank_depth` chunks the subject may read, lets a cross-encoder or hosted rerank
  API order them, and returns the first k. The reranker never sees a chunk the subject may not
  read, and can reorder, rescore or drop hits but not add one; returned hits keep their stored
  text and metadata. Any function or object with `rerank` (and optionally `arerank`) works.
  Events report `reranked` and `rerank_ms`, and OpenTelemetry records a
  `wardowl.rerank.duration` histogram. Guide: "Reranking".
- `wardowl.backends.lancedb`: LanceDB, the embedded vector database, with the permission filter
  applied before the vector search ranks (LanceDB's pre-filtering). Permission terms are stored
  hex-encoded in a list column and matched with `array_has_any`, and deletes match a hex-encoded id,
  so no group name, user id or document id is ever part of the SQL. `add` upserts, keeping the last
  version of a repeated id; metadata is stored as JSON and returned exactly. `create_table` and
  `create_indexes` (a `LABEL_LIST` index on the terms) set the table up. Tested with the
  conformance kit, with text and vector queries, on LanceDB 0.40. Extra: `wardowl[lancedb]`.
- `wardowl.mcp.add_search_tool`: a permission-aware search tool for MCP servers (MCP Python SDK
  2.3 or later), so AI assistants and agents search with the calling user's permissions. The user
  comes from the access token the server verified for the request, through your `subject_for`
  function, never from the tool's arguments; a call without a verified token is refused, and a
  fixed `Subject` serves single-user stdio servers. The read-only tool takes `query` and a bounded
  `limit`, returns structured results without metadata unless keys are named, and reports a
  permission service failure to the model without internal details. Extra: `wardowl[mcp]`.
- `Query`: search with a vector you computed, alone or with the text. `retrieve(Query(vector=...),
  subject)` searches an image's or another model's embedding, an embedding from your own service
  or cache, or one shared by every backend in `RankFusion` (vector backends use the vector, keyword
  backends the text). Permissions apply exactly as for text. Every vector backend accepts it, and
  `embed` is now optional on all of them: build a backend without one, write with
  `add(documents, embeddings=...)` and search with vectors. The LangChain and LlamaIndex store
  wrappers search by vector, the Haystack component takes a `query_embedding` input, and the
  LlamaIndex retriever forwards a `QueryBundle`'s embedding. Audit fingerprints use the query's
  text when there is text and its vector otherwise; events never contain the vector.
  `check_backend(..., query=...)` runs the conformance kit with any query, and CI runs it with
  vector queries against every store. Guide: "Your own vectors".

- `RankFusion`: hybrid search. A backend that searches several backends with the same permission
  filter and fuses their rankings by reciprocal rank fusion, with weights, a rank constant and a
  per-backend depth; `aretrieve` searches them concurrently. A chunk two backends return with
  different metadata is left out and logged, since its permissions are uncertain. Guide: "Hybrid
  search".
- `mode="keyword"` on the pgvector backend: PostgreSQL full-text search (`websearch_to_tsquery`,
  ranked by `ts_rank_cd`) under the same permission condition, so one table serves hybrid search.
  `create_schema(..., text_search="english")` adds the full-text GIN index, and CI checks that the
  query plan uses it. Tested with the conformance kit, alone and fused with vector search.
- `mode="keyword"` on the Elasticsearch and OpenSearch backends: BM25 on the text under the same
  permission filter, with no embedding function needed. Tested with the conformance kit, alone and
  fused with vector search, against both engines.
- `wardowl.backends.redis`: vector search on the Redis Query Engine (Redis 8 or later, or Redis
  Stack), with the permission filter in the same KNN query. Permission terms are stored
  hex-encoded in a TAG field, so no group name or user id can add query syntax and TAG matching's
  case folding never merges two values; metadata is stored as JSON and returned exactly.
  `create_index` sets up the index, and `redis.asyncio` clients give a native `aretrieve`. Tested
  with the conformance kit against Redis 8.10.2. Extra: `wardowl[redis]`, as for the shared
  cache.
- `wardowl.haystack.WardowlRetriever`: a Haystack pipeline component over any wardowl
  backend and authorizer. The authenticated user is a run input, so one pipeline serves every
  user, and the component refuses to run without a `Subject`. It outputs Haystack `Document`s and
  `complete`, and has `run_async`. The extra requires `haystack-ai` 2.12 or later; CI tests the
  current Haystack 3 release. Extra: `wardowl[haystack]`.
- `wardowl.backends.mongodb`: MongoDB Atlas Vector Search, with the permission filter as the
  `$vectorSearch` pre-filter on a type-tagged array field (a BSON filter, so values are never query
  syntax). Atlas indexes writes shortly after they happen, so `add` and `delete` wait until the
  index shows the change, including a replaced chunk's new permissions (`wait=False` skips it).
  `create_search_index` sets up the index, and `AsyncMongoClient` collections give a native
  `aretrieve`. Tested with the conformance kit against `mongodb/mongodb-atlas-local` 8.0.30. Extra:
  `wardowl[mongodb]`.
- `wardowl.backends.weaviate`: Weaviate, with the permission filter applied inside the vector
  search. Permissions are matched on a type-tagged `text[]` property with `field` tokenization, each
  term guarded at both ends so case and whitespace never blur two values, through the client's
  filter objects; metadata is stored as unindexed JSON and returned exactly. Document ids become
  UUIDs, with the original kept on the object. `create_collection` sets up a self-provided cosine
  vector, and `WeaviateAsyncClient` gives a native `aretrieve`. Tested with the conformance kit
  against Weaviate 1.39.12. Extra: `wardowl[weaviate]`.

### Changed

- `SearchBackend.search` receives the query exactly as it was given to `retrieve`, a string or a
  `Query`, and its `query` parameter is typed `Any`, so backends typed for strings still satisfy
  the protocol and keep working with string queries.
- `retrieve` and `aretrieve` raise `TypeError` for a query that is neither a string nor a `Query`,
  instead of passing it to the backend.

### Fixed

- Chroma: `add` deletes and re-adds a replaced chunk, so none of its old metadata keys can remain
  and match a permission filter; it also checks that there is one embedding per document.

## [1.2.2] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.2.1, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.2.2, or allow for `ttl`.

## [1.2.1] - 2026-10-19

The security fix of 1.3.1, backported to 1.2 as the security policy promises for the previous minor
release. Upgrade if you use `CachingAuthorizer`, and above all if you call `invalidate()` to revoke
access; or move to 1.3.1.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused.

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.2.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError` instead of
  recursing until `RecursionError`, and the shared cache treats such an entry as a miss.

## [1.2.0] - 2026-10-17

Breadth and scale. wardowl now works with six more systems: the Qdrant, Milvus, Elasticsearch
and OpenSearch vector stores, and the SpiceDB and Cerbos permission systems. Authorizers combine
(`AllOf`, `AnyOf`, `TenantAuthorizer`), with scoped post-filtering for multi-tenant indexes; the
permission cache can be shared across replicas through Redis; OpenTelemetry metrics show where each
retrieval spends its time; and a subject can be in tens of thousands of groups. A conformance kit,
`wardowl.testing`, checks your own backends and authorizers against the reference semantics, as
CI does for every built-in one. One behaviour is stricter: an authorizer answer that is not a
filter now fails closed instead of reaching the backend.

Python 3.10 is still supported. 1.2 comes out before 3.10's end of life on 2026-10-31, and the
support policy drops a version in the first minor release after that, so 1.3 will be the first
release without it, not 1.2 as the 1.1.0 notes said.

### Added

- A project logo. The documentation's front page shows it as a live 3D model (three.js, vendored, so
  the site makes no third-party requests), with a still render as the fallback when WebGL is
  unavailable or the reader prefers reduced motion; it also appears in the sidebar, as the favicon
  and at the top of the README.
- A redesigned documentation site on the Shibuya theme: a product-style front page (feature cards,
  how it works, code in tabs, integrations, measured results), a top navigation bar with menus,
  copy buttons on code, edit links on every page, and link previews. It uses each platform's own
  font and no third-party services, and its buttons meet WCAG AA contrast in light and dark mode.
- `wardowl.testing`: a conformance kit for your own backends and authorizers.
  `check_backend` compares a backend with the reference filter semantics on generated documents and
  nested filters (exact results, `k`, ordering, unchanged metadata, `asearch`, no leaks through the
  retriever); `check_authorizer` checks that `check` and `filter_for` agree. Every built-in backend,
  and the example OPA policy against a real OPA, runs it in CI. `HashingEmbedder` gives vector stores
  deterministic embeddings without a model.
- `wardowl.backends.qdrant`: a Qdrant backend. The filter becomes Qdrant's `query_filter`
  (`MatchAny` per value type, an empty `In` decided before the query), `create_collection` adds
  keyword payload indexes on the permission fields, points use LangChain's Qdrant layout so existing
  collections work, and `AsyncQdrantClient` gives a native `aretrieve`. Tested with the conformance kit
  against a real Qdrant server and Qdrant's local mode. Extra: `wardowl[qdrant]`.
- `wardowl.backends.elasticsearch` and `wardowl.backends.opensearch`: kNN search with the
  permission filter applied during the search. Permissions are matched on one type-tagged keyword
  field (`wardowl_terms`), because keyword fields compare strings and would otherwise match the
  integer `1` against `"1"`; metadata is stored unindexed and returned exactly, and never adds
  mappings. `create_index` sets up the mapping, `permission_terms` lets other ingestion pipelines
  write the field, and async clients give a native `aretrieve`. Tested with the conformance kit
  against Elasticsearch 9.5.5 and OpenSearch 3.9.0. Extras: `wardowl[elasticsearch]`,
  `wardowl[opensearch]`.
- `wardowl.backends.milvus`: a Milvus backend, with the permission filter applied inside the
  vector search. Permissions are matched on a type-tagged array field, as on the search engines,
  and the values are passed as Milvus filter parameters, never written into the expression, so a
  group name cannot change what a filter selects. `create_collection` sets up the schema and
  indexes, searches use strong consistency by default, and `AsyncMilvusClient` gives a native
  `aretrieve`. Tested with the conformance kit against Milvus 3.0.2. Extra: `wardowl[milvus]`.
- `wardowl.authorizers.spicedb.SpiceDBAuthorizer`: SpiceDB over its HTTP API, with no extra
  dependency. CheckBulkPermissions in batches (answers matched by the request they echo),
  LookupResources for the pre-filter (post-filtering when the list may be truncated or is
  conditional on a caveat), the subject's groups as `group:<name>#member` subject sets, caveat
  context, a configurable consistency, and conditional answers treated as failures, not decisions.
  Tested against a real SpiceDB for random relationship graphs and with the conformance kit.
- `wardowl.authorizers.cerbos.CerbosAuthorizer`: Cerbos over its HTTP API, with no extra
  dependency. CheckResources per chunk (metadata as resource attributes, the subject's groups and
  attributes on the principal), and pre-filters translated from PlanResources query plans: `and`,
  `or`, `hasIntersection`, `in` and `==` on resource attributes become exact filters, anything else
  falls back to post-filtering. Example policy in `examples/cerbos`. Tested against Cerbos 0.56.0.
- `AllOf` and `AnyOf` combine authorizers (each part asked only about the chunks still undecided),
  and `TenantAuthorizer` keeps every subject inside its tenant; a subject without a tenant gets
  nothing. Guide: "Combining authorizers and multi-tenancy".
- Scoped post-filtering: an authorizer may offer `scope_for(subject, action)`, a filter every
  readable chunk matches. When there is no exact filter, the retriever post-filters inside the
  scope (for example only the subject's tenant) and still checks every candidate, so a tenant with
  a small share of the index keeps its recall. `AllOf` and `AnyOf` provide scopes, and
  `CachingAuthorizer` caches them.
- `CachingAuthorizer(shared=...)`: a second cache tier shared by every process, with
  `wardowl.caching.RedisCache` (Redis, Valkey, Dragonfly, Redis Cluster; extra
  `wardowl[redis]`). Replicas reuse each other's answers, each keeps its own copy for at most
  `local_ttl` seconds (1 by default), and `invalidate()` reaches every replica within `local_ttl`.
  Shared entries carry their expiry time, so the revocation bound holds across replicas; key names
  are digests; values can be signed with HMAC-SHA256 (`signing_key`) so planted entries are
  ignored; and a failing shared cache is only ever a miss.
- The conformance kit also generates nested lists and objects in metadata, filters of 300 values,
  and subjects in hundreds of groups.
- `wardowl.otel.OpenTelemetryMetrics`: retrieval metrics for dashboards and alerts. The
  `gen_ai.client.operation.duration` histogram from the OpenTelemetry GenAI conventions, retrievals
  by outcome, time spent in the backend and in the authorizer, chunks returned, examined and denied,
  and violations. Attributes have a handful of values each; subjects, queries and chunks are never
  recorded.
- `RetrievalEvent.search_ms`, `authorize_ms` and `searches`: where the time of each retrieval went,
  also on spans and in audit lines. Spans also carry the GenAI conventions' `gen_ai.operation.name`,
  `gen_ai.retrieval.top_k` and `error.type`; their name is unchanged.

### Changed

- The retriever rejects a `filter_for` (or `scope_for`) answer that is not a filter with
  `AuthorizationError`, instead of handing it to the backend.
- pgvector: an `In` with more than 100 values (a subject in many groups) is sent as two array
  parameters instead of two per value. A subject in more than about 32,000 groups used to exceed
  PostgreSQL's limit of 65,535 parameters, and retrieval failed; CI now serves one in 40,000. The
  operations guide has a section on subjects in very many groups for every backend.

### Documentation

- A production checklist: what to settle before launch, and what happens when each part of the
  system fails, with what the operator sees.
- The security model covers the shared cache's trust boundary, what a wrong `scope_for` can and
  cannot do, how each backend returns metadata, and the new authorizers.

## [1.1.2] - 2026-10-16

The README, and so the PyPI page, opens with the logo lockup, centred, with a dark version for
dark themes. The library is unchanged from 1.1.0.

## [1.1.1] - 2026-10-16

The README, and so the project's PyPI page, shows the project logo. The library is unchanged from
1.1.0.

## [1.1.0] - 2026-10-16

Hardening for production: resilient connections to remote permission services, releases that can
be verified, and the first measurements at a million chunks. The one change in behaviour is that
the authorizers no longer follow HTTP redirects. Python 3.10 reaches its end of life on 2026-10-31;
following the support policy, 1.2 will be the first release without it.

### Added

- The OPA and OpenFGA authorizers reuse HTTP connections (one per thread) instead of opening one per
  request, and replace a connection the server closed while idle.
- Transient failures are retried once by default (`retries=`): refused or reset connections,
  missing responses, HTTP 502, 503 and 504. Timeouts and other errors are not retried.
- `wardowl.authorizers.CircuitBreaker`: pauses requests to a permission service that keeps
  failing, then probes; pass it as `circuit_breaker=`.
- `ssl_context=` on both authorizers, for your own CA or mutual TLS.
- `PgVectorBackend(ef_search=...)` sets pgvector's HNSW search depth for each search, trading
  latency for recall, without changing the setting for other users of the connection pool.
- A load benchmark (`benchmarks/load_bench.py`): a million MS MARCO passages on pgvector, recall
  and latency by selectivity, throughput with concurrent users, and the cost of OPA as the
  permission service with and without caching. CI runs a small version against real services. The
  first results are on the Benchmarks page of the documentation.
- Fault-injection tests: under any sequence of injected faults, retrieval through OPA returns
  exactly the permitted top-k or raises `AuthorizationError`.

### Security

- Releases attach the published files to the GitHub release with a Sigstore signature for each
  and a CycloneDX SBOM; SECURITY.md shows how to verify a download.
- Every GitHub Action is pinned to a commit, checkouts no longer keep credentials, CodeQL scans the
  Python code and the workflows, and OpenSSF Scorecard publishes the project's security score.

### Changed

- The authorizers no longer follow HTTP redirects; a redirect is an error, so retrieval fails
  closed.

## [1.0.0] - 2026-10-16

The first stable release. The public API in the
[API reference](https://muzakkirhussain011.github.io/wardowl/api.html) now follows semantic
versioning. Everything in 1.0.0rc1 below is included; these are the changes since the release
candidate.

### Added

- Tested on Python 3.14. Every Python version CPython still supports is supported, and a version is
  dropped in the first minor release after its end of life (Python 3.10's is 2026-10-31).
- A release procedure for maintainers in CONTRIBUTING.

### Removed

- `RetrievalEvent.extra`, a field nothing ever set. Audit lines no longer carry an empty `"extra"`.
  Removed before 1.0 so that it is not frozen into the stable API; adding a field later is
  compatible, removing one is not.

## [1.0.0rc1] - 2026-10-16

The first release candidate for 1.0. The public API in the
[API reference](https://muzakkirhussain011.github.io/wardowl/api.html) is what 1.0.0 will ship:
changes before then are fixes, unless testing the release candidate shows that a breaking change is
needed, in which case it will be announced in a second release candidate. Install it with
`pip install --pre wardowl`.

### Added

- `PermissionedRetriever` with `prefilter`, `postfilter` and `auto` strategies, sync
  (`retrieve`) and async (`aretrieve`) entry points sharing one implementation.
- Failing closed: any authorizer error, wrong number of decisions or non-boolean decision raises
  `AuthorizationError` and returns nothing.
- Re-check of pre-filtered results, with `RetrievalResult.violations` counting hits that a
  backend returned despite the filter.
- A backend-neutral filter language (`In`, `And`, `Or`) with reference semantics in `matches`;
  empty `And`/`Or` are rejected because an empty conjunction would mean "allow everything".
- `MetadataACLAuthorizer` (permissions stored on each chunk) and `FunctionAuthorizer` (your own
  logic).
- `InMemoryBackend` with deterministic keyword scoring, for tests and small corpora.
- Property-based tests (Hypothesis) for the security guarantees on Python 3.10 to 3.13.
- Contributor documentation: CONTRIBUTING, GOVERNANCE, CODE_OF_CONDUCT, SECURITY.
- `wardowl.backends.pgvector`: PostgreSQL + pgvector backend with the permission filter in the
  query as JSON containment tests (GIN-indexable), iterative HNSW scans, `create_schema`, and a
  native async search when given an async pool. Extra: `wardowl[pgvector]`.
- `wardowl.backends.chroma`: Chroma backend translating filters into `where` clauses. It never
  stores Chroma array metadata: in chromadb 1.5.x (local mode) a deleted collection's array
  metadata surfaces on new records in other collections and is used by `where` filters
  (chroma-core/chroma#7594). List values become
  one boolean field each, and search results are rebuilt from an exact JSON copy of the metadata,
  so the re-check always sees the true permissions. Extra: `wardowl[chroma]`.
- `wardowl.authorizers.opa`: Open Policy Agent authorizer using only the standard library, with
  per-chunk decisions and optional policy-provided filters; every failure fails closed. Example
  policy in `examples/opa`.
- `filter_to_dict` / `filter_from_dict` for a JSON form of filters.
- `wardowl.authorizers.openfga`: OpenFGA authorizer over the HTTP API (no extra dependency):
  batched BatchCheck, ListObjects pre-filtering that falls back to post-filtering when the list may
  be truncated, and the subject's groups sent as contextual tuples. Tested against a real OpenFGA
  for random relationship graphs.
- `wardowl.langchain`: `WardowlRetriever` (a LangChain retriever; refuses to run without a
  subject) and `LangChainVectorStoreBackend` (any LangChain vector store, post-filtered).
  Extra: `wardowl[langchain]`.
- `wardowl.llamaindex`: `WardowlRetriever`, `PermissionNodePostprocessor` and
  `LlamaIndexBackend`. Extra: `wardowl[llamaindex]`.
- Runnable examples for LangChain and LlamaIndex, executed in CI.
- `CachingAuthorizer`: caches pre-filters and per-chunk decisions (keyed by chunk id and
  metadata) per subject for `ttl` seconds, with that as the documented revocation bound; errors and
  malformed answers are never cached; `invalidate()`; sync and async.
- Retrieval events: every call, including failed ones, produces a `RetrievalEvent` for
  `observers` (subject id, strategy, returned ids, denied and violation counts, duration, error
  type, optional HMAC of the query). Never query or chunk text. `JsonLinesAudit` writes them as
  JSON lines; observer failures are logged and never affect retrieval.
- `wardowl.otel.OpenTelemetryObserver`: one span per retrieval with counts and outcomes, the
  subject id only on request. Extra: `wardowl[otel]`.
- Documentation site (Sphinx, MyST, Furo) in `docs/`: getting started, concepts, a security model,
  operations, one page per integration, extending wardowl, benchmarks and an API reference.
  CI builds it with warnings as errors, and every push to `main` publishes it to
  <https://muzakkirhussain011.github.io/wardowl/>. Extra: `wardowl[docs]`.
- Benchmark harness (`benchmarks/`): recall@k against exact ground truth, leaks, completeness and
  latency for exact search, Chroma and pgvector across permission selectivity and layouts, with a
  Kaggle notebook for real embeddings. CI runs a small version against real Chroma and PostgreSQL
  and fails on any leak.
- Conformance tests: every backend must return exactly the documents the reference semantics
  select, for random metadata and nested filters, and never leak through the retriever. CI runs
  them against real PostgreSQL, Chroma and OPA.

### Changed

- `RetrievalResult.complete` is documented precisely: it is `False` when wardowl knows results
  may be missing, and it trusts the backend's ranking, so it cannot see results an approximate index
  failed to return. The benchmarks measure how often that happens.
- Filter values must be strings or integers, and metadata values of other types (booleans,
  floats) never match. This keeps every backend's answer identical to the reference.
- The project is now `wardowl`, a permission-aware retrieval library (ADR 0005). The earlier
  Rampart gateway lives on as an example deployment in `examples/gateway`.

[Unreleased]: https://github.com/muzakkirhussain011/wardowl/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/muzakkirhussain011/wardowl/compare/v2.1.1...v3.0.0
[2.1.1]: https://github.com/muzakkirhussain011/wardowl/compare/v2.1.0...v2.1.1
[2.0.2]: https://github.com/muzakkirhussain011/wardowl/compare/v2.0.1...v2.0.2
[2.1.0]: https://github.com/muzakkirhussain011/wardowl/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/muzakkirhussain011/wardowl/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.6.0...v2.0.0
[1.6.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.0...v1.3.0
[1.2.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.0.0rc1...v1.0.0
[1.0.0rc1]: https://github.com/muzakkirhussain011/wardowl/releases/tag/v1.0.0rc1

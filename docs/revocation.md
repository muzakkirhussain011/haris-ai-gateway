# Revocation and consistency

When someone loses access to a document, how soon does wardowl stop returning it? The delay is
the sum of what each layer adds. This page lists the layers, then two ways to configure them: a
stated bound on staleness, which suits most workloads, and strict revocation, for access that must
stop as soon as it is withdrawn.

## Where the delay comes from

1. **Where the permission lives.** With permissions on the chunks (`MetadataACLAuthorizer`,
   `TenantAuthorizer`, or an OPA or Cerbos policy that reads chunk metadata), a change takes effect
   when the chunks are updated: `set_metadata`, a re-index, or the next run of a permission sync
   such as [Google Drive](integrations/google-drive.md). A re-index from data read before the
   change that lands after it undoes the change, so the writes for one document must happen one
   after another ([Operations](operations.md#one-writer-at-a-time-for-each-document)). With a
   permission service (OpenFGA, SpiceDB, Keto, OPA data, Cerbos policies), it takes effect when the
   service has the change; with an in-process engine (Cedar, Casbin), when the engine in your
   process has it.
2. **The permission service's own consistency.**
   - OpenFGA with `consistency="MINIMIZE_LATENCY"` (its default) may answer from its cache, when one
     is configured; `"HIGHER_CONSISTENCY"` skips it. See [OpenFGA](integrations/openfga.md).
   - SpiceDB with `{"minimizeLatency": True}` (the default here) answers from a recent snapshot.
     SpiceDB documents the bound as its revision quantization interval plus 10% of it, 5.5 seconds
     with its defaults; measured against SpiceDB 1.56 on Postgres it reached 8.7 seconds, and a
     revoked permission sometimes granted access again after it had stopped. `SpiceDBAuthorizer`
     therefore declares `max_staleness` of 11 seconds, twice the documented bound.
     `{"fullyConsistent": True}`, or `{"atLeastAsFresh": {"token": zedtoken}}` with the token from
     your write, answers from current data. See [SpiceDB](integrations/spicedb.md#consistency-and-revocation).
   - OPA and Cerbos answer from the policies and data they have loaded; bundle polling or a policy
     reload adds its interval (Cerbos reloaded a changed policy file in 2.5 seconds, at most 4,
     in the same measurements).
   - Keto has no consistency option: `KetoAuthorizer` sends none, as Keto's check API takes none.
     A deleted relationship stops granting access once Keto's checks no longer see it, which
     depends on your Keto deployment and its database, not on wardowl.
   - Cedar and Casbin run in your process, so there is no service to wait for: a change applies on
     the next check once the engine has it. `CedarAuthorizer` evaluates the policies it was created
     with (create a new one to change them) and the `entities` you give it, which, as a function
     of the subject, are read again on every check. Casbin evaluates the policy its enforcer holds
     in memory: a change made through the enforcer (`remove_policy`, say) applies at once, and one
     written to the adapter's storage by another process only once that enforcer reloads it
     (`load_policy()`, or a Casbin watcher).
3. **`CachingAuthorizer`.** A cached decision is used for at most `ttl` seconds. `invalidate()`
   drops decisions at once in its own process, including answers that were still being fetched, and
   reaches other replicas sharing a Redis within `local_ttl`. A service that is itself behind (layer
   2) may still answer from before the change just after `invalidate()`; for `source_staleness`
   seconds (the authorizer's declared `max_staleness`) the cache passes such answers on without
   keeping them, so it never holds one for `ttl`. See [permission caching](operations.md#permission-caching).
4. **The store's visibility delay**, when permissions live on chunks: some stores show an update
   to searches only after a refresh (Elasticsearch and OpenSearch refresh every second by default).
5. **Requests already running.** A retrieval that started before the revocation may finish with the
   decision it already had. wardowl never caches such an answer after `invalidate()`, but it
   cannot recall a result already returned, or what a model has already been given.

## Bounded staleness

The usual choice: answers are cached, and the longest a revoked permission keeps granting access is
known and chosen.

```python
authorizer = CachingAuthorizer(
    OpenFGAAuthorizer(FGA_URL, STORE_ID),
    ttl=30,
    shared=RedisCache(redis_client, signing_key=CACHE_SIGNING_KEY),
    namespace="openfga-docs",  # one per authorizer configuration
    local_ttl=1,
)
```

- **Revocations nobody reports** (a change made directly in the permission service): the delay is at
  most the service's own staleness plus `ttl`.
- **Revocations you report** by calling `authorizer.invalidate(user_id)` after the change (in
  async code, `await authorizer.ainvalidate(user_id)`, which does the same without blocking the
  event loop): no retrieval that starts in that process afterwards uses the old decision, and
  other replicas stop within `local_ttl`. The tests revoke a permission under concurrent retrievals, with one and three
  replicas, and check exactly this. With a service that is itself behind by up to S seconds, its
  own answers can still predate the change for S seconds, cache or not; the cache adds nothing to
  that when `source_staleness` is at least S. The bound after `invalidate()` is then
  `source_staleness` in that process and `source_staleness` plus `local_ttl` in the others. If
  the service is staler than `source_staleness`, an answer it gives after the hold is cached for
  up to `ttl`, so set it from what the service documents, with a margin.

`source_staleness` comes from the authorizer's `max_staleness` when it declares one:
`SpiceDBAuthorizer` does, from its consistency (11 seconds, or 0 with `fullyConsistent`). For a
service whose delay the adapter cannot know, pass it yourself: OpenFGA's check cache TTL under
`MINIMIZE_LATENCY` (10 seconds when the cache is on), OPA's bundle `max_delay_seconds`, Cerbos's
store poll interval or its reload delay for a watched directory.

```python
authorizer = CachingAuthorizer(CerbosAuthorizer(CERBOS_URL), ttl=30, source_staleness=10)
```

Choose `ttl` as a security decision. 30 seconds removes most repeated calls within a conversation;
a few seconds still removes many.

## Strict revocation

For access that must stop at once, such as when an account is disabled, make every layer current
and put the revocation in the request path of the change itself:

- **Ask the service for current answers:** `OpenFGAAuthorizer(..., consistency="HIGHER_CONSISTENCY")`,
  or `SpiceDBAuthorizer(..., consistency={"fullyConsistent": True})`, or pass SpiceDB the zedtoken
  of your write with `atLeastAsFresh`. Keto needs no setting. With Casbin, make the change through
  the enforcer that serves retrievals, or reload it before you report success; with Cedar, replace
  the authorizer, or change what its `entities` function returns.
- **Do not cache, or invalidate before you report success.** Either leave `CachingAuthorizer` out,
  or call `invalidate(user_id)` in the same code that applies the revocation, and only then tell the
  administrator it is done. `invalidate()` raises if it cannot reach Redis. It also waits for Redis
  in the calling thread, so in an `async def` handler `await ainvalidate(user_id)` instead, which
  waits in a worker thread and raises the same errors; if that task is cancelled, call it again
  before reporting success. With `RedisCache`, `invalidate(user_id)` costs two round trips plus
  one per 500 entries its index lists, while `invalidate()` of everyone scans the whole Redis
  database ([what each costs](operations.md#sharing-the-cache-across-processes)).
- **For permissions on chunks,** call `set_metadata` in that same code, and allow for the store's
  visibility delay.
- **Keep `recheck=True`** (the default), so every pre-filtered hit is decided by the authorizer as
  well as by the filter.

The price is a call to the permission service for every retrieval, with its latency and load; the
[benchmarks](benchmarks.md) show what an OPA call and a cache cost under load. Even so, a retrieval
already running when the revocation happens can complete with its earlier answer: strict means no
request that starts afterwards sees the document, not that one in flight is recalled.

## Checking it

`RetrievalEvent.started_at` and the returned chunk ids, in your audit log, show whether a revoked
document was returned after a given time. To measure your own deployment's delay, revoke a test
permission while traffic runs and find the last retrieval that returned the document, as
`tests/test_revocation.py` does.

# Production checklist

What to settle before wardowl serves real users, and what happens when each part of the system
fails. Each item links to the page with the details.

## Before launch

**Identity**

- [ ] The `Subject` is built from your authenticated session: the user id and groups your identity
  provider vouches for, never values from the request body, and never a tool argument a language
  model fills in. ([Security model](security.md#the-subject-must-come-from-authentication))
- [ ] Subject ids and group names are written exactly as the chunks' permissions hold them.
  wardowl compares them exactly and never lowercases, trims or normalises them, in the
  `Subject`, in `OIDCAuthenticator` or in the filters. The Google Drive connector writes addresses
  lowercased, so lowercase the id and the groups where the subject is built; an id or group that
  differs in case does not match. ([Google Drive](integrations/google-drive.md#how-drive-sharing-maps-onto-subjects))
- [ ] Only groups that grant access to documents are on the subject. A subject in tens of thousands
  of groups works, but costs on every query. ([Operations](operations.md#subjects-in-very-many-groups))
- [ ] In a multi-tenant deployment, every subject carries its tenant, and the authorizer is wrapped
  in `AllOf(TenantAuthorizer(), ...)`. A subject without a tenant then gets nothing.
  ([Combining authorizers](combining.md))

**Permissions on the chunks**

- [ ] Permission metadata is copied onto every chunk at ingestion, and re-copied when a document's
  sharing changes. How soon that happens is your revocation delay for those chunks.
  ([Security model](security.md#what-it-relies-on))
- [ ] The permission fields are indexed in your vector store: `create_schema` (pgvector),
  `create_collection` (Qdrant, Milvus) and `create_index` (Elasticsearch, OpenSearch) create the
  indexes; Chroma needs no setup. ([Integrations](integrations/index.md))
- [ ] Your own backend or authorizer, if you wrote one, passes the conformance kit.
  ([Extending wardowl](extending.md#test-it-with-the-conformance-kit))

**Retrieval settings**

- [ ] `recheck=True` (the default) stays on. It also catches a filter that is broader than the
  authorizer's own check; with `recheck=False` only a backend that ignores or mistranslates the
  filter is caught.
- [ ] `strategy="auto"` (the default) or `"prefilter"` when the authorizer can return a filter.
  Post-filtering alone loses recall for users who may read little of the index.
  ([Benchmarks](benchmarks.md#what-the-load-results-show))
- [ ] `max_candidates` is sized for your least-privileged users, and `complete=False` is monitored.
- [ ] A permission change at the source reaches the chunks' metadata (`set_metadata`) within your
  revocation target, and you know each source document's chunk ids.
  ([Operations](operations.md#changing-a-documents-permissions))
- [ ] The writes for each source document (re-indexing and permission sync) happen one after
  another, so a late write from data read before a revocation cannot undo it; on pgvector and
  MongoDB, every writer also passes the document's `revision`.
  ([Operations](operations.md#one-writer-at-a-time-for-each-document))

**The permission service**

- [ ] HTTP authorizers have a `timeout` and `retries` you have chosen, and a shared
  `CircuitBreaker` so a failing service is not flooded.
  ([Operations](operations.md#resilience-of-remote-permission-services))
- [ ] The HTTP service and the MCP tool have a `retrieval_timeout`, and the HTTP service's thread
  pool is sized with `max_workers` for the concurrency you expect. Every store and permission
  service still has its own client timeouts, which free the threads that `retrieval_timeout`
  cannot stop. ([HTTP service](integrations/http.md#time-bounds-and-threads))
- [ ] Connections use TLS and credentials (`headers=`, or `token=` for SpiceDB).
- [ ] `CachingAuthorizer` sits in front, with a `ttl` chosen as a security decision: it is the
  longest a revoked permission keeps granting access. ([Operations](operations.md#permission-caching))
- [ ] With several replicas, the shared cache has a `signing_key` and a Redis reachable only by the
  application. ([Security model](security.md#what-it-relies-on))

**Observability**

- [ ] Audit events go to durable storage (`JsonLinesAudit` or your own observer), with an
  `audit_key` from your secret store if you want query fingerprints. ([Operations](operations.md#audit-events))
- [ ] You have decided what a lost audit event means. Among `observers`, delivery is best-effort
  and a failure is counted in `wardowl.observer_failures`; an audit log that a control depends on
  goes in `required_observers`, and then a retrieval it cannot record returns nothing.
  ([Operations](operations.md#when-an-observer-fails))
- [ ] `OpenTelemetryMetrics` and `OpenTelemetryObserver` are installed, and the alerts in
  [What to alert on](operations.md#what-to-alert-on) are set up, starting with any increase in
  `wardowl.violations`.
- [ ] `examined`, `denied` and timings stay in logs and metrics and are never shown to end users;
  they can hint that forbidden documents exist. ([Security model](security.md#what-it-does-not-do))

**Supply chain and capacity**

- [ ] The version is pinned, and the release's signatures are verified as
  [SECURITY.md](https://github.com/muzakkirhussain011/wardowl/blob/main/SECURITY.md#verifying-a-release)
  describes.
- [ ] Capacity is measured on your own data with the load harness in `benchmarks/`
  ([Benchmarks](benchmarks.md#reproduce-it)). In ours, over a million chunks, one 4-CPU machine
  shared by PostgreSQL and the load generator peaked at 131 requests a second.

## When something fails

wardowl fails closed: whenever it cannot be sure what a subject may read, it returns nothing and
says why. The table shows what each failure looks like.

| What fails | What wardowl does | What you see |
|---|---|---|
| The permission service is down, slow or erroring | Raises `AuthorizationError` after the timeout and retries, or when an authorizer's `total_timeout` for the whole check runs out; with a circuit breaker open, at once | `wardowl.retrievals` with `error.type = AuthorizationError`; no results reach the model |
| The permission service gives a malformed answer (wrong count, non-booleans, not a filter) | Raises `AuthorizationError`; never caches the answer | As above |
| The vector store is down, or a search or count fails | Raises `SearchError`, with the store client's exception as its `__cause__`; nothing is returned | `wardowl.retrievals` with `error.type = SearchError`; the span's `wardowl.error.cause` and `RetrievalEvent.cause` name the client's exception |
| The vector store ignores or mistranslates the filter | The re-check drops every chunk outside it | `violations > 0` on the result, the event and the `wardowl.violations` metric |
| The store matches a chunk the stored metadata denies: permission terms another tool left stale, or MongoDB's index catching up after a write | The re-check drops it | `violations > 0`, as above; the [runbook](runbook.md#the-violations-count-rose) tells these causes apart |
| The authorizer's filter selects more than its `check` allows, or a permission changes between the two | The re-check drops what `check` denies | `violations > 0`, as above |
| Post-filtering cannot find `k` readable chunks within `max_candidates` | Returns what it found | `complete = False`; `wardowl.outcome = incomplete` |
| An approximate index returns fewer than `k` chunks under the filter (it may have stopped early) | Returns what it found | `complete = False`; `wardowl.outcome = incomplete` |
| A subject has no tenant under `TenantAuthorizer` | Raises `AuthorizationError` | As for a failing authorizer |
| `OIDCAuthenticator` refuses a token: expired, not yet valid by the service's clock (a provider's clock ahead), or failing another check | Answers 401, the same whatever the reason | An INFO line on the `wardowl` logger naming the check, such as `ImmatureSignatureError` ([HTTP service](integrations/http.md#why-a-token-was-refused)) |
| A token carries no groups claim | Without `require_groups`, a subject with no groups: 200 with fewer hits; with it, 401 | A DEBUG line on the `wardowl` logger, or the 401's INFO line |
| The shared cache (Redis) is down or slow | Treats it as a miss and asks the permission service | A warning on the `wardowl` logger; higher `wardowl.authorize.duration` |
| A shared-cache entry is unsigned, tampered with or copied under another key | Ignores it (with `signing_key`) | Nothing: it is a miss |
| `invalidate()` cannot reach Redis | Raises, because the caller asked for a revocation now | The exception at the caller |
| An observer raises | Logs and counts it; retrieval is unaffected | An error on the `wardowl` logger; `observer_failures` and `wardowl.observer_failures` rise |
| A required observer raises (`required_observers`) | Raises `AuditError`; nothing is returned | As above, and `wardowl.retrievals` with `error.type = AuditError` |
| An observer blocks (a full disk, a collector that stopped reading) | Under `aretrieve`, waits for it in a worker thread: that request is delayed, the others are not | Slower requests; with `retrieval_timeout`, 503s or tool errors |
| A retrieval in the HTTP service or the MCP tool takes longer than `retrieval_timeout` | Cancels it and returns nothing: 503, or a tool error | An error on the `wardowl` logger; `error.type = CancelledError` |
| A recall sample's count or exact search fails | Logs it; returns the retrieval's hits | `recall_sample = failed`; `wardowl.recall_samples` with that value |
| Redis answers in RESP2 and its `search-on-timeout` policy is not `fail` | Raises, since a timed-out search would look complete | `SearchError`, with the reason in its `__cause__` |

What to do about each, containment first, is in the [incident runbook](runbook.md).

What wardowl does not protect against is listed in the
[security model](security.md#what-it-does-not-do): output control, prompt injection inside readable
chunks, and the side channels above.

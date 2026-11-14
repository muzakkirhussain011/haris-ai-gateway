---
description: Permission-aware retrieval returns the best matches among the chunks the asking user may read. This page explains how wardowl does it.
---

# Concepts

Permission-aware retrieval returns the best matches among the chunks the asking user may read, not
among all chunks. wardowl has four parts: the **subject** who asks, the **backend** that searches,
the **authorizer** that decides, and the **retriever** that combines them and enforces the
guarantees.

## Subjects

A `Subject` is who is asking: a user id, the groups they belong to, and any attributes your
policy needs.

```python
from wardowl import Subject

alice = Subject("alice", groups=["hr", "managers"], attributes={"region": "emea"})
```

The id must not be empty, and `groups` must be a collection: `Subject("bob", "eng")` raises
instead of quietly becoming the groups `e`, `n` and `g`. Subjects are immutable and hashable,
which is what lets [`CachingAuthorizer`](operations.md#permission-caching) key its cache by
them.

## Documents and hits

A `Document` is a chunk as stored: an id, its text and its metadata. A `Hit` is a search result:
the same fields plus the backend's relevance score, where higher is better. Permissions usually
live in the metadata, for example `{"allowed_groups": ["hr"]}` or `{"doc_id": "handbook-2026"}`
for a permission system that decides per source document.

## Authorizers

An authorizer answers two questions:

`check(subject, hits, action) -> list[bool]`
: May the subject see each of these hits? One boolean per hit, in order. Every authorizer must
  answer this.

`filter_for(subject, action) -> Filter | None`
: A filter that selects exactly what the subject may see, or `None` when the rules cannot be
  written as one. When it returns a filter, the vector store can filter inside the search.

Any doubt must raise. The retriever turns every exception from an authorizer into an
`AuthorizationError` and returns nothing.

| Authorizer | Decides with | Pre-filters |
|---|---|---|
| `MetadataACLAuthorizer` | `allowed_users` and `allowed_groups` on each chunk (`"*"` for everyone) | Yes |
| [`TenantAuthorizer`](combining.md#tenant-isolation) | The chunk's `tenant` field against the subject's `tenant` attribute | Yes |
| `FunctionAuthorizer` | Your function, optionally with your own `filter_for` | If you give one |
| [`AllOf`, `AnyOf`](combining.md#allof-and-anyof) | Several authorizers: all of them, or any one | When every part has a filter; otherwise a [scope](combining.md#scoped-post-filtering), where the parts allow one |
| [`OPAAuthorizer`](integrations/opa.md) | A Rego policy in Open Policy Agent | If the policy returns a filter |
| [`OpenFGAAuthorizer`](integrations/openfga.md) | Relationship tuples in OpenFGA | Yes, through ListObjects |
| [`SpiceDBAuthorizer`](integrations/spicedb.md) | Relationships in SpiceDB | Yes, through LookupResources |
| [`CerbosAuthorizer`](integrations/cerbos.md) | Cerbos policies | Yes, from query plans |
| [`KetoAuthorizer`](integrations/keto.md) | Relationship tuples in Ory Keto | No |
| [`CedarAuthorizer`](integrations/cedar.md) | Cedar policies, in your process | No |
| [`CasbinAuthorizer`](integrations/casbin.md) | A Casbin enforcer, in your process | No |
| [`CachingAuthorizer`](operations.md#permission-caching) | Any of the above, cached for a bounded time | As the wrapped one |

## Filters

Filters are a small language every backend can translate: `In`, `And` and `Or`.

```python
from wardowl import And, In, Or

f = Or(
    In("allowed_users", ["alice"]),
    And(In("allowed_groups", ["hr", "*"]), In("region", ["emea"])),
)
```

`In(field, values)` is true when the metadata field equals one of the values, or, for a list
field such as `allowed_groups: ["sales", "hr"]`, when the two share an element. The rules are
strict on purpose, because a permission filter that is too generous is a leak:

- A missing field never matches. Missing permissions mean no access.
- Values are strings or integers only. Metadata values of other types (booleans, floats, nested
  objects) never match. Python treats `True == 1`, JSON stores compare by type, and keeping to two
  types gives every backend the same answer.
- `And()` and `Or()` with no clauses are rejected. An empty `And` would be true for everything.
- `In(field, [])` matches nothing.

`matches(f, metadata)` is the reference semantics. Every backend's translation is tested against
it. `filter_to_dict` and `filter_from_dict` convert filters to and from JSON, which is how
`OPAAuthorizer` receives them from a policy.

## Strategies

`PermissionedRetriever(backend, authorizer, strategy=...)` decides where permissions are applied.

| Strategy | When it is used | Cost | Result |
|---|---|---|---|
| `prefilter` | The authorizer returns a filter and the backend supports filters | One search | The exact top-k of what the subject may read, as far as the index is exact |
| `postfilter` | Any authorizer and backend | `k × overfetch` candidates, widening up to `max_candidates` | Exact when enough permitted candidates are found; `complete=False` otherwise |
| `auto` (default) | `prefilter` when both sides support it, else `postfilter` | | |

**Pre-filtering** puts the filter inside the search. The vector store only ranks chunks the subject
may read, so a subject who may read 0.1% of the corpus still gets ten good results, not the
permitted few among the global top ten.

**Post-filtering** checks every candidate with `check`. It starts with `k × overfetch` candidates
(default 4), and while fewer than k are permitted it multiplies the fetch by `overfetch` and
searches again, until k are found, the backend runs out, or `max_candidates` (default 1000) is
reached. The last case is reported as `complete=False`, so you
know the result may be missing permitted chunks rather than finding out from users. So is running
out inside a scope on an approximate index, which may have stopped early
([What a result tells you](#what-a-result-tells-you)).
`max_candidates` bounds every strategy: a `k` above it raises `ValueError`, a reranker's depth is
capped at it, and post-filtering checks no more candidates than it asked the backend for, even if
the backend returns more. Subjects who
may read only a small share of the corpus are where post-filtering struggles; the
[benchmarks](benchmarks.md) show by how much.

Choose `prefilter` explicitly when you want an error rather than a silent fallback if a filter is
not available.

### What the store receives

| Strategy | The filter in each search |
|---|---|
| `prefilter` | `filter_for`: exactly what the subject may read |
| `postfilter` | `scope_for`, when the authorizer has it and the backend supports filters; otherwise none |
| `auto` | As `prefilter` when the backend supports filters and `filter_for` returns a filter; otherwise as `postfilter` |

A scope is a filter every readable chunk matches, such as the subject's tenant
([Scoped post-filtering](combining.md#scoped-post-filtering)). `AllOf` and `AnyOf` have
`scope_for`, and `CachingAuthorizer` passes on the scope of the authorizer it wraps. The other
built-in authorizers have none, so with them, or with a backend whose `supports_filters` is false,
post-filtering searches without a filter.

A scope can be the whole permission filter. `AllOf`'s scope joins each part's filter, or its scope
when it has none, so when every part has a filter, as in
`AllOf(TenantAuthorizer(), MetadataACLAuthorizer())`, the scope is the same filter pre-filtering
would send. The retriever asks for it even with `strategy="postfilter"`: the store then filters
inside the search as it would for pre-filtering, and the retriever over-fetches and checks each
candidate as well. `strategy="postfilter"` does not mean an unfiltered search.

## What a result tells you

| Field | Meaning |
|---|---|
| `hits` | Permitted chunks, best first, at most `k` |
| `strategy` | The strategy that actually ran |
| `complete` | `False` when wardowl knows results may be missing (see below) |
| `examined` | Candidates the authorizer checked |
| `denied` | Post-filtered candidates the subject may not read |
| `violations` | Pre-filtered hits the re-check rejected |
| `shortfall` | Why a pre-filtered search returned fewer than `k` hits: `exhausted`, `missed` or `unknown` ([Short results](#short-results)); `None` when it did not |
| `fallback` | What `short_results` did about it: `exact`, `widened`, `over_limit` or `unsupported`; `None` when it did nothing |
| `sampled_recall` | For a retrieval `recall_sample_rate` sampled, the share of an exact search's top `k` that came back |
| `recall_sample` | What happened to that sample: `measured`, `over_limit`, `unsupported` or `failed`; `None` when the retrieval was not sampled |

`violations` should always be 0. Anything else means the backend returned chunks outside the
filter, and deserves an alert: the re-check removed them, but the backend has a bug.

`complete=False` means wardowl knows results may be missing. That is the case when:

- the re-check removed pre-filtered hits;
- post-filtering stopped at `max_candidates` before it found k permitted hits;
- a search under a filter returned fewer hits than it asked for, and the backend's search is
  approximate. This covers a pre-filtered result with fewer than k hits, an empty one included,
  and a scoped post-filtered search whose candidates ran out before k readable chunks were found.

A short result is reported as complete only when it proves that nothing else matches: the backend
declares `exact_search` (as `InMemoryBackend` does, `FaissBackend` with a flat index, and
`QdrantBackend` with `SearchParams(exact=True)`), a count shows that no more chunks match
([Short results](#short-results)), or post-filtering searched without a filter, where a short
candidate list means the store ran out.
An approximate index (HNSW and similar) can stop early under a selective filter and return fewer
hits, or none, though the user may read more; wardowl cannot tell that apart from "the user may read
fewer than k chunks", so it reports `complete=False`. pgvector's iterative index scans, for
example, keep going only until they have visited `hnsw.max_scan_tuples` graph elements (20,000 by
default) or used `work_mem` × `hnsw.scan_mem_multiplier` of memory (4 MB with PostgreSQL's default
`work_mem`). A user who may read few chunks, none of them near the question, can then get no rows:
in a large acceptance run on Kaggle, that happened to least-privileged users who could read
hundreds of chunks ([pgvector](integrations/pgvector.md#iterative-index-scans)).

`complete=True` with k hits trusts the backend's ranking. An approximate index can return k hits
that are not the true top k, and `complete` cannot see that. How much each backend misses is
measured in the [benchmarks](benchmarks.md); for pgvector, keep iterative index scans on (the
default), and raise their limits for users who may read little. `recall_sample_rate` measures it
in production ([Short results](#short-results)).

## Short results

By default a short pre-filtered result from an approximate index says `complete=False` and
`shortfall="unknown"`: the index may have stopped early, or the user may read fewer than k
matching chunks. `short_results` tells the two apart, and can repair the first:

```python
retriever = PermissionedRetriever(backend, authorizer, short_results="exact", max_exact_rows=10_000)
```

| `short_results=` | After a short pre-filtered result from an approximate index |
|---|---|
| `"report"` (default) | Nothing more runs: `complete=False`, `shortfall="unknown"` |
| `"count"` | The backend counts the chunks the filter matches. No more than came back: `shortfall="exhausted"` and `complete=True`. More: `shortfall="missed"` and `complete=False` |
| `"exact"` | Counts; after a miss, searches again exactly under the same filter, if the count is at most `max_exact_rows` (10,000 by default; `None` for no bound). `fallback="exact"`, and the result is complete |
| `"widen"` | Counts; after a miss, searches again with `wider`, the same store with larger search settings, and keeps the best hits of both searches. `fallback="widened"`; complete only if it found as many as were counted |

The count is cheap: a filter-only query that stops at its limit (pgvector), or the payload index's
count (Qdrant). A count above the hits proves the index missed readable chunks; a count no larger
than the hits proves there are no more. Under-retrieval mostly happens when the readable set is
small, which is when an exact search is cheap too: in wardowl's acceptance run on Kaggle, the users
pgvector returned nothing to could read about 600 chunks. `max_exact_rows` keeps a large readable
set from setting off a large scan; above it, the first hits stay, with `fallback="over_limit"`.
`"widen"` costs one more approximate search, which can still miss chunks; it is there to compare
with `"exact"`, and for stores whose exact search is too slow.

The guarantees hold on every path. The search run again uses the filter the authorizer gave, and
its hits are checked again with `check`, together with the first search's hits that it did not
return, so a backend that ignored the filter either time is counted in `violations`. An error in
the count or in the search fails the retrieval, as any backend error does. `RetrievalEvent` and
the [OpenTelemetry hooks](operations.md#audit-events) report the shortfall, the fallback and the
time they took (`shortfall_ms`).

A backend takes part by offering `count(filter, limit)` and, for `"exact"`, an exact search on
request (`supports_exact`; see [Extending](extending.md#a-backend)). `PgVectorBackend` and
`QdrantBackend` do, in vector mode. With a backend that offers neither, as with `RankFusion`,
`"count"`, `"exact"` and `"widen"` report as `"report"` does, and `fallback="unsupported"` says
why. A backend whose `exact_search` is True never needs them: its short results are
`shortfall="exhausted"` already.

`recall_sample_rate` (0 by default) watches what `complete` cannot see. For that share of
pre-filtered retrievals on a backend that can search exactly, the retriever runs the exact search
too, after the first, and reports in `sampled_recall` how much of its top k came back. The exact
hits are never returned, and only those the filter matches are counted. A rate of 0.01 costs one
more search in a hundred; the `wardowl.sampled_recall` histogram then shows how far the index's
answers are from the true top k for your users and filters.

The exact search is bounded the way `short_results="exact"` is. With `max_exact_rows` set (10,000
by default), the backend first counts the chunks the filter matches, and the exact search runs
only when there are no more than that: a subject who may read most of a large store does not set
off a scan of it on every sampled retrieval (`recall_sample="over_limit"`), and a backend that
cannot count is not sampled (`"unsupported"`). The measure then covers only retrievals whose
readable set is within the bound; with `max_exact_rows=None`, every sampled retrieval runs its
exact search, bounded only by the store's own timeout. The sample is monitoring and
never fails the retrieval: if its count or exact search raises, a store timeout for one, the
error is logged on the `wardowl` logger, `recall_sample` is `"failed"`, and the retrieval returns
the hits it found. `wardowl.recall_samples` counts samples by what happened to them.

OpenSearch takes a similar approach inside the engine: since 3.5, when an approximate filtered
k-NN search returns fewer than k results, it falls back to an exact search. wardowl does it above
the store, for stores that do not, with the permission check applied to whatever comes back.

## The guarantees

**Fail closed.** If the authorizer raises, times out, returns the wrong number of decisions, or
returns anything that is not a real `bool` (`"no"`, `1`, `None`), the call raises
`AuthorizationError`. There is no fallback to unfiltered results.

**Defence in depth.** With `recheck=True` (the default), pre-filtered hits are checked again with
`authorizer.check`. A backend that ignores or mistranslates the filter cannot leak; the rejected
hits are dropped and counted in `violations`. With `recheck=False` the authorizer is not asked
again, but each pre-filtered hit is still matched against the filter in the process: a backend
that ignores or mistranslates the filter still cannot leak, though a filter broader than the
authorizer's own check would go unnoticed.

**One algorithm, two drivers.** `retrieve` and `aretrieve` run the same plan, written once, so the
async path cannot drift from the sync one. Backends and authorizers may provide `asearch`,
`afilter_for` and `acheck`; otherwise their sync methods run in a thread.

These guarantees are tested as properties with [Hypothesis](https://hypothesis.readthedocs.io):
for random corpora, permissions, subjects and queries, no strategy may return a chunk the subject
may not read, results must equal an independent oracle's top-k, and a backend that ignores the
filter must still not leak.

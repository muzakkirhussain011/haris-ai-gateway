# SpiceDB

Relationship-based access control in the style of Google Zanzibar, through SpiceDB's HTTP API. No
extra dependency: requests use the standard library. Start SpiceDB with its HTTP gateway:

```bash
spicedb serve --grpc-preshared-key "$SPICEDB_KEY" --http-enabled   # HTTP on :8443
```

```python
from wardowl.authorizers.spicedb import SpiceDBAuthorizer

authorizer = SpiceDBAuthorizer("http://localhost:8443", token=SPICEDB_KEY, resource_field="doc_id")
```

The token is a bearer token, so it is refused with `ValueError` over `http://` to any host but this
machine (`localhost`, `127.0.0.1`, `::1`, with no proxy in the way): serve SpiceDB's HTTP gateway
with TLS (`--http-tls-cert-path` and `--http-tls-key-path`) and use an `https://` url, or pass
`allow_insecure=True` on a network you trust. `ssl_context=`, for your own CA or mutual TLS, is
refused with `http://`, where it would not be used.

## The schema

The authorizer asks whether `user:<subject id>` has a permission (by default `view` for the action
`read`) on `document:<resource id>`. A schema such as this one fits:

```text
definition user {}

definition group {
    relation member: user
}

definition document {
    relation viewer: user | user:* | group#member
    permission view = viewer
}
```

Chunks of one source document share its id in metadata (`doc_id` above), so a relationship is
written once per document, not once per chunk. With `resource_field=None` the chunk id is the
resource. A chunk without its resource id, or whose field holds anything but one string (a list,
a number), is denied. Change the names with `resource_type=`, `subject_type=`,
`permissions={"read": "view"}`, `group_type=` and `group_relation=`. Subject ids and group names
are sent as separate fields of SpiceDB's JSON, never inside a `type:id#relation` string, so a `:`
or `#` in one cannot change its type or relation.

SpiceDB object ids are 1 to 1024 letters, digits and `/_|-=+` (its API's rule; `*` alone is the
wildcard, which only subjects use). Store such an id in the chunk's `resource_field` and use such
subject ids (for example a stable user id rather than an email address). A chunk whose resource
id breaks the rule is denied without being sent, since SpiceDB would refuse the whole request and
every retrieval that found the chunk would return nothing. A subject id that breaks the rule, or
with `group_subjects=True` a group name, raises `UnsupportedSubject` before anything is sent, since
SpiceDB would refuse every request naming it. `UnsupportedSubject` is an `AuthorizationError`, so
retrieval fails closed, and a `ValueError`, since the subject is at fault. With the default
`group_subjects=False`, group names are never sent and need not follow the rule.

## Groups

By default only the user is checked (`group_subjects=False`), and SpiceDB follows the group
memberships it stores: write `group:eng#member@user:ann` relationships, and a document shared with
`group:eng#member` is readable by ann. SpiceDB then applies every part of the permission to ann
herself, exclusions and intersections included.

When memberships live only in your identity provider, `group_subjects=True` also checks each of the
subject's groups as a subject set, `group:<name>#member`, and allows a chunk if the user or any of
the groups has the permission. A document shared with `group:eng#member` is then readable by anyone
whose `Subject` lists `eng`, without syncing memberships into SpiceDB. The groups are trusted as
given, so they must come from the verified token.

**Do not use `group_subjects=True` with a permission that excludes or intersects.** Each group is
asked about on its own, as if it were the reader:

- With an exclusion, such as `permission view = viewer - banned`, a banned user still reads a
  document shared with their group, because the group is not banned.
- With an intersection, such as `permission view = viewer & org->member`, a grant through a group
  can be refused, because the group is not itself a member of the organisation.

For example, a document readable only inside its tenant:

```text
definition organization {
    relation member: user
}

definition document {
    relation tenant: organization
    relation viewer: user | group#member
    permission view = viewer & tenant->member
}
```

With `group_subjects=True` and memberships only in the token, `group:eng#member` is a viewer but not
a member of the organization, which lists users, so it lacks `view`; and `user:alice` is not a
viewer, since her membership of `eng` is not in SpiceDB. Every document granted to a group is then
denied.

With such a schema, either store the memberships in SpiceDB and keep the default,
`group_subjects=False`, so that SpiceDB evaluates the whole permission for the user; or keep the
permission wardowl asks about a plain union, as in [the schema above](#the-schema), and apply the
other conditions outside SpiceDB, such as the tenant with
`AllOf(TenantAuthorizer(), SpiceDBAuthorizer(...))` ([Combining](../combining.md#tenant-isolation)).
CI checks, against a real SpiceDB, that a banned member of a permitted group is denied by default.

## Checks

`check` sends CheckBulkPermissions requests of `batch_size` questions (100 by default): one per
distinct resource and subject. Answers are matched to questions by the request SpiceDB echoes
with each one, not by position. A conditional answer (a caveat whose context was not supplied) is
not a decision and raises `AuthorizationError`; supply caveat context with
`caveat_context=lambda subject: {...}`.

The requests are sent one after another. A check of r distinct resources for s subjects (the user,
plus each group with `group_subjects=True`) sends r × s / `batch_size` requests, rounded up, and
`filter_for` sends one LookupResources request per subject. `timeout=` bounds each request.
`total_timeout=` (seconds; no bound by default) bounds all the requests of one `check` or
`filter_for` together: once it runs out, the request in flight is abandoned, nothing more is sent,
and the call raises `AuthorizationError` caused by a `TimeoutError`.

## Pre-filtering

`filter_for` calls LookupResources for the user (and, with `group_subjects=True`, for each group
subject), and turns the union of
the documents found into `In("doc_id", ...)`, so the vector store filters inside the search. If a
lookup returns `lookup_limit` documents (1000 by default) the list may be truncated, so no filter
is returned and the retriever post-filters instead; it does the same when a lookup result is
conditional on a caveat. Subjects who may read very many documents are therefore post-filtered,
which stays correct.

A lookup that finds no document gives no filter either. LookupResources answers as a stream of
lines with no mark at its end, so a stream that a proxy, a load balancer or a server fault ended
before its first line looks exactly like a subject who may read nothing, and trusting it would
return nothing without an error. With no filter, the retriever post-filters, and
CheckBulkPermissions decides each candidate, or fails closed if it is broken too. The cost falls on
subjects who truly read nothing: a post-filtered search, checking up to `max_candidates`
candidates, where an empty filter cost one search. Under `strategy="prefilter"` the retriever has no
filter to apply for them and raises `ValueError`, as it does for a truncated lookup; pass
`trust_empty_lookup=True` to take an empty lookup as "nothing is readable", as earlier versions did,
which can lose results but never return one the subject may not read.

`In` matches a list field when any of its elements is listed, while `check` denies a chunk whose
resource field is not one string. The retriever's re-check of pre-filtered hits (`recheck=True`,
the default) drops such a chunk and counts it in `violations`. With `recheck=False` the filter
alone decides, so the resource field must be one string on every chunk.

## Consistency and revocation

The default consistency, `{"minimizeLatency": True}`, lets SpiceDB answer from a recent snapshot.
A revoked permission can then keep granting access for a while. SpiceDB rounds the revision such a
read uses down to `--datastore-revision-quantization-interval` (5 seconds by default) and may pick
one older still by `--datastore-revision-quantization-max-staleness-percent` of it (10% by
default), which its documentation gives as the bound: 5.5 seconds with the defaults, plus
`--datastore-follower-read-delay-duration` when it reads from replicas
([`spicedb serve` flags](https://authzed.com/docs/spicedb/concepts/commands)). Measured against
SpiceDB 1.56.2 on Postgres with those defaults, revocations took a median 3.3 seconds and up to
8.7, and in some a revoked document came back after it had gone, since successive answers can use
an older revision than the one before.

`SpiceDBAuthorizer` declares this as `max_staleness`: 11 seconds, twice the documented bound, for
any consistency but `fullyConsistent`, which declares 0. Pass `max_staleness=` for a server with
other settings (twice the interval times one plus the percentage, plus any follower read delay), or
0 if you send `atLeastAsFresh` with the token of every change you report to the cache.

For fresher answers pass `consistency={"fullyConsistent": True}`, or `{"atLeastAsFresh": {"token":
zedtoken}}` with the ZedToken from your last write. Combined with `CachingAuthorizer`, the
revocation bound is the cache's `ttl` plus the staleness. After `invalidate()`, the cache reads
`max_staleness` and, for that long, keeps no answer for the subject, so one from the stale snapshot
is not held for `ttl`; the bound is then the staleness alone, plus `local_ttl` on other replicas
([Revocation and consistency](../revocation.md#bounded-staleness)).

## Failures

Every failure raises `AuthorizationError`, so retrieval returns nothing: an unreachable server, a
timeout (`timeout=`, 2 seconds by default, for each request except the DNS lookup of the host, as
[for OPA](opa.md#failing-closed); `total_timeout=` for a whole call, as [above](#checks)), an HTTP
error, a malformed answer or one larger than
`max_response_bytes=` (10 MiB by default), a per-item error, or a missing answer. Connections are
reused, transient failures are retried once, and a `CircuitBreaker` can pause requests to a failing
server (see [Operations](../operations.md#resilience-of-remote-permission-services)).

CI checks the authorizer against a real SpiceDB (v1.56.2): for random relationship graphs, every
strategy returns exactly the reference answer, and `check` and `filter_for` pass the
[conformance kit](../extending.md#test-it-with-the-conformance-kit).

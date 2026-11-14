# Incident runbook

What to do when something goes wrong: for each situation, what you see, what it means, and the
steps, containment first. [What to alert on](operations.md#what-to-alert-on) lists the signals, and
[When something fails](production.md#when-something-fails) what each failure looks like.

## Be ready before you need it

- **Keep the audit events** for as long as you may need to look back. Each `RetrievalEvent` names the
  subject and the chunks returned, with the time, never the query or the text, so it is safe to keep
  ([Audit events](operations.md#audit-events)).
- **Log what each subject was built from**: the user, groups and tenant from the verified token, with
  the request. The event has the subject's id only, and an investigation needs the rest.
- **Be able to call `invalidate()` in production**, for example from your admin service or from the
  code that changes permissions. With a shared cache one call reaches every replica; without one,
  every process needs the call.
- **Keep chunk ids traceable** to their source document (for example `"handbook#12"`), so you can find
  every chunk of a document whose sharing was wrong.

## Someone saw a document they should not have

**Contain first.**

1. Remove the access at its source: in the permission service, or on the chunks with
   `set_metadata` ([Changing a document's permissions](operations.md#changing-a-documents-permissions)).
2. Call `authorizer.invalidate(user_id)` on the `CachingAuthorizer`, or `invalidate()` for every
   subject if you cannot tell who else is affected. With a shared cache, no retrieval that starts in
   the calling process afterwards uses the old decision, and other replicas stop within `local_ttl`.
3. Keep the audit events and request logs for the period, before any retention job removes them.

**Find out what happened.** Look up the events whose `subject_id` is that user and whose
`returned_ids` include the document's chunks, and note each event's `started_at`. Then go down
this table:

| Possible cause | How to tell | What to do |
|---|---|---|
| The access was allowed at that moment | The sharing or permission history grants it at `started_at` | Nothing failed. Review how the document came to be shared. |
| The retrieval started before the access was removed | `started_at` is before the change | Expected: a result already computed cannot be recalled ([Where the delay comes from](revocation.md#where-the-delay-comes-from), item 5). |
| It was removed, but within the cache's bound | The change came less than `ttl` before `started_at` and nothing called `invalidate()`; or less than `local_ttl` before it, on another replica | The documented bound. Call `invalidate()` from the code that removes access, or lower `ttl` ([Revocation and consistency](revocation.md)). |
| The permission service was behind | Its consistency mode (OpenFGA's `MINIMIZE_LATENCY`, SpiceDB's `minimizeLatency`) or its bundle or policy reload interval covers the gap | Use the stricter modes for this kind of access ([Strict revocation](revocation.md#strict-revocation)). |
| The chunk's metadata was wrong or not yet updated | The metadata stored on the chunk does not match the source document's sharing | Correct it with `set_metadata`, and run your permission sync on every sharing change, not on a schedule only. |
| A late re-index put the old permissions back | The chunk holds the sharing from before the change, and your logs show a re-index of the document that read it before the permission sync and wrote after it | Write the current sharing with `set_metadata`, and run the writes for each document one after another; on pgvector and MongoDB, pass `revision=` ([One writer at a time for each document](operations.md#one-writer-at-a-time-for-each-document)). |
| The subject had a group, tenant or id it should not have | The claims logged with that request | Build the subject only from verified authentication ([Security model](security.md#the-subject-must-come-from-authentication)). |
| None of these | | Treat it as a possible bug in wardowl: report it privately as [SECURITY.md](https://github.com/muzakkirhussain011/wardowl/blob/main/SECURITY.md) describes, with the versions, the authorizer, the backend and the event. |

Check your version against the project's
[security advisories](https://github.com/muzakkirhussain011/wardowl/security/advisories) as well.

**Afterwards.** If the chunks reached a model, its answers in that conversation may contain them;
handle that under your data-incident process. Record the cause and what changed, and if the cache's
bound was the cause, decide `ttl` again as the security decision it is.

## The violations count rose

**What it means.** The store returned chunks that the re-check then rejected. The re-check
(`recheck=True`, the default) dropped them, so none reached the caller. With `recheck=False`, from
1.4.0 they are matched against the filter in the process and dropped too; before 1.4.0 they were
passed through, so upgrade if you use it. The filter the store applied and the re-check disagreed:
a bug in the store is one reason, and the table below lists the others.

1. From the events with `violations` above 0, find which retriever, and so which store, index and
   authorizer, and since when. Note whether the count stays up or comes in bursts.
2. Find the rejected chunks. Events name only the chunks returned, so repeat the search outside
   the retriever for an affected subject and question:

   ```python
   f = authorizer.filter_for(subject)
   hits = backend.search(question, k, f)
   rejected = [h for h, ok in zip(hits, authorizer.check(subject, hits)) if not ok]
   ```

   With `recheck=False`, the retriever rejects the hits for which `wardowl.matches(f, h.metadata)`
   is False. A cause that has passed, such as an index that caught up, leaves `rejected` empty.
3. Go down this table.

| Possible cause | How to tell | What to do |
|---|---|---|
| Another tool changed the metadata and left the permission terms stale (Elasticsearch, OpenSearch, Milvus, MongoDB, Redis, Weaviate, LanceDB, Vespa, Cassandra, Pinecone) | Read a rejected chunk with the store's own client: its `wardowl_terms` differ from what wardowl writes for its metadata (`permission_terms(metadata)`, in the modules of the backends that have it), and a writer other than wardowl wrote it | Have every writer recompute the terms in the same write as each metadata change, or change metadata only with `set_metadata`. Rewrite the stale chunks. Look for chunks of the same writer whose terms grant less than their metadata: those are hidden from pre-filtered searches, and nothing counts them. |
| MongoDB's index had not caught up with a write | The store is MongoDB; the violations came while `add` or `set_metadata` ran, or after writes made with `wait=False`, and `rejected` is empty now | Nothing failed: such a chunk is dropped, not leaked ([Writes become searchable shortly after](integrations/mongodb.md#writes-become-searchable-shortly-after)). Expect bursts around writes; investigate violations that persist. |
| Chroma records without wardowl's JSON copy of the metadata | The store is Chroma, and the rejected records have no `wardowl_metadata` key: another tool wrote them, or they are kept from needtoknow 1.x, which wrote `needtoknow_metadata` | Write them with the backend's `add`. For a 1.x collection, index the chunks into a new collection, as [Upgrading](upgrading.md) says for needtoknow 1.x. |
| The authorizer's filter selects more than its `check` allows | `rejected` is not empty, its metadata and terms agree, and the authorizer's `check` denies chunks its filter selects: a field of a shape the policy does not expect (a list as OpenFGA's or SpiceDB's resource id, a list compared with `==` in Cerbos), an OPA policy whose filter and decisions differ, or a `FunctionAuthorizer` whose `filter_for` is broader than its `check`. `wardowl.testing.check_authorizer` with those subjects and chunks fails | Store each field in the shape the policy assumes, or correct the policy so the filter selects exactly what `check` allows ([OPA](integrations/opa.md#filters-for-pre-filtering), [Cerbos](integrations/cerbos.md#pre-filtering-from-query-plans)) |
| A permission changed between the filter and the check | The authorizer asks OpenFGA or SpiceDB, whose filter (ListObjects or LookupResources) and check (BatchCheck or CheckBulkPermissions) are separate calls, or a `CachingAuthorizer` still holds a filter from before the change; the violations follow permission changes | Nothing failed: the chunk the check now denies was dropped. With `CachingAuthorizer`, call `invalidate()` when permissions change, so a filter cached before the change stops being used ([Revocation and consistency](revocation.md)). |
| None of these: the store or the backend's translation | The count rose after a change: the store's version, the index mapping or schema, the type of a permission field (a number where the filter has strings), your own backend's translation, or a wardowl upgrade. `wardowl.testing.check_backend` fails against a staging copy of that store at that version ([conformance kit](extending.md#test-it-with-the-conformance-kit)); it compares the store's answers with the reference semantics on generated documents and filters | Report it as SECURITY.md describes: a store that returns what the filter excludes is a bug in its translation, even though the re-check contained it. |

## Retrievals fail with AuthorizationError

**What it means.** wardowl could not get a decision it can trust, so it returned nothing. Users
get errors; nothing is exposed.

1. Find which part is failing. From 1.4.0, `RetrievalEvent.cause` (or the span attribute
   `wardowl.error.cause`) names the underlying error: `TimeoutError` for a slow permission
   service, `ConnectionRefusedError` or another `OSError` for one that cannot be reached,
   `CircuitOpenError` for calls an open circuit breaker refused, and none for an error status
   (HTTP 503) or an answer in the wrong shape, which the service's own logs explain. The error's
   message is fixed; the text of the underlying exception is in its `__cause__`, in tracebacks
   your logs record.
2. Check the circuit breaker: while `CircuitBreaker.is_open`, calls are refused without contacting
   the service, for `reset_after` seconds, then one probe goes through. The causes of the failures
   before it opened say why it opened.
3. Restore the permission service. Do not work around it with an authorizer that allows on error, by
   removing the authorizer, or with a fallback that grants access: that turns an outage into an
   exposure.

Subjects whose decisions are still cached keep working until their entries expire. Raising `ttl`
during an outage keeps more of them working, and lengthens how long a revocation takes to apply.

## The shared cache is failing, or someone else may have written to it

**Redis is down or slow.** Every read or write that fails counts as a miss, so answers stay correct
and the permission service takes the load. From 1.4.0, `CachingAuthorizer.stats().shared_failures`
counts them. `invalidate()` raises while Redis is unreachable, because a revocation cannot be
confirmed: retry it once Redis is back.

**Someone else may have written to it.**

- With `signing_key` set, planted or altered entries fail their HMAC and are ignored.
- Without it, set a `signing_key` (at least 16 random bytes) on every replica: unsigned entries are
  then ignored. Then restrict who can write to that Redis.
- If the signing key itself may have leaked, replace it with a new random key on every replica, then
  call `invalidate()`. Entries signed with the old key are ignored; while replicas run different
  keys they simply miss each other's entries.

## An advisory affects your version

1. Read the advisory for the affected and patched versions, and any workaround.
2. Upgrade to a patched release of the line you run (supported lines are in
   [SECURITY.md](https://github.com/muzakkirhussain011/wardowl/blob/main/SECURITY.md#supported-versions)),
   for example `pip install --upgrade "wardowl>=1.2.1,<1.3"`.
3. Read that release's notes in the [changelog](changelog.md) before a rolling upgrade: a release
   that changes shared-cache keys asks you to call `invalidate()` once every replica runs it.
4. Apply the advisory's workaround to anything you cannot upgrade at once.

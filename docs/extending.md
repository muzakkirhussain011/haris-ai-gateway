# Extending wardowl

Any vector store and any permission system can be plugged in. Both sides are small protocols, so
there is nothing to subclass.

## A backend

A backend is any object with a `search` method and a `supports_filters` attribute:

```python
from wardowl import Filter, Hit


class MyBackend:
    supports_filters = True  # False if search() ignores `filter`

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        """Return at most k hits, best first, with each chunk's stored metadata."""
```

With `supports_filters = True`, translate the filter into your store's native filter. The rules
that make translations agree are in [Concepts](concepts.md#filters): a missing field never
matches, only strings and integers match, list fields match when they share an element, and
`In(field, [])` matches nothing. Return the metadata exactly as stored, because the re-check
decides on it.

Return a list of at most `k` hits. The retriever keeps no more hits than it asked for, but it reads
the whole answer before it cuts it to that number, so a generator that ignores `k` is read to its
end.

Add `async def asearch(...)` with the same signature for a native async path; otherwise
`aretrieve` runs `search` in a thread.

Optionally, set `exact_search = True` when `search` ranks every chunk that matches the filter, as a
brute-force scan or a flat index does, rather than walking an approximate index (HNSW, IVF and the
like) that can stop early. A result with fewer than k hits then proves that no other chunk matches,
and the retriever reports it as complete. Without the attribute, or with any value but `True`, the
search counts as approximate: a pre-filtered result with fewer than k hits, an empty one included,
says `complete=False`, and so does post-filtering that runs out of candidates inside a scope
([What a result tells you](concepts.md#what-a-result-tells-you)). It is not part of the
`SearchBackend` protocol's required members, so a backend without it still satisfies the protocol.
Declare it only when it holds for every search, whatever the index and settings: if exactness
depends on configuration, compute it from that configuration, as `FaissBackend` does from its
index.

Optionally, let the retriever check and repair a short result from an approximate index
([Short results](concepts.md#short-results)):

```python
class MyBackend:
    supports_filters = True
    supports_exact = True  # search() and asearch() accept exact=True

    def search(self, query, k, filter=None, *, exact=False):
        """With exact=True, rank every chunk that matches the filter, as a brute-force scan does."""

    def count(self, filter, limit):
        """How many chunks match the filter: the true number, or any number of at least limit."""
```

`count` may stop counting at `limit`, and may return `None` when a count of the filter's chunks
says nothing about the search, as for a keyword search, which ranks only chunks that contain the
query's words. An `async def acount(...)` with the same signature is used by `aretrieve` when it is
there. Both are read with `getattr`, like `exact_search`, and the retriever passes `exact` only to
a backend whose `supports_exact` is True, so a backend without them keeps working unchanged. The
retriever takes a count no larger than the hits, and an exact search shorter than k, as proof
that nothing else matches, so neither may fall short of the truth. The conformance kit checks both
when they are there.

Optionally, give it `set_metadata(metadata: Mapping[str, Mapping[str, Any]])` to replace stored
chunks' metadata without re-embedding them ([Changing a document's permissions](operations.md#changing-a-documents-permissions)):
replace the whole metadata, ignore ids that are not stored, and return once searches see the
change. The conformance kit checks it when it is there. A store that can compare a stored value
and write in one atomic step can also take `revision=` on `add` and `set_metadata`, as pgvector and
MongoDB do, and raise `wardowl.backends.StaleRevision` for chunks stored with a newer one
([One writer at a time for each document](operations.md#one-writer-at-a-time-for-each-document)).
The retriever never writes, so this is optional.

`query` is whatever the caller passed to `retrieve`. String queries arrive as strings; a caller
with its own vector passes a `Query`, and `Query.of(query).embedded(self.embed)` gives the vector
to search with either way. See [Your own vectors](vectors.md#in-your-own-backend).

## An authorizer

```python
from wardowl import Filter, Hit, Subject


class MyAuthorizer:
    def check(self, subject: Subject, hits: list[Hit], action: str = "read") -> list[bool]:
        """One real bool per hit, in order. Raise AuthorizationError on any doubt."""

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """A filter selecting exactly what the subject may access, or None."""
```

`check` is required. `filter_for` may always return `None`, in which case results are
post-filtered. If it returns a filter, the filter and `check` must agree: the store applies the
filter, `check` re-checks the results, and any disagreement shows up as `violations`. Async
versions are `afilter_for` and `acheck`.

For rules that fit in a function, `FunctionAuthorizer(check, filter_for=None)` wraps one.

## Test it with the conformance kit

`wardowl.testing` checks a backend or an authorizer the way the built-in ones are checked in
wardowl's CI. It generates documents, permission metadata and nested filters from a seed (with
scalar and list fields, mixed value types and missing fields: the cases where filter translations
tend to go wrong) and compares your implementation with the reference semantics. It needs no extra
packages, no model and no network.

```python
from wardowl.testing import HashingEmbedder, check_authorizer, check_backend


def make_backend(documents):
    backend = MyStoreBackend(client, embed=HashingEmbedder(), collection=new_collection())
    backend.add(documents)
    return backend


def test_my_backend_conforms():
    check_backend(make_backend)


def test_my_authorizer_conforms():
    check_authorizer(MyAuthorizer(...), subjects, hits)  # subjects and hits from your test data
```

`check_backend` checks that a search returns exactly the documents `matches` selects, at most k,
best first, with text and metadata exactly as stored (the retriever re-checks pre-filtered hits
against that metadata), that `asearch` returns the same hits as `search` (ids, order, text,
metadata and scores, with and without a filter), and that retrieval through `PermissionedRetriever`,
with `retrieve` and `aretrieve` alike, never returns a chunk the subject may not read. Its data
holds what stores tend to confuse: `1` and `"1"`, `"a"` and `"A"`, one letter composed and
decomposed, and field names with a dot or letters outside ASCII. A backend may refuse a filter on
such a field name by raising `ValueError`; it may not answer wrongly. For an approximate index that
may miss matches, pass `exact=False`: a result may then be short, but never wrong. A backend whose
`exact_search` is True is checked as exact whatever `exact` says, since the retriever takes its
short results as proof that nothing else matches, and so is every search asked for with
`exact=True`. `count`, where it returns a number, must count what `matches` selects.

`check_backend` stores documents through your factory and changes them only with the backend's own
`set_metadata`, so it shows that the backend's writes keep what the store filters on in step with
the metadata it returns. It has no handle on the store's own client, and cannot write as another
tool would. If something else changes the metadata of chunks the backend serves, such as an
ingestion pipeline writing with the store's client, test that it also rewrites what the store
filters on (the permission terms, on the backends that keep them): stale terms show up as
`violations`, or hide chunks without a sign ([What a result tells you](concepts.md#what-a-result-tells-you)).

`check_authorizer` checks that `check` returns one boolean per hit, that a hit's answer does not
depend on the rest of the batch, and that `check` agrees with the filter from `filter_for`. If the
two disagree, pre-filtered and post-filtered results differ. It also checks that `acheck`,
`afilter_for` and `ascope_for`, where they exist, agree with the sync methods, and it empties every
`CachingAuthorizer` before each call, so a cached answer cannot hide what the authorizer behind it
does.

`check_authorizer` shows that the authorizer is consistent, not that its policy is right: one whose
`check` and `filter_for` agree on letting everyone read everything passes. Nor does it check that a
`scope_for` covers every readable chunk; a scope too narrow hides chunks the subject may read
([Security model](security.md#what-it-relies-on)). Test the policy itself as well, with subjects and
chunks whose answers you know:

```python
from wardowl import AllOf, Hit, MetadataACLAuthorizer, Subject, TenantAuthorizer

authorizer = AllOf(TenantAuthorizer(), MetadataACLAuthorizer())


def test_policy_known_cases():
    salaries = Hit("salaries-1", "Salary bands", 1.0, {"tenant": "acme", "allowed_groups": ["hr"]})
    assert authorizer.check(Subject("ann", ["hr"], {"tenant": "acme"}), [salaries]) == [True]
    assert authorizer.check(Subject("bob", ["eng"], {"tenant": "acme"}), [salaries]) == [False]
    assert authorizer.check(Subject("eve", ["hr"], {"tenant": "globex"}), [salaries]) == [False]
```

Retrieving through a `PermissionedRetriever` in the same test also covers the backend's filter.

A failure raises `ConformanceError` with the seed, the example number, the documents, the filter
and what came back. For shrinking to a minimal failing example, the same properties can be written
with [Hypothesis](https://hypothesis.readthedocs.io), as wardowl's own
[`tests/test_backends.py`](https://github.com/muzakkirhussain011/wardowl/blob/main/tests/test_backends.py)
does.

## Contributing an adapter

New backends and authorizers are welcome. An adapter goes in its own module behind an optional
extra, passes `check_backend` or `check_authorizer` against the real service in CI, and fails
closed on every error. See the
[contributing guide](https://github.com/muzakkirhussain011/wardowl/blob/main/CONTRIBUTING.md).

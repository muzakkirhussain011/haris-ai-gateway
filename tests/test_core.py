from __future__ import annotations

import asyncio
import copy
import json
import pickle
from fractions import Fraction

import pytest

from wardowl import (
    AllOf,
    And,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    InvalidQuery,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    SearchError,
    Subject,
    TenantAuthorizer,
    matches,
)

ALICE = Subject("alice", groups=["hr"])
BOB = Subject("bob", groups=["eng"])


def corpus() -> list[Document]:
    return [
        Document("handbook", "leave policy for all staff", {"allowed_groups": ["*"]}),
        Document("salaries", "salary bands and leave payouts", {"allowed_groups": ["hr"]}),
        Document("runbook", "incident response and deploy rollback", {"allowed_groups": ["eng"]}),
        Document("bob-review", "performance review leave notes", {"allowed_users": ["bob"]}),
        Document("orphan", "leave policy draft with no permissions"),
    ]


# ---- filters and types -------------------------------------------------------------------------


def test_in_matches_scalars_and_lists():
    assert matches(In("team", {"eng"}), {"team": "eng"})
    assert matches(In("groups", {"eng"}), {"groups": ["hr", "eng"]})
    assert not matches(In("groups", {"eng"}), {"groups": []})
    assert not matches(In("team", {"eng"}), {})


def test_empty_and_or_are_rejected_because_empty_and_means_allow_all():
    with pytest.raises(ValueError):
        And()
    with pytest.raises(ValueError):
        Or()


def test_nested_filters():
    f = And(In("tenant", {"acme"}), Or(In("team", {"eng"}), In("level", {"public"})))
    assert matches(f, {"tenant": "acme", "level": "public"})
    assert not matches(f, {"tenant": "other", "team": "eng"})


def test_subject_rejects_a_single_string_for_groups():
    with pytest.raises(TypeError):
        Subject("bob", groups="eng")
    with pytest.raises(ValueError):
        Subject("")


def test_subjects_are_hashable_for_caching():
    assert hash(Subject("bob", ["eng"])) == hash(Subject("bob", ["eng"], {"region": "eu"}))


@pytest.mark.parametrize("bad", [lambda: Subject(42), lambda: Subject(b"bob"), lambda: Subject("bob", ["eng", 7])])
def test_subject_ids_and_groups_are_strings(bad):
    # 7 and "7" are different filter values, so a group of another type would match differently.
    with pytest.raises(TypeError):
        bad()


def test_subject_attributes_cannot_change_once_the_subject_is_made():
    roles, org = ["reader"], {"region": "eu"}
    attributes = {"tenant": "acme", "roles": roles, "org": org, "tags": {"a"}}
    subject = Subject("bob", ["eng"], attributes)
    attributes["tenant"], org["region"] = "globex", "us"
    roles.append("admin")
    assert subject.attributes == {"tenant": "acme", "roles": ("reader",), "org": {"region": "eu"}, "tags": {"a"}}
    changes = [
        lambda: subject.attributes.__setitem__("tenant", "globex"),
        lambda: subject.attributes.update(tenant="globex"),
        lambda: subject.attributes.pop("tenant"),
        lambda: subject.attributes.clear(),
        lambda: subject.attributes["org"].__setitem__("region", "us"),
        lambda: subject.attributes["org"].__ior__({"region": "us"}),
    ]
    for change in changes:
        with pytest.raises(TypeError):
            change()
    assert subject.attributes["tenant"] == "acme" and subject.attributes["org"] == {"region": "eu"}
    # Still a dict for the authorizers that send attributes to a permission service, and copyable.
    plain = Subject("ann", [], {"tenant": "acme", "roles": ["reader"], "org": {"region": "eu"}})
    assert json.loads(json.dumps(plain.attributes)) == {"tenant": "acme", "roles": ["reader"], "org": {"region": "eu"}}
    assert pickle.loads(pickle.dumps(subject)) == subject == copy.deepcopy(subject)  # noqa: S301


def test_a_decision_cached_for_a_subject_is_not_reused_after_its_attributes_change():
    tenants = ["acme"]
    bob = Subject("bob", [], {"tenants": tenants})
    authz = CachingAuthorizer(FunctionAuthorizer(lambda s, hits, a: ["acme" in s.attributes["tenants"]] * len(hits)))
    hits = [Hit("plan", "t", 1.0, {})]
    assert authz.check(bob, hits) == [True]
    tenants.remove("acme")  # the caller's list, changed after the decision was cached
    assert authz.check(Subject("bob", [], {"tenants": []}), hits) == [False]


def test_the_in_memory_backend_keeps_its_own_copy_of_the_metadata():
    stored = {"allowed_groups": ["hr"]}
    backend = InMemoryBackend([Document("d", "leave", stored)])
    stored["allowed_groups"].append("eng")  # the caller's dict, changed after it was added
    hit = backend.search("leave", 1)[0]
    hit.metadata["allowed_groups"].append("sales")  # a hit handed out, changed by whoever got it
    hit.metadata["allowed_users"] = ["bob"]
    assert backend.search("leave", 1)[0].metadata == {"allowed_groups": ["hr"]}
    assert PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve("leave", BOB).hits == []


# ---- metadata ACL ------------------------------------------------------------------------------


def test_metadata_acl_user_group_everyone_and_missing_permissions():
    backend = InMemoryBackend(corpus())
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    ids = {h.id for h in retriever.retrieve("leave", BOB, k=10).hits}
    assert ids == {"handbook", "bob-review"}  # not hr-only, not the chunk without permissions
    ids = {h.id for h in retriever.retrieve("leave", ALICE, k=10).hits}
    assert ids == {"handbook", "salaries"}


def test_unsupported_action_fails_closed():
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), MetadataACLAuthorizer(), action="delete")
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_blank_group_names_grant_nothing_on_either_side(strategy):
    # Subject accepts "" and "  " as groups, as an identity provider can send them; the sync layer
    # never writes them. A chunk that lists one must not be readable through it.
    docs = [
        Document("empty", "leave", {"allowed_groups": [""]}),
        Document("blank", "leave", {"allowed_groups": ["  ", "\t"]}),
        Document("eng", "leave", {"allowed_groups": ["eng"]}),
    ]
    subject = Subject("bob", ["", "  ", "\t", "eng"])
    retriever = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(), strategy=strategy)
    assert [h.id for h in retriever.retrieve("leave", subject, k=5).hits] == ["eng"]
    assert MetadataACLAuthorizer().filter_for(subject) == Or(
        In("allowed_users", ["bob"]), In("allowed_groups", ["eng", "*"])
    )
    # Nor does a blank marker for everyone: it would make a chunk that lists "" readable by all.
    blank = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(everyone=""), strategy=strategy)
    assert blank.retrieve("leave", Subject("ann"), k=5).hits == []
    assert [h.id for h in blank.retrieve("leave", subject, k=5).hits] == ["eng"]


# ---- strategies --------------------------------------------------------------------------------


def test_auto_uses_prefilter_when_possible_and_postfilter_otherwise():
    backend = InMemoryBackend(corpus())
    assert PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve("leave", BOB).strategy == "prefilter"
    only_check = FunctionAuthorizer(lambda s, hits, a: [h.metadata.get("allowed_groups") == ["*"] for h in hits])
    assert PermissionedRetriever(backend, only_check).retrieve("leave", BOB).strategy == "postfilter"


def test_prefilter_requires_a_filter_capable_authorizer():
    only_check = FunctionAuthorizer(lambda s, hits, a: [True] * len(hits))
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), only_check, strategy="prefilter")
    with pytest.raises(ValueError):
        retriever.retrieve("leave", BOB)


def test_prefilter_requires_a_filter_capable_backend():
    class NoFilters(InMemoryBackend):
        supports_filters = False

    with pytest.raises(ValueError):
        PermissionedRetriever(NoFilters(corpus()), MetadataACLAuthorizer(), strategy="prefilter")


def test_postfilter_widens_until_it_finds_k_permitted_hits():
    # 100 matching chunks; Bob may read only the 3 lowest-ranked ones.
    docs = [Document(f"d{i:03d}", "leave " * (1 + (i % 5)), {"allowed_groups": ["hr"]}) for i in range(97)]
    docs += [Document(f"z{i}", "leave", {"allowed_users": ["bob"]}) for i in range(3)]
    retriever = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(), strategy="postfilter")
    result = retriever.retrieve("leave", BOB, k=3)
    assert sorted(h.id for h in result.hits) == ["z0", "z1", "z2"]
    assert result.complete and result.denied >= 97


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_k_above_max_candidates_is_refused(strategy):
    # Pre-filtering searched for k hits and re-checked all of them, however large k was.
    retriever = PermissionedRetriever(
        InMemoryBackend(corpus()), MetadataACLAuthorizer(), strategy=strategy, max_candidates=3
    )
    with pytest.raises(ValueError, match="max_candidates"):
        retriever.retrieve("leave", BOB, k=4)
    with pytest.raises(ValueError, match="max_candidates"):
        asyncio.run(retriever.aretrieve("leave", BOB, k=4))
    assert retriever.retrieve("leave", ALICE, k=3).hits  # up to max_candidates is fine


def test_postfilter_checks_no_more_candidates_than_it_asked_for():
    class Generous(InMemoryBackend):
        def search(self, query, k, filter=None):
            return super().search(query, k + 100, filter)  # ignores k

    asked = []

    def deny(subject, hits, action):
        asked.append(len(hits))
        return [False] * len(hits)

    docs = [Document(f"d{i:03d}", "leave", {}) for i in range(300)]
    authz = FunctionAuthorizer(deny)
    retriever = PermissionedRetriever(Generous(docs), authz, strategy="postfilter", max_candidates=20)
    result = retriever.retrieve("leave", BOB, k=2)
    assert asked == [8, 12] and result.examined == 20 and result.complete is False


def test_the_rerank_depth_stays_within_max_candidates():
    sizes = []

    class Recording(InMemoryBackend):
        def search(self, query, k, filter=None):
            sizes.append(k)
            return super().search(query, k, filter)

    retriever = PermissionedRetriever(
        Recording(corpus()), MetadataACLAuthorizer(), reranker=lambda query, hits: hits, max_candidates=10
    )
    retriever.retrieve("leave", BOB, k=2)
    assert sizes == [10]  # not the default rerank_depth of 50


def test_postfilter_reports_incomplete_results_when_capped():
    docs = [Document(f"d{i:03d}", "leave", {"allowed_groups": ["hr"]}) for i in range(50)]
    docs.append(Document("zzz", "leave", {"allowed_users": ["bob"]}))
    retriever = PermissionedRetriever(
        InMemoryBackend(docs), MetadataACLAuthorizer(), strategy="postfilter", max_candidates=20
    )
    result = retriever.retrieve("leave", BOB, k=1)
    assert result.hits == [] and result.complete is False


# ---- complete: what a short result proves ------------------------------------------------------


class Store:
    """The in-memory backend's answers behind an index that declares `exact_search` as given, or
    not at all (an approximate index, which may stop early). Counts its searches."""

    supports_filters = True

    def __init__(self, docs, exact_search=None):
        self.store = InMemoryBackend(docs)
        if exact_search is not None:
            self.exact_search = exact_search
        self.searches = 0

    def search(self, query, k, filter=None):
        self.searches += 1
        return self.store.search(query, k, filter)


def _both(retriever, query, subject, k):
    """What `retrieve` returns, once `aretrieve` has returned the same."""
    result = retriever.retrieve(query, subject, k)
    assert asyncio.run(retriever.aretrieve(query, subject, k)) == result
    return result


EXACT_AND_APPROXIMATE = [pytest.param(InMemoryBackend, True, id="exact"), pytest.param(Store, False, id="approximate")]


@pytest.mark.parametrize(("backend", "complete"), EXACT_AND_APPROXIMATE)
@pytest.mark.parametrize(("query", "found"), [("leave", 2), ("salary", 0)], ids=["short", "empty"])
def test_a_short_prefiltered_result_is_complete_only_from_an_exact_search(backend, complete, query, found):
    # Bob may read two chunks about leave and none about salaries. Only an exact search proves that
    # nothing else matches: an approximate index may have stopped early under the filter, as
    # pgvector's iterative scan does at hnsw.max_scan_tuples.
    result = _both(PermissionedRetriever(backend(corpus()), MetadataACLAuthorizer()), query, BOB, 5)
    assert result.strategy == "prefilter" and len(result.hits) == found and result.violations == 0
    assert result.complete is complete


@pytest.mark.parametrize(("declared", "complete"), [(True, True), (False, False), ("yes", False), (1, False)])
def test_only_exact_search_true_makes_a_short_result_complete(declared, complete):
    retriever = PermissionedRetriever(Store(corpus(), declared), MetadataACLAuthorizer())
    assert _both(retriever, "leave", BOB, 5).complete is complete


def test_a_full_result_from_an_approximate_search_is_taken_at_the_backends_word():
    # k hits: complete trusts the ranking, and cannot see better chunks an index failed to rank.
    result = _both(PermissionedRetriever(Store(corpus()), MetadataACLAuthorizer()), "leave", BOB, 2)
    assert len(result.hits) == 2 and result.complete is True


def test_with_a_reranker_a_pool_shorter_than_its_depth_is_short():
    # The reranker chooses from the top rerank_depth chunks; an approximate index that returned fewer
    # may have missed the one it would put first.
    def retriever(backend):
        return PermissionedRetriever(backend, MetadataACLAuthorizer(), reranker=lambda q, hits: hits, rerank_depth=5)

    assert _both(retriever(Store(corpus())), "leave", BOB, 2).complete is False
    assert _both(retriever(InMemoryBackend(corpus())), "leave", BOB, 2).complete is True


ANN = Subject("ann", [], {"tenant": "acme"})
ANYONE = FunctionAuthorizer(lambda subject, hits, action: [True] * len(hits))  # a rule with no filter


@pytest.mark.parametrize(("declared", "complete"), [(True, True), (None, False)], ids=["exact", "approximate"])
def test_running_out_inside_a_scope_is_complete_only_on_an_exact_search(declared, complete):
    # The rule has no filter, so AllOf has none either, but it scopes post-filtering to the tenant.
    # Its 3 chunks come back, fewer than the 20 asked for: an exact search has ranked the whole
    # scope, while an approximate index may have stopped early inside it.
    docs = [Document(f"a{i}", "leave policy", {"tenant": "acme"}) for i in range(3)]
    docs += [Document(f"g{i:02d}", "leave policy", {"tenant": "globex"}) for i in range(40)]
    store = Store(docs, declared)
    result = _both(PermissionedRetriever(store, AllOf(TenantAuthorizer(), ANYONE)), "leave", ANN, 5)
    assert result.strategy == "postfilter" and sorted(h.id for h in result.hits) == ["a0", "a1", "a2"]
    assert result.complete is complete
    assert store.searches == 2  # one for each driver: a short list still ends the search, as before


def test_running_out_without_a_filter_is_complete_on_any_search():
    # No filter and no scope: an index with no filter to apply returns every chunk it holds, up to
    # the number asked for, so a short list is the end of the store.
    store = Store(corpus())
    result = _both(PermissionedRetriever(store, FunctionAuthorizer(MetadataACLAuthorizer().check)), "leave", BOB, 5)
    assert result.strategy == "postfilter" and sorted(h.id for h in result.hits) == ["bob-review", "handbook"]
    assert result.complete is True and store.searches == 2


# ---- failing closed ----------------------------------------------------------------------------


def _raises(*_args):
    raise ConnectionError("permission service unreachable")


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_authorizer_errors_return_nothing(strategy):
    authorizer = FunctionAuthorizer(_raises, filter_for=_raises if strategy == "prefilter" else None)
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy=strategy)
    with pytest.raises(AuthorizationError) as failure:
        retriever.retrieve("leave", BOB)
    assert "unreachable" in str(failure.value.__cause__)


# What a real authorizer's error said, word for word but for the host and the password.
DETAILS = "could not reach opa.internal:8181 as svc-wardowl (password hunter2)"


def _leaks():
    raise ConnectionError(DETAILS)


class _Leaky:
    """An authorizer whose every call fails with an error that names a host and a password."""

    def check(self, subject, hits, action="read"):
        _leaks()

    def filter_for(self, subject, action="read"):
        _leaks()

    def scope_for(self, subject, action="read"):
        _leaks()


class _LeakyAsync:
    async def acheck(self, subject, hits, action="read"):
        _leaks()

    async def afilter_for(self, subject, action="read"):
        _leaks()

    check = _Leaky.check
    filter_for = _Leaky.filter_for


def _fails_without_the_secret(call, raised=AuthorizationError):
    with pytest.raises(raised) as failure:
        call()
    error = failure.value
    assert "hunter2" not in str(error) and "hunter2" not in repr(error) and "opa.internal" not in str(error)
    assert DETAILS in str(error.__cause__)  # kept for the logs, which record the traceback
    return error


@pytest.mark.parametrize("strategy", ["auto", "postfilter"])
@pytest.mark.parametrize("authorizer", [_Leaky(), _LeakyAsync()], ids=["sync", "async"])
def test_an_authorizer_error_reaches_no_message_but_stays_the_cause(strategy, authorizer):
    # Agent frameworks show a tool's error message to the model: it must hold no host or password.
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy=strategy)
    assert str(_fails_without_the_secret(lambda: retriever.retrieve("leave", BOB))) == "authorizer failed"
    _fails_without_the_secret(lambda: asyncio.run(retriever.aretrieve("leave", BOB)))


def test_a_redactor_error_reaches_no_message_but_stays_the_cause():
    def redact(subject, hits):
        _leaks()

    retriever = PermissionedRetriever(InMemoryBackend(corpus()), MetadataACLAuthorizer(), redactor=redact)
    assert str(_fails_without_the_secret(lambda: retriever.retrieve("leave", BOB))) == "redaction failed"
    _fails_without_the_secret(lambda: asyncio.run(retriever.aretrieve("leave", BOB)))


class _LeakyStore(InMemoryBackend):
    """A store whose searches fail with an error that names a host and a password, as drivers' do."""

    def search(self, query, k, filter=None):
        _leaks()


class _LeakyAsyncStore(_LeakyStore):
    async def asearch(self, query, k, filter=None):
        _leaks()


class _LeakyCount(InMemoryBackend):
    """A store whose count fails that way; it searches approximately, so a short result is counted."""

    exact_search = False

    def count(self, filter, limit):
        _leaks()


class _LeakyAsyncCount(_LeakyCount):
    async def acount(self, filter, limit):
        _leaks()


@pytest.mark.parametrize("store", [_LeakyStore, _LeakyAsyncStore], ids=["sync", "async"])
def test_a_store_error_reaches_no_message_but_stays_the_cause(store):
    # A store's driver names its host and credentials too, and a tool's error reaches the model.
    events = []
    retriever = PermissionedRetriever(store(corpus()), MetadataACLAuthorizer(), observers=[events.append])
    error = _fails_without_the_secret(lambda: retriever.retrieve("leave", BOB), SearchError)
    assert str(error) == "search failed" and not isinstance(error, AuthorizationError)
    _fails_without_the_secret(lambda: asyncio.run(retriever.aretrieve("leave", BOB)), SearchError)
    assert [(e.error, e.cause, e.returned_ids) for e in events] == [("SearchError", "ConnectionError", ())] * 2


@pytest.mark.parametrize("store", [_LeakyCount, _LeakyAsyncCount], ids=["sync", "async"])
def test_a_count_error_reaches_no_message_but_stays_the_cause(store):
    retriever = PermissionedRetriever(store(corpus()), MetadataACLAuthorizer(), short_results="count")
    error = _fails_without_the_secret(lambda: retriever.retrieve("leave", BOB, k=10), SearchError)
    assert str(error) == "count failed"
    _fails_without_the_secret(lambda: asyncio.run(retriever.aretrieve("leave", BOB, k=10)), SearchError)


def test_a_query_the_store_cannot_search_is_raised_as_it_is():
    # InvalidQuery says the request is at fault (the HTTP service answers it with 422): not wrapped.
    class TextOnly(InMemoryBackend):
        def search(self, query, k, filter=None):
            raise InvalidQuery("this store searches text only")

    retriever = PermissionedRetriever(TextOnly(corpus()), MetadataACLAuthorizer())
    with pytest.raises(InvalidQuery, match="text only"):
        retriever.retrieve("leave", BOB)
    with pytest.raises(InvalidQuery, match="text only"):
        asyncio.run(retriever.aretrieve("leave", BOB))


@pytest.mark.parametrize(
    "decisions",
    [
        lambda hits: [True] * (len(hits) + 1),  # wrong length
        lambda hits: ["yes"] * len(hits),  # truthy non-bools must not count as allow
        lambda hits: [1] * len(hits),
    ],
)
def test_malformed_decisions_fail_closed(decisions):
    authorizer = FunctionAuthorizer(lambda s, hits, a: decisions(hits))
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy="postfilter")
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)


def test_invalid_arguments():
    backend, authz = InMemoryBackend(corpus()), MetadataACLAuthorizer()
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz, strategy="fastest")
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz, overfetch=1)
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz).retrieve("leave", BOB, k=0)
    with pytest.raises(ValueError, match="audit_key"):
        PermissionedRetriever(backend, authz, audit_key=b"short")  # guessable, unlike 16 random bytes


# ---- async -------------------------------------------------------------------------------------


def test_native_async_backend_and_authorizer_are_used():
    calls = []

    class AsyncBackend(InMemoryBackend):
        async def asearch(self, query, k, filter=None):
            calls.append("asearch")
            return self.search(query, k, filter)

    class AsyncAuthorizer(MetadataACLAuthorizer):
        async def afilter_for(self, subject, action="read"):
            calls.append("afilter_for")
            return self.filter_for(subject, action)

        async def acheck(self, subject, hits, action="read"):
            calls.append("acheck")
            return self.check(subject, hits, action)

    retriever = PermissionedRetriever(AsyncBackend(corpus()), AsyncAuthorizer())
    result = asyncio.run(retriever.aretrieve("leave", BOB, k=10))
    assert {h.id for h in result.hits} == {"handbook", "bob-review"}
    assert calls == ["afilter_for", "asearch", "acheck"]


def test_async_authorizer_errors_fail_closed():
    authorizer = FunctionAuthorizer(_raises)
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy="postfilter")
    with pytest.raises(AuthorizationError):
        asyncio.run(retriever.aretrieve("leave", BOB))


# ---- odd answers: deep or malformed filters, replies that are not hits ------------------------


def _deep(depth):
    """A filter `depth` levels deep: an `In` inside `depth - 1` `And`s, built without recursion."""
    f = In("allowed_groups", ["*"])
    for _ in range(depth - 1):
        f = And(f)
    return f


class _Ignores(InMemoryBackend):
    """A store that ignores the filter: only the retriever then matches a pre-filtered hit against it."""

    def search(self, query, k, filter=None):
        return super().search(query, k)


@pytest.mark.parametrize("recheck", [True, False])
@pytest.mark.parametrize("store", [InMemoryBackend, _Ignores])
def test_an_authorizers_filter_nested_more_than_a_hundred_levels_fails_closed(store, recheck):
    # 5,000 levels exceed Python's recursion limit wherever the filter is matched or translated; with
    # recheck=False, retrieve raised a bare RecursionError. Now neither depth is searched.
    for depth in (101, 5_000):
        authorizer = FunctionAuthorizer(MetadataACLAuthorizer().check, filter_for=lambda s, a, d=depth: _deep(d))
        retriever = PermissionedRetriever(store(corpus()), authorizer, recheck=recheck)
        with pytest.raises(AuthorizationError, match="nested more than 100 levels deep"):
            retriever.retrieve("leave", BOB)
        with pytest.raises(AuthorizationError, match="nested more than 100 levels deep"):
            asyncio.run(retriever.aretrieve("leave", BOB))


@pytest.mark.parametrize("recheck", [True, False])
def test_an_authorizers_filter_a_hundred_levels_deep_is_searched(recheck):
    authorizer = FunctionAuthorizer(MetadataACLAuthorizer().check, filter_for=lambda s, a: _deep(100))
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, recheck=recheck)
    for result in (retriever.retrieve("leave", BOB), asyncio.run(retriever.aretrieve("leave", BOB))):
        assert [h.id for h in result.hits] == ["handbook"] and result.violations == 0


def test_an_authorizers_scope_is_checked_like_its_filter():
    class DeepScope:
        def check(self, subject, hits, action="read"):
            return [True] * len(hits)

        def filter_for(self, subject, action="read"):
            return None

        def scope_for(self, subject, action="read"):
            return _deep(5_000)

    retriever = PermissionedRetriever(InMemoryBackend(corpus()), DeepScope())
    with pytest.raises(AuthorizationError, match="nested more than 100 levels deep"):
        retriever.retrieve("leave", BOB)
    with pytest.raises(AuthorizationError, match="nested more than 100 levels deep"):
        asyncio.run(retriever.aretrieve("leave", BOB))


def _holding_itself():
    f = Or(In("allowed_groups", ["eng"]))
    object.__setattr__(f, "clauses", (f,))  # only possible around the constructor
    return f


def _and_without_clauses():
    f = And(In("allowed_groups", ["eng"]))
    object.__setattr__(f, "clauses", ())  # And() refuses this: it would allow everything
    return f


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (lambda: Or(In("allowed_groups", ["eng"]), "everything"), "holding str, not a filter"),
        (lambda: And(In("tenant", ["acme"]), Or(In("allowed_groups", ["eng"]), None)), "holding NoneType"),
        (_and_without_clauses, "an And without clauses"),
        (_holding_itself, "a filter that holds itself"),
    ],
    ids=["str", "nested None", "empty And", "cycle"],
)
@pytest.mark.parametrize("recheck", [True, False])
def test_a_filter_holding_something_else_fails_as_the_authorizers_fault(answer, message, recheck):
    # And() and Or() take any clause. Such a filter reached the store, whose translation failed on
    # it, and the retrieval raised SearchError, which blames the store.
    authorizer = FunctionAuthorizer(MetadataACLAuthorizer().check, filter_for=lambda s, a: answer())
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, recheck=recheck)
    with pytest.raises(AuthorizationError, match=message):
        retriever.retrieve("leave", BOB)
    with pytest.raises(AuthorizationError, match=message):
        asyncio.run(retriever.aretrieve("leave", BOB))


def test_a_filter_whose_clauses_are_shared_is_walked_once_per_clause():
    # 2**64 paths through 65 clause objects: a walk along every path would not end.
    f = In("allowed_groups", ["*"])
    for _ in range(64):
        f = Or(f, f)
    authorizer = FunctionAuthorizer(lambda s, hits, a: [True] * len(hits), filter_for=lambda s, a: f)
    store = InMemoryBackend([Document("open", "leave policy", {"allowed_groups": ["*"]})])
    assert [h.id for h in PermissionedRetriever(store, authorizer).retrieve("leave", BOB).hits] == ["open"]


class _Replies(InMemoryBackend):
    """A store whose every search answers with `reply`, as a broken or foreign backend might."""

    def __init__(self, reply):
        super().__init__(corpus())
        self.reply = reply

    def search(self, query, k, filter=None):
        return self.reply


class _AsyncReplies(_Replies):
    async def asearch(self, query, k, filter=None):
        return self.reply


READABLE = {"allowed_groups": ["*"]}


@pytest.mark.parametrize(
    "reply",
    [
        [Hit(["handbook"], "leave", 1.0, READABLE)],  # an id that cannot be hashed
        [Hit(7, "leave", 1.0, READABLE)],
        [Hit("handbook", "leave", None, READABLE)],  # passed the check and was returned as it was
        [Hit("handbook", "leave", "high", READABLE), Hit("salaries", "leave", 0.5, READABLE)],
        [Hit("handbook", "leave", True, READABLE)],
        [Hit("handbook", "leave", 1.0, None)],  # no metadata to match the filter against
        [Hit("handbook", "leave", 1.0, ["allowed_groups", "*"])],
        [{"id": "handbook", "text": "leave", "score": 1.0, "metadata": READABLE}],
        None,
        "handbook",
    ],
)
@pytest.mark.parametrize("store", [_Replies, _AsyncReplies], ids=["sync", "async"])
@pytest.mark.parametrize(("strategy", "recheck"), [("prefilter", True), ("prefilter", False), ("postfilter", True)])
def test_a_reply_that_is_not_hits_fails_as_the_stores_fault(reply, store, strategy, recheck):
    events = []
    retriever = PermissionedRetriever(
        store(reply), MetadataACLAuthorizer(), strategy=strategy, recheck=recheck, observers=[events.append]
    )
    with pytest.raises(SearchError, match=r"^search failed$") as failure:
        retriever.retrieve("leave", BOB)
    assert isinstance(failure.value.__cause__, TypeError)
    with pytest.raises(SearchError, match=r"^search failed$") as failure:
        asyncio.run(retriever.aretrieve("leave", BOB))
    assert isinstance(failure.value.__cause__, TypeError)
    assert [(e.error, e.cause, e.returned_ids) for e in events] == [("SearchError", "TypeError", ())] * 2


def test_only_the_hits_asked_for_are_read_and_any_real_number_is_a_score():
    reply = [
        Hit("handbook", "leave", 2, READABLE),
        Hit("bob-review", "leave", Fraction(1, 2), {"allowed_users": ["bob"]}),
        None,  # past k: never read
    ]
    retriever = PermissionedRetriever(_Replies(reply), MetadataACLAuthorizer())
    assert [h.id for h in retriever.retrieve("leave", BOB, k=2).hits] == ["handbook", "bob-review"]
    with pytest.raises(SearchError):
        retriever.retrieve("leave", BOB, k=3)

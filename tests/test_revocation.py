"""Revocation through CachingAuthorizer: what `invalidate()` promises, under concurrent retrievals.

The promise (docs/operations.md): after `invalidate()` returns, no retrieval that starts in this
process uses an answer given before it, and other replicas sharing the cache stop within
`local_ttl`. The hard case is an answer the authorizer gave before the revocation, to a call that
is still in flight when `invalidate()` runs: it must not be cached afterwards, here or in the
shared cache. Another is an authorizer that still answers from before the change just after it,
as SpiceDB does under `minimizeLatency`: for its `max_staleness`, nothing it says is cached.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

from wardowl import (
    AllOf,
    AnyOf,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)

NS = "test-policy"  # a shared cache needs a namespace of its own
BOB = Subject("bob", ["eng"])
ANN = Subject("ann", ["hr"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})]


class Shared:
    """A dictionary standing in for Redis, safe to use from several threads; it ignores TTLs."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get_many(self, keys):
        with self._lock:
            return [self.data.get(k) for k in keys]

    def set_many(self, entries, ttl):
        with self._lock:
            self.data.update(entries)

    def delete_prefix(self, prefix):
        with self._lock:
            for key in [k for k in self.data if k.startswith(prefix)]:
                del self.data[key]


class Held:
    """MetadataACLAuthorizer whose answers wait until `release` is set, so a test can revoke
    while a call is in flight. Counts the hits and filters it was asked about."""

    def __init__(self):
        self.inner = MetadataACLAuthorizer()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.checks = self.filters = 0

    def _wait(self):
        self.entered.set()
        assert self.release.wait(5), "the test never released the authorizer"

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        self._wait()
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        self._wait()
        return self.inner.filter_for(subject, action)


def _in_flight(call, held, revoke):
    """Start `call` in a thread, revoke while the authorizer is answering it, then let it finish."""
    worker = threading.Thread(target=call)
    worker.start()
    assert held.entered.wait(5)
    revoke()
    held.release.set()
    worker.join(5)
    assert not worker.is_alive()


def _released():
    held = Held()
    held.release.set()
    return held


# ---- an answer given before invalidate() is not cached after it ------------------------------------


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_given_before_invalidate_is_not_cached_in_this_process(scope):
    held = Held()
    cache = CachingAuthorizer(held, ttl=30)
    _in_flight(lambda: cache.check(BOB, HITS), held, lambda: cache.invalidate(BOB if scope == "subject" else None))
    cache.check(BOB, HITS)
    assert held.checks == 4  # asked again: the answer from before the revocation was not kept


def test_a_filter_given_before_invalidate_is_not_cached():
    held = Held()
    cache = CachingAuthorizer(held, ttl=30)
    _in_flight(lambda: cache.filter_for(BOB), held, lambda: cache.invalidate(BOB))
    cache.filter_for(BOB)
    assert held.filters == 2


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_another_replica_writes_after_invalidate_is_never_read(scope):
    shared = Shared()
    held, fresh = Held(), _released()
    slow = CachingAuthorizer(held, shared=shared, namespace=NS)  # its answer arrives after the revocation
    admin = CachingAuthorizer(_released(), shared=shared, namespace=NS)  # the replica that revokes
    _in_flight(lambda: slow.check(BOB, HITS), held, lambda: admin.invalidate(BOB if scope == "subject" else None))
    assert any("!epoch" not in key for key in shared.data)  # the late answer was written...
    third = CachingAuthorizer(fresh, shared=shared, namespace=NS)
    third.check(BOB, HITS)
    assert fresh.checks == 2  # ...but no replica uses it


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_copied_from_the_shared_cache_while_invalidate_runs_is_not_kept(scope):
    class Racing(Shared):
        """Runs `during` once, just before the next epoch is written: in the middle of invalidate()."""

        during = None

        def set_many(self, entries, ttl):
            if self.during is not None and any("!epoch" in k for k in entries):
                during, self.during = self.during, None
                during()
            super().set_many(entries, ttl)

    shared, held = Racing(), _released()
    cache = CachingAuthorizer(held, shared=shared, namespace=NS)
    cache.check(BOB, HITS)  # cached in this process and in the shared cache
    # A retrieval in this process after it dropped its own entries, before the new epoch exists:
    # it finds the old entry in the shared cache, still valid, and copies it.
    shared.during = lambda: cache.check(BOB, HITS)
    cache.invalidate(BOB if scope == "subject" else None)
    asked = held.checks
    cache.check(BOB, HITS)
    assert held.checks == asked + 2  # asked again: the copy made during invalidate() was dropped


def test_a_shared_entry_put_back_after_invalidate_is_refused():
    shared = Shared()
    first = CachingAuthorizer(_released(), shared=shared, namespace=NS)
    first.check(BOB, HITS)
    before = dict(shared.data)
    first.invalidate(BOB)
    shared.data.update(before)  # the entries as they were before the revocation, written back
    held = _released()
    CachingAuthorizer(held, shared=shared, namespace=NS).check(BOB, HITS)
    assert held.checks == 2


def test_async_answers_given_before_invalidate_are_not_cached():
    class HeldAsync:
        def __init__(self):
            self.inner, self.checks = MetadataACLAuthorizer(), 0
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        def check(self, subject, hits, action="read"):
            return self.inner.check(subject, hits, action)

        def filter_for(self, subject, action="read"):
            return self.inner.filter_for(subject, action)

        async def acheck(self, subject, hits, action="read"):
            self.checks += len(hits)
            self.entered.set()
            await self.release.wait()
            return self.inner.check(subject, hits, action)

    async def scenario(shared):
        held = HeldAsync()
        cache = CachingAuthorizer(held, shared=shared, namespace=NS)
        call = asyncio.create_task(cache.acheck(BOB, HITS))
        await held.entered.wait()
        cache.invalidate(BOB)
        held.release.set()
        await call
        await cache.acheck(BOB, HITS)
        return held.checks

    assert asyncio.run(scenario(None)) == 4
    assert asyncio.run(scenario(Shared())) == 4


# ---- an authorizer that answers from stale data --------------------------------------------------


class Clock:
    """A clock the test moves; the cache reads it as both its own clock and the wall clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Lagging:
    """A permission service that answers from its data as it was `lag` seconds ago, as SpiceDB under
    minimizeLatency does, and says so with `max_staleness`. Bob reads "a", Ann reads "b"."""

    def __init__(self, clock, lag=5.0):
        self.clock, self.max_staleness = clock, lag
        self.history = [(-math.inf, {"a": {"bob"}, "b": {"ann"}})]  # from when, who reads what
        self.checks = self.filters = 0

    def _readers(self):
        seen = self.clock() - self.max_staleness
        return [readers for since, readers in self.history if since <= seen][-1]

    def revoke(self, doc, user):
        readers = {d: set(users) for d, users in self.history[-1][1].items()}
        readers[doc].discard(user)
        self.history.append((self.clock(), readers))

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        readers = self._readers()
        return [subject.id in readers.get(h.id, ()) for h in hits]

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return In("doc_id", sorted(d for d, users in self._readers().items() if subject.id in users))


DOCS_AB = [Hit("a", "t", 1.0, {"doc_id": "a"}), Hit("b", "t", 1.0, {"doc_id": "b"})]


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_after_invalidate_a_stale_answer_is_passed_on_but_not_cached(scope):
    clock = Clock()
    source = Lagging(clock, lag=5.0)
    cache = CachingAuthorizer(source, ttl=30, clock=clock)
    assert (cache.source_staleness, cache.max_staleness) == (5.0, 35.0)
    assert cache.check(BOB, DOCS_AB) == [True, False]
    source.revoke("a", "bob")
    cache.invalidate(BOB if scope == "subject" else None)
    clock.now += 1
    assert cache.check(BOB, DOCS_AB) == [True, False]  # the source still answers from before the change,
    clock.now += 3
    assert cache.check(BOB, DOCS_AB) == [True, False]
    assert source.checks == 6  # and is asked every time: nothing it said in the hold was kept
    asked = source.checks
    cache.check(ANN, DOCS_AB)
    cache.check(ANN, DOCS_AB)
    assert source.checks == asked + (2 if scope == "subject" else 4)  # ann is held back only with everyone
    clock.now += 1.5  # the hold has ended, 5 s after invalidate(), and the source has caught up
    assert cache.check(BOB, DOCS_AB) == [False, False]
    clock.now += 20
    asked = source.checks
    assert cache.check(BOB, DOCS_AB) == [False, False]
    assert source.checks == asked  # the answer given after the hold was cached


def test_after_invalidate_a_stale_filter_is_not_cached_either():
    clock = Clock()
    source = Lagging(clock)
    cache = CachingAuthorizer(source, ttl=30, clock=clock)
    assert cache.filter_for(BOB) == In("doc_id", ["a"])
    source.revoke("a", "bob")
    cache.invalidate("bob")
    clock.now += 2
    assert cache.filter_for(BOB) == In("doc_id", ["a"])  # stale, and passed on
    clock.now += 4
    assert cache.filter_for(BOB) == cache.filter_for(BOB) == In("doc_id", [])
    assert source.filters == 3  # the warm-up, the one in the hold, and one after it, then cached


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_the_hold_reaches_every_replica_through_the_shared_cache(scope):
    clock, shared = Clock(), Shared()
    source = Lagging(clock)
    a = CachingAuthorizer(source, ttl=30, shared=shared, namespace=NS, clock=clock)
    # b declares no staleness of its own: it holds back because of the hold a wrote with its epoch.
    b = CachingAuthorizer(source, ttl=30, shared=shared, namespace=NS, source_staleness=0, clock=clock)
    assert b.check(BOB, DOCS_AB) == [True, False]  # in b's own cache and in the shared cache
    source.revoke("a", "bob")
    a.invalidate(BOB if scope == "subject" else None)
    clock.now += 1.5  # b's own copy has expired (local_ttl, 1 s)
    asked = source.checks
    assert b.check(BOB, DOCS_AB) == [True, False]  # stale, from the source,
    clock.now += 0.5
    assert b.check(BOB, DOCS_AB) == [True, False]
    assert source.checks == asked + 4  # each time: kept neither in b nor in the shared cache
    assert all("!epoch" in key for key in shared.data)
    clock.now += 4  # a's hold has ended, and the source has caught up
    assert b.check(BOB, DOCS_AB) == [False, False]
    assert a.check(BOB, DOCS_AB) == [False, False]
    assert source.checks == asked + 6  # a read the answer b wrote after the hold


def test_async_calls_keep_nothing_during_the_hold():
    async def scenario(shared):
        clock = Clock()
        source = Lagging(clock)
        cache = CachingAuthorizer(source, ttl=30, shared=shared, namespace=NS, clock=clock)
        await cache.acheck(BOB, DOCS_AB)
        source.revoke("a", "bob")
        cache.invalidate("bob")
        clock.now += 1
        held = [await cache.acheck(BOB, DOCS_AB), await cache.afilter_for(BOB)]
        held += [await cache.acheck(BOB, DOCS_AB), await cache.afilter_for(BOB)]
        clock.now += 5
        after = [await cache.acheck(BOB, DOCS_AB), await cache.afilter_for(BOB)]
        after += [await cache.acheck(BOB, DOCS_AB), await cache.afilter_for(BOB)]
        return held, after, source.checks, source.filters

    for shared in (None, Shared()):
        held, after, checks, filters = asyncio.run(scenario(shared))
        assert held == [[True, False], In("doc_id", ["a"])] * 2
        assert after == [[False, False], In("doc_id", [])] * 2
        assert (checks, filters) == (2 + 4 + 2, 2 + 1)  # nothing kept in the hold; kept after it


def test_another_replicas_hold_is_kept_by_async_calls_too():
    async def scenario():
        clock, shared = Clock(), Shared()
        source = Lagging(clock)
        a = CachingAuthorizer(source, ttl=30, shared=shared, namespace=NS, clock=clock)
        b = CachingAuthorizer(source, ttl=30, shared=shared, namespace=NS, source_staleness=0, clock=clock)
        a.invalidate("bob")
        answers = [await b.acheck(BOB, DOCS_AB), await b.afilter_for(BOB), await b.acheck(BOB, DOCS_AB)]
        return answers, source.checks, source.filters, shared.data

    answers, checks, filters, data = asyncio.run(scenario())
    assert answers == [[True, False], In("doc_id", ["a"]), [True, False]]
    assert (checks, filters) == (4, 1) and all("!epoch" in key for key in data)


def test_the_staleness_comes_from_the_authorizer_unless_it_is_given():
    lagging = Lagging(Clock(), lag=7)
    assert CachingAuthorizer(MetadataACLAuthorizer()).source_staleness == 0.0  # declares none: current
    assert CachingAuthorizer(lagging).source_staleness == 7.0
    assert CachingAuthorizer(lagging, source_staleness=0).source_staleness == 0.0
    assert CachingAuthorizer(MetadataACLAuthorizer(), source_staleness=2).source_staleness == 2.0
    # Combined, an answer is as old as its oldest part; a cache adds its ttl to what it wraps.
    assert AllOf(TenantAuthorizer(), lagging).max_staleness == 7.0
    assert AnyOf(lagging, MetadataACLAuthorizer()).max_staleness == 7.0
    assert CachingAuthorizer(CachingAuthorizer(lagging, ttl=10), ttl=5).source_staleness == 17.0

    class Odd:
        max_staleness = "soon"

        def check(self, subject, hits, action="read"):
            return [False] * len(hits)

        def filter_for(self, subject, action="read"):
            return None

    with pytest.raises(ValueError, match="staleness"):
        CachingAuthorizer(Odd())
    for bad in (-1, float("nan"), float("inf"), True, "5"):
        with pytest.raises(ValueError, match="staleness"):
            CachingAuthorizer(MetadataACLAuthorizer(), source_staleness=bad)


def test_an_epoch_whose_hold_cannot_be_read_is_a_failed_read():
    shared = Shared()
    CachingAuthorizer(_released(), shared=shared, namespace=NS).invalidate(BOB)
    (key,) = shared.data  # bob's epoch
    for value in (b"0" * 32 + b"/soon", b"0" * 32 + b"/nan", b"0" * 32 + b"/inf", b"0" * 32 + b"/\xff"):
        shared.data[key] = value
        cache = CachingAuthorizer(_released(), shared=shared, namespace=NS)
        assert cache.check(BOB, HITS) == [True, False]  # the authorizer is asked,
        assert cache.stats().shared_failures == 1
    assert list(shared.data) == [key]  # and nothing is written under an epoch that could not be read
    shared.data[key] = b"0" * 32 + b"/1e300"  # a hold that never ends in practice: answers pass, unkept
    held = _released()
    CachingAuthorizer(held, shared=shared, namespace=NS).check(BOB, HITS)
    assert held.checks == 2 and list(shared.data) == [key]


# ---- invalidate() by subject id --------------------------------------------------------------------


BOBS = [Subject("bob", ["eng"], {"tenant": "acme"}), Subject("bob", ["hr"], {"tenant": "globex"})]


def test_invalidate_drops_every_subject_with_the_id_whatever_its_groups_and_attributes():
    held = _released()
    cache = CachingAuthorizer(held)
    for subject in [*BOBS, ANN]:
        cache.check(subject, HITS)
    cache.invalidate("bob")  # the id alone, as a revocation hook usually has it
    for subject in [*BOBS, ANN]:
        cache.check(subject, HITS)
    assert held.checks == 6 + 4  # asked again for both bobs, not for ann


def test_invalidate_by_id_reaches_the_shared_cache():
    shared, two = Shared(), _released()
    a, b = (CachingAuthorizer(held, shared=shared, namespace=NS) for held in (_released(), two))
    for subject in [*BOBS, ANN]:
        a.check(subject, HITS)
    a.invalidate(BOBS[0])  # a Subject works too: every subject with its id is dropped
    for subject in [*BOBS, ANN]:
        b.check(subject, HITS)
    assert two.checks == 4  # the other replica asked for both bobs; ann came from the shared cache


def test_invalidate_refuses_an_empty_id():
    with pytest.raises(ValueError):
        CachingAuthorizer(MetadataACLAuthorizer()).invalidate("")


# ---- revocation under concurrent retrievals ------------------------------------------------------


class Source:
    """The permission service: who may read each document, changed while retrievals run. Each
    answer takes a few milliseconds, so calls are in flight when a permission is revoked."""

    def __init__(self, readers):
        self.readers = {doc: set(users) for doc, users in readers.items()}
        self.lock = threading.Lock()

    def check(self, subject, hits, action):
        with self.lock:
            answer = [subject.id in self.readers.get(h.id, ()) for h in hits]
        time.sleep(0.003)
        return answer

    def revoke(self, doc, user):
        with self.lock:
            self.readers[doc].discard(user)


DOCS = [Document(f"doc-{i}", f"quarterly plan part {i}", {}) for i in range(6)]


def _traffic(retrievers, subject, stop, log):
    def user(retriever):
        while not stop.is_set():
            started = time.monotonic()
            ids = [h.id for h in retriever.retrieve("quarterly plan", subject, 6).hits]
            log.append((started, time.monotonic(), retriever, ids))

    threads = [threading.Thread(target=user, args=(r,)) for r in retrievers for _ in range(4)]
    for t in threads:
        t.start()
    return threads


@pytest.mark.parametrize("replicas", [1, 3])
def test_once_invalidate_returns_a_revoked_document_stops_being_returned(replicas):
    source = Source({d.id: {"bob"} for d in DOCS})
    shared = Shared() if replicas > 1 else None
    local_ttl = 0.1
    # Room for 4 of the 6 decisions a retrieval needs, so every retrieval asks the source about
    # some of them and answers are in flight when the permission is revoked; ttl is long, so an
    # answer kept by mistake would be served for the rest of the test.
    caches = [
        CachingAuthorizer(
            FunctionAuthorizer(source.check), ttl=30, maxsize=4, shared=shared, namespace=NS, local_ttl=local_ttl
        )
        for _ in range(replicas)
    ]
    retrievers = [PermissionedRetriever(InMemoryBackend(DOCS), c, strategy="postfilter") for c in caches]
    bob, stop, log = Subject("bob"), threading.Event(), []
    threads = _traffic(retrievers, bob, stop, log)
    time.sleep(0.3)  # every replica has cached bob's access to every document
    source.revoke("doc-3", "bob")
    caches[0].invalidate(bob)  # the revocation hook runs on one replica
    revoked_at = time.monotonic()
    time.sleep(0.6 + local_ttl)
    stop.set()
    for t in threads:
        t.join(5)

    seen_after = [(started - revoked_at, r) for started, _, r, ids in log if started > revoked_at and "doc-3" in ids]
    # This process: nothing that started after invalidate() returned. Other replicas: they may use
    # their own copies for local_ttl; a late answer written to the shared cache is never read.
    slack = 0.25  # scheduling on a busy CI runner, between an answer and its being kept
    late = [delay for delay, r in seen_after if r is retrievers[0] or delay > local_ttl + slack]
    assert not late, f"doc-3 was returned {max(late):.3f} s after invalidate() returned"
    assert any(started > revoked_at + local_ttl + slack for started, *_ in log)  # traffic kept going
    assert all("doc-2" in ids for *_, ids in log)  # and the permissions not revoked still apply

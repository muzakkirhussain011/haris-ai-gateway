"""CachingAuthorizer with a cache shared by several processes, and RedisCache against a real Redis."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import (
    CachingAuthorizer,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)
from wardowl.caching import RedisCache, SharedCache

NS = "test-policy"  # a shared cache needs a namespace of its own
BOB = Subject("bob", ["eng"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})]


class Counting:
    def __init__(self):
        self.inner = MetadataACLAuthorizer()
        self.checks = self.filters = 0

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return self.inner.filter_for(subject, action)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeShared:
    """A dictionary standing in for Redis; it ignores TTLs, so expiry is the cache's own doing."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.fail = False

    def get_many(self, keys):
        if self.fail:
            raise ConnectionError("down")
        return [self.data.get(k) for k in keys]

    def set_many(self, entries, ttl):
        if self.fail:
            raise ConnectionError("down")
        self.data.update(entries)

    def delete_prefix(self, prefix):
        if self.fail:
            raise ConnectionError("down")
        for key in [k for k in self.data if k.startswith(prefix)]:
            del self.data[key]


class IndexedShared(FakeShared):
    """FakeShared with the index RedisCache keeps: the names written for each subject id. Records
    every delete_prefix (a scan, in Redis), and can run `between` once inside a write, after the
    names are listed and before the entries exist, as RedisCache's pipeline orders them."""

    def __init__(self):
        super().__init__()
        self.indexes: dict[str, set[str]] = {}
        self.prefixes: list[str] = []
        self.between = None

    def set_many_indexed(self, entries, ttl, index):
        if self.fail:
            raise ConnectionError("down")
        self.indexes.setdefault(index, set()).update(entries)
        if self.between is not None:
            between, self.between = self.between, None
            between()
        self.data.update(entries)

    def delete_indexed(self, index, prefix):
        if self.fail:
            raise ConnectionError("down")
        for key in self.indexes.get(index, ()):
            if key.startswith(prefix):
                self.data.pop(key, None)

    def delete_prefix(self, prefix):
        self.prefixes.append(prefix)
        super().delete_prefix(prefix)


class Unindexed:
    """A shared cache used as wardowl 3.0 used it, with no index: what a replica not yet upgraded writes."""

    def __init__(self, store):
        self.store = store

    def get_many(self, keys):
        return self.store.get_many(keys)

    def set_many(self, entries, ttl):
        self.store.set_many(entries, ttl)

    def delete_prefix(self, prefix):
        self.store.delete_prefix(prefix)


def _hash_tag(key):
    """What Redis Cluster hashes for a key: the text between its first "{" and the next "}", if any."""
    start = key.find("{")
    end = key.find("}", start + 1)
    return key[start + 1 : end] if start >= 0 and end > start + 1 else key


def _replicas(shared, clock=None, ttl=30.0):
    clock = clock or Clock()
    inners = [Counting(), Counting()]
    return clock, inners, [CachingAuthorizer(i, ttl=ttl, shared=shared, namespace=NS, clock=clock) for i in inners]


def test_fake_shared_is_a_shared_cache():
    assert isinstance(FakeShared(), SharedCache)


def test_a_second_replica_uses_the_first_ones_answers():
    shared = FakeShared()
    _, (one, two), (a, b) = _replicas(shared)
    assert a.check(BOB, HITS) == [True, False] and a.filter_for(BOB) is not None
    assert b.check(BOB, HITS) == [True, False] and b.filter_for(BOB) == a.filter_for(BOB)
    assert (one.checks, one.filters) == (2, 1) and (two.checks, two.filters) == (0, 0)
    # Key names reveal neither the subject nor the chunks.
    assert not any("bob" in k or "allowed_groups" in k for k in shared.data)


def test_the_revocation_bound_holds_across_replicas():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)  # written at t=1000, expires at t=1030
    clock.now += 25
    b.check(BOB, HITS)  # copied into b's own cache with 5 seconds left, not a fresh 30
    clock.now += 6
    b.check(BOB, HITS)
    assert two.checks == 2  # asked again: the shared entry and b's copy have both expired


def test_a_copied_entry_never_outlives_the_ttl_whatever_expiry_it_carries():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS[:1])
    (key,) = shared.data
    entry = json.loads(shared.data[key])
    shared.data[key] = json.dumps({**entry, "expires": entry["expires"] + 10**9}).encode()
    b.check(BOB, HITS[:1])  # b copies it, for at most ttl
    shared.data.clear()  # (Redis drops it after the writer's ttl; signing_key rejects planted ones)
    clock.now += 31
    b.check(BOB, HITS[:1])
    assert two.checks == 1  # b's copy expired after ttl, so b asked again


def test_invalidate_reaches_every_replica():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    a.check(Subject("ann", ["hr"]), HITS)
    a.invalidate(BOB)
    b.check(BOB, HITS)
    b.check(Subject("ann", ["hr"]), HITS)
    assert two.checks == 2  # bob was asked again, ann still came from the shared cache
    a.invalidate()
    # Every entry is gone; the epochs stay (everyone's and bob's), and refuse late writes.
    assert len(shared.data) == 2 and all(key.startswith(f"{NS}!epoch") for key in shared.data)


def test_invalidate_reaches_a_replica_holding_its_own_copy_within_local_ttl():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(BOB, HITS)  # b now holds its own copy, for at most local_ttl (1 second)
    a.invalidate(BOB)
    clock.now += 0.5
    b.check(BOB, HITS)
    assert two.checks == 0  # still b's own copy
    clock.now += 0.6
    b.check(BOB, HITS)
    assert two.checks == 2  # its copy expired and the shared entry is gone, so b asked


def test_invalidating_everyone_reaches_a_replica_within_local_ttl_even_before_the_entries_are_deleted():
    class Lagging(FakeShared):
        """Deletes nothing: Redis's SCAN has not reached the entries yet."""

        def delete_prefix(self, prefix):
            pass

    shared = Lagging()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(Subject("ann", ["hr"]), HITS)  # b reads the global epoch now, at t=1000
    asked = two.checks
    clock.now += 0.9
    a.invalidate()
    b.check(BOB, HITS)  # b's epoch is 0.9 s old, so it still accepts bob's entry, and copies it
    assert two.checks == asked
    clock.now += 0.6
    b.check(BOB, HITS)
    assert two.checks == asked + 2  # its copy expired with its epoch, 1 s after reading it, not 1.9 s


def test_a_global_epoch_read_in_flight_cannot_put_back_the_epoch_invalidate_replaced():
    class Racing(FakeShared):
        during = None

        def get_many(self, keys):
            values = super().get_many(keys)
            if self.during is not None and keys == [f"{NS}!epoch"]:
                during, self.during = self.during, None
                during()  # invalidate() runs after the old epoch was read, before it is kept
            return values

    shared = Racing()
    clock, (one, _two), (a, b) = _replicas(shared)
    a.invalidate()
    b.check(BOB, HITS)  # written under that epoch
    old = {key: value for key, value in shared.data.items() if "!epoch" not in key}
    clock.now += 2  # a's copy of the global epoch is old, so its next lookup reads it again
    shared.during = a.invalidate
    a.check(BOB, HITS)
    shared.data.update(old)  # answers from before the second invalidate(), put back
    asked = one.checks
    a.check(BOB, HITS)
    assert one.checks == asked + 2  # a kept the new epoch, so it refused the old answers


def test_invalidating_everyone_never_deletes_the_epoch_of_an_id():
    shared = FakeShared()
    _, _, (a, _b) = _replicas(shared)
    a.invalidate(BOB)
    bobs = {key: value for key, value in shared.data.items() if key != f"{NS}!epoch"}
    a.invalidate()  # while it deletes, an invalidate() of bob may have just written bob's epoch
    assert bobs and bobs.items() <= shared.data.items()


def test_epochs_are_text_that_a_redis_client_decoding_responses_can_read():
    shared = FakeShared()
    _, _, (a, _b) = _replicas(shared)
    a.invalidate(BOB)
    a.invalidate()
    assert all(value.decode("ascii").isalnum() for value in shared.data.values())  # only epochs are left


def test_invalidate_refuses_an_id_of_another_type():
    with pytest.raises(TypeError):
        CachingAuthorizer(Counting(), shared=FakeShared(), namespace=NS).invalidate(42)  # "42" would be missed


def test_without_a_shared_cache_entries_live_for_ttl():
    clock, inner = Clock(), Counting()
    cache = CachingAuthorizer(inner, ttl=30, local_ttl=1, clock=clock)
    cache.check(BOB, HITS)
    clock.now += 29
    cache.check(BOB, HITS)
    assert inner.checks == 2  # local_ttl only applies next to a shared cache


@pytest.mark.parametrize(
    "options",
    [
        {"ttl": 0},
        {"local_ttl": 0},
        {"maxsize": 0},
        {"namespace": ""},
        {"namespace": None},  # a shared cache needs one
        {"ttl": float("nan")},  # an entry would never expire: NaN never compares as past
        {"ttl": float("inf")},
        {"local_ttl": float("nan")},
    ],
)
def test_invalid_options_are_refused(options):
    with pytest.raises(ValueError):
        CachingAuthorizer(Counting(), shared=FakeShared(), **{"namespace": NS, **options})


def test_two_kinds_of_authorizer_never_share_a_namespace_of_one_shared_cache():
    # AllOf(CachingAuthorizer(TenantAuthorizer(), shared=r), CachingAuthorizer(OpenFGAAuthorizer(...),
    # shared=r)) once put both in the default namespace, where each answered with the other's decisions.
    shared = FakeShared()
    CachingAuthorizer(TenantAuthorizer(), shared=shared, namespace="tenant")
    with pytest.raises(ValueError, match="namespace 'tenant'"):
        CachingAuthorizer(MetadataACLAuthorizer(), shared=shared, namespace="tenant")
    CachingAuthorizer(MetadataACLAuthorizer(), shared=shared, namespace="acl")  # a namespace of its own
    CachingAuthorizer(TenantAuthorizer(), shared=shared, namespace="tenant")  # another copy of the same one

    class Client:  # stands in for a redis.Redis: two RedisCache objects around it are one shared cache
        pass

    client = Client()
    CachingAuthorizer(TenantAuthorizer(), shared=RedisCache(client), namespace="tenant")
    with pytest.raises(ValueError, match="namespace 'tenant'"):
        CachingAuthorizer(MetadataACLAuthorizer(), shared=RedisCache(client), namespace="tenant")


@pytest.mark.parametrize("expires", [float("nan"), float("inf")])
def test_an_expiry_that_is_not_a_finite_number_is_a_miss(expires):
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS[:1])
    (key,) = shared.data
    entry = json.loads(shared.data[key])
    shared.data[key] = json.dumps({**entry, "expires": expires}).encode()  # JSON carries NaN and Infinity
    b.check(BOB, HITS[:1])  # a miss: b asks, and keeps its own answer for local_ttl
    clock.now += 10**6
    shared.data.clear()
    b.check(BOB, HITS[:1])
    assert two.checks == 2  # with NaN, a copy of the entry would never have expired here


@pytest.mark.parametrize(
    "blob",
    [
        b"not json",
        b"{}",
        b'{"expires": "soon", "epoch": [null, null], "value": true}',
        b'{"expires": 1e12, "epoch": [null, null], "value": "yes"}',
        b'{"expires": 1e12, "value": true}',  # no epoch: written by an older version, or planted
        # An integer too large for a float: float() raises OverflowError, which used to escape the read.
        pytest.param(
            b'{"expires": 1' + b"0" * 400 + b', "epoch": [null, null], "value": true}', id="expires-too-large"
        ),
        # Nested too deep for the JSON decoder. A short id: pytest puts the test id in an environment
        # variable, and Windows refuses one longer than 32767 characters.
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-too-deep"),
    ],
)
def test_unreadable_entries_are_misses(blob):
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS[:1])
    shared.data = dict.fromkeys(shared.data, blob)
    assert b.check(BOB, HITS[:1]) == [True] and two.checks == 1


def test_a_malformed_shared_filter_is_a_miss():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.filter_for(BOB)
    blob = b'{"expires": 1e12, "epoch": [null, null], "value": {"filter": {"in": "nonsense"}}}'
    shared.data = dict.fromkeys(shared.data, blob)
    assert b.filter_for(BOB) is not None and two.filters == 1


def test_a_failing_shared_cache_never_changes_an_answer():
    shared = FakeShared()
    _, (one, _two), (a, _b) = _replicas(shared)
    shared.fail = True
    assert a.check(BOB, HITS) == [True, False] and a.filter_for(BOB) is not None
    assert one.checks == 2
    with pytest.raises(ConnectionError):
        a.invalidate(BOB)  # the caller asked for a revocation now: its failure is reported


def test_async_paths_use_the_shared_cache():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    asyncio.run(a.acheck(BOB, HITS))
    asyncio.run(a.afilter_for(BOB))
    assert asyncio.run(b.acheck(BOB, HITS)) == [True, False]
    assert asyncio.run(b.afilter_for(BOB)) == a.filter_for(BOB)
    assert (two.checks, two.filters) == (0, 0)


# ---- invalidate(subject) through the index of a subject's entries ---------------------------------

ANN = Subject("ann", ["hr"])


def test_invalidating_a_subject_deletes_the_entries_its_index_lists_and_scans_nothing():
    shared = IndexedShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    a.filter_for(BOB)
    a.check(Subject("bob", ["hr"]), HITS[:1])  # the same id with other groups: in the same index
    bobs = set(shared.data)
    a.check(ANN, HITS)
    anns = set(shared.data) - bobs
    assert (len(bobs), len(anns), len(shared.indexes)) == (4, 2, 2)  # one index per subject id
    a.invalidate(BOB)
    assert shared.prefixes == []  # nothing scanned for a subject
    assert {key for key in shared.data if "!epoch" not in key} == anns  # bob's entries deleted, ann's kept
    b.check(BOB, HITS)
    b.check(ANN, HITS)
    assert two.checks == 2  # bob was asked again, ann came from the shared cache
    a.invalidate()
    assert shared.prefixes == [f"{NS}:"]  # everyone: the one scan
    assert all("!epoch" in key for key in shared.data)


def test_entries_written_without_an_index_are_refused_after_invalidate_and_deleted_by_invalidating_everyone():
    # A rolling upgrade: a replica still on 3.0 writes entries that no index lists.
    shared = IndexedShared()
    clock = Clock()
    old_inner, third_inner = Counting(), Counting()
    old = CachingAuthorizer(old_inner, shared=Unindexed(shared), namespace=NS, clock=clock)
    new = CachingAuthorizer(Counting(), shared=shared, namespace=NS, clock=clock)
    old.check(BOB, HITS)
    legacy = dict(shared.data)
    assert len(legacy) == 2 and not shared.indexes
    new.invalidate(BOB)
    assert legacy.items() <= shared.data.items()  # not deleted: no index lists them...
    CachingAuthorizer(third_inner, shared=shared, namespace=NS, clock=clock).check(BOB, HITS)
    assert third_inner.checks == 2  # ...but an upgraded replica refuses them, under the new epoch
    shared.data.update(legacy)  # (it wrote its own answers under the same names)
    clock.now += 1.1  # old's own copies expire, so it reads the shared cache again
    old.check(BOB, HITS)
    assert old_inner.checks == 4  # and so does the replica not yet upgraded
    new.invalidate()
    assert all("!epoch" in key for key in shared.data)  # the scan of everyone deletes them


def test_an_entry_written_while_invalidate_runs_stays_listed_and_is_never_read():
    shared = IndexedShared()
    clock, (_one, _two), (a, b) = _replicas(shared)
    # b's invalidate() runs after a listed its names and before a wrote its entries, which a's
    # answer, obtained before the invalidation, carries the old epoch for.
    shared.between = lambda: b.invalidate(BOB)
    a.check(BOB, HITS)
    late = {key for key in shared.data if "!epoch" not in key}
    assert len(late) == 2 and late <= set().union(*shared.indexes.values())
    third_inner = Counting()
    CachingAuthorizer(third_inner, shared=shared, namespace=NS, clock=clock).check(BOB, HITS)
    assert third_inner.checks == 2  # not read
    b.invalidate(BOB)
    assert not late & set(shared.data)  # listed, so the next invalidation deletes them


def _bobs_names():
    """The names of everything a replica keeps in the shared cache for bob: entries, epoch, index."""
    shared = IndexedShared()
    _, _, (a, _b) = _replicas(shared)
    a.check(BOB, HITS)
    a.filter_for(BOB)
    a.check(Subject("bob", ["hr"]), HITS[:1])
    entries = set(shared.data)
    a.invalidate(BOB)  # writes bob's epoch
    names = entries | set(shared.data) | set(shared.indexes)
    assert len(names) == 6  # four entries, the epoch and the index
    return names


def test_a_subjects_entries_epoch_and_index_share_one_cluster_hash_tag():
    # Redis Cluster runs a pipeline or a multi-key command only on keys of one slot.
    assert len({_hash_tag(name) for name in _bobs_names()}) == 1


def test_a_subjects_entries_epoch_and_index_share_one_cluster_slot_by_redis_py():
    key_slot = pytest.importorskip("redis.crc").key_slot
    assert len({key_slot(name.encode()) for name in _bobs_names()}) == 1


def test_ainvalidate_clears_redis_in_a_worker_thread_while_the_event_loop_runs():
    class Slow(IndexedShared):
        def __init__(self):
            super().__init__()
            self.entered, self.release = threading.Event(), threading.Event()
            self.threads: set[int] = set()

        def set_many(self, entries, ttl):  # in the index's path, only epochs are written this way
            self.threads.add(threading.get_ident())
            super().set_many(entries, ttl)

        def delete_indexed(self, index, prefix):
            self.threads.add(threading.get_ident())
            self.entered.set()
            assert self.release.wait(5), "the event loop never ran while Redis was being cleared"
            super().delete_indexed(index, prefix)

    shared = Slow()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    a.check(ANN, HITS)

    async def main():
        task = asyncio.create_task(a.ainvalidate(BOB))
        while not shared.entered.is_set():  # this loop runs while the worker thread waits on Redis
            await asyncio.sleep(0.01)
        shared.release.set()
        await task
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert shared.threads and loop_thread not in shared.threads
    b.check(BOB, HITS)
    b.check(ANN, HITS)
    assert two.checks == 2  # bob's entries are gone, ann's are not


def test_ainvalidate_finishes_in_its_thread_when_the_task_awaiting_it_is_cancelled():
    class Slow(IndexedShared):
        def __init__(self):
            super().__init__()
            self.entered, self.release = threading.Event(), threading.Event()

        def delete_indexed(self, index, prefix):
            self.entered.set()
            assert self.release.wait(5)
            super().delete_indexed(index, prefix)

    shared = Slow()
    _, _, (a, _b) = _replicas(shared)
    a.check(BOB, HITS)

    async def main():
        task = asyncio.create_task(a.ainvalidate(BOB))
        while not shared.entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        shared.release.set()

    asyncio.run(main())  # which waits for the worker thread before it returns
    assert all("!epoch" in key for key in shared.data)


@pytest.mark.parametrize("make", [FakeShared, IndexedShared], ids=["prefix", "index"])
def test_ainvalidate_refuses_and_raises_as_invalidate_does(make):
    shared = make()
    _, _, (a, _b) = _replicas(shared)
    with pytest.raises(TypeError):
        asyncio.run(a.ainvalidate(42))
    with pytest.raises(ValueError):
        asyncio.run(a.ainvalidate(""))
    shared.fail = True
    with pytest.raises(ConnectionError):
        asyncio.run(a.ainvalidate(BOB))  # the caller asked for a revocation now: its failure is reported


def test_ainvalidate_without_a_shared_cache_drops_this_processs_answers():
    inner = Counting()
    cache = CachingAuthorizer(inner)
    cache.check(BOB, HITS)
    asyncio.run(cache.ainvalidate(BOB.id))
    cache.check(BOB, HITS)
    assert inner.checks == 4


@settings(max_examples=80, deadline=None)
@given(corpora(max_size=15), subjects, queries, st.integers(1, 5), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_sharing_never_changes_the_answer(docs, subject, query, k, strategy):
    shared = FakeShared()
    for _ in range(2):  # the second replica is served from the shared cache
        authz = CachingAuthorizer(MetadataACLAuthorizer(), shared=shared, namespace=NS)
        retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + k)
        assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


# ---- RedisCache against a real Redis -------------------------------------------------------

REDIS_URL = os.environ.get("WARDOWL_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="set WARDOWL_TEST_REDIS_URL to run against Redis")


def _redis():
    redis = pytest.importorskip("redis")
    return redis.Redis.from_url(REDIS_URL)


def test_a_short_signing_key_is_refused():
    with pytest.raises(ValueError):
        RedisCache(None, signing_key=b"short")
    with pytest.raises(TypeError, match="bytes"):
        RedisCache(None, signing_key="a string of more than sixteen characters")


@needs_redis
def test_redis_cache_shares_expires_and_deletes_by_prefix():
    client, ns = _redis(), f"wo-{uuid.uuid4().hex}"
    cache = RedisCache(client)
    cache.set_many({f"{ns}:a": b"1", f"{ns}:b": b"2", f"{ns}x:c": b"3"}, ttl=30)
    assert cache.get_many([f"{ns}:a", f"{ns}:b", f"{ns}:missing"]) == [b"1", b"2", None]
    assert 0 < client.pttl(f"{ns}:a") <= 30_000
    cache.delete_prefix(f"{ns}:")
    assert cache.get_many([f"{ns}:a", f"{ns}:b", f"{ns}x:c"]) == [None, None, b"3"]
    cache.delete_prefix(f"{ns}x:")


@needs_redis
def test_redis_cache_ignores_unsigned_tampered_and_foreign_entries():
    client, ns = _redis(), f"wo-{uuid.uuid4().hex}"
    cache = RedisCache(client, signing_key=b"k" * 32)
    cache.set_many({f"{ns}:a": b"true"}, ttl=30)
    assert cache.get_many([f"{ns}:a"]) == [b"true"]
    client.set(f"{ns}:b", b"true")  # written without the key
    signed = client.get(f"{ns}:a")
    client.set(f"{ns}:c", signed)  # a valid value copied under another key
    client.set(f"{ns}:d", signed[:-4] + b"fals")  # tampered
    assert cache.get_many([f"{ns}:b", f"{ns}:c", f"{ns}:d"]) == [None, None, None]
    assert RedisCache(client, signing_key=b"z" * 32).get_many([f"{ns}:a"]) == [None]
    cache.delete_prefix(f"{ns}:")


@needs_redis
def test_replicas_share_decisions_through_redis():
    namespace, clock = f"wo-{uuid.uuid4().hex}", Clock()
    clock.now = time.time()  # shared entries carry wall-clock expiry times
    shared = RedisCache(_redis(), signing_key=b"s" * 32)
    one, two = Counting(), Counting()
    a = CachingAuthorizer(one, shared=shared, namespace=namespace, clock=clock)
    b = CachingAuthorizer(two, shared=shared, namespace=namespace, clock=clock)
    assert a.check(BOB, HITS) == [True, False] and b.check(BOB, HITS) == [True, False]
    first = a.filter_for(BOB)  # a asks first; b then finds it in Redis
    assert b.filter_for(BOB) == first and (two.checks, two.filters) == (0, 0)
    a.invalidate(BOB)
    clock.now += 1.1  # b's own copy has expired (local_ttl), and Redis no longer has the entry
    b.check(BOB, HITS)
    assert two.checks == 2
    a.invalidate()


@needs_redis
@pytest.mark.parametrize("decoding", [False, True])
def test_the_hold_of_an_invalidation_reaches_replicas_through_redis(decoding):
    # The epoch carries when the hold ends, signed like any value, and readable by a client that
    # decodes responses: replica b keeps nothing for bob until then, though it declares no staleness.
    redis = pytest.importorskip("redis")
    namespace, clock = f"wo-{uuid.uuid4().hex}", Clock()
    clock.now = time.time()  # shared entries and holds carry wall-clock times
    client = redis.Redis.from_url(REDIS_URL, decode_responses=decoding)
    shared = RedisCache(client, signing_key=b"s" * 32)
    one, two = Counting(), Counting()
    a = CachingAuthorizer(one, shared=shared, namespace=namespace, source_staleness=5, clock=clock)
    b = CachingAuthorizer(two, shared=shared, namespace=namespace, clock=clock)
    a.invalidate(BOB)
    b.check(BOB, HITS)
    b.check(BOB, HITS)
    assert two.checks == 4  # asked both times: nothing kept during the hold
    clock.now += 5.5
    b.check(BOB, HITS)
    b.check(BOB, HITS)
    assert two.checks == 6  # after it, kept as usual
    a.invalidate()
    shared.delete_prefix(f"{namespace}!")  # the epochs, which outlive the test otherwise


def _no_scan(*args, **kwargs):
    raise AssertionError("invalidate() of one subject scanned the database")


@needs_redis
def test_redis_invalidate_of_one_subject_deletes_through_its_index_without_a_scan(monkeypatch):
    client, namespace, clock = _redis(), f"wo-{uuid.uuid4().hex}", Clock()
    clock.now = time.time()  # shared entries carry wall-clock expiry times
    shared = RedisCache(client, signing_key=b"s" * 32)
    one, two = Counting(), Counting()
    a = CachingAuthorizer(one, shared=shared, namespace=namespace, clock=clock)
    b = CachingAuthorizer(two, shared=shared, namespace=namespace, clock=clock)
    ann = Subject("ann", ["hr"])
    a.check(BOB, HITS)
    a.filter_for(BOB)
    bobs = set(client.scan_iter(match=f"{namespace}:*"))
    a.check(ann, HITS)
    anns = set(client.scan_iter(match=f"{namespace}:*")) - bobs
    indexes = list(client.scan_iter(match=f"{namespace}!index:*"))
    assert (len(bobs), len(anns), len(indexes)) == (3, 2, 2)
    assert all(30_000 < client.pttl(index) <= 90_000 for index in indexes)  # ttl plus a minute
    assert all(len(client.zrange(index, 0, -1)) in (2, 3) for index in indexes)
    monkeypatch.setattr(client, "scan_iter", _no_scan)
    a.invalidate(BOB)
    monkeypatch.undo()
    assert [client.exists(key) for key in sorted(bobs | anns)] == [key in anns for key in sorted(bobs | anns)]
    b.check(BOB, HITS)
    b.check(ann, HITS)
    assert two.checks == 2  # bob was asked again, ann came from Redis
    a.invalidate()  # everyone: a scan, which deletes every entry
    assert not list(client.scan_iter(match=f"{namespace}:*"))
    shared.delete_prefix(f"{namespace}!")  # the epochs and indexes, which outlive the test otherwise


@needs_redis
def test_redis_entries_written_without_an_index_are_refused_then_deleted_by_invalidating_everyone(monkeypatch):
    client, namespace, clock = _redis(), f"wo-{uuid.uuid4().hex}", Clock()
    clock.now = time.time()
    shared = RedisCache(client, signing_key=b"s" * 32)
    old = CachingAuthorizer(Counting(), shared=Unindexed(shared), namespace=namespace, clock=clock)
    new = CachingAuthorizer(Counting(), shared=shared, namespace=namespace, clock=clock)
    old.check(BOB, HITS)  # as a replica still on 3.0 writes them: no index
    legacy = set(client.scan_iter(match=f"{namespace}:*"))
    assert len(legacy) == 2 and not list(client.scan_iter(match=f"{namespace}!index:*"))
    monkeypatch.setattr(client, "scan_iter", _no_scan)
    new.invalidate(BOB)
    monkeypatch.undo()
    assert all(client.exists(key) for key in legacy)  # not deleted, as no index lists them...
    third = Counting()
    CachingAuthorizer(third, shared=shared, namespace=namespace, clock=clock).check(BOB, HITS)
    assert third.checks == 2  # ...but refused, under the epoch the invalidation wrote
    new.invalidate()
    assert not list(client.scan_iter(match=f"{namespace}:*"))
    shared.delete_prefix(f"{namespace}!")


@needs_redis
def test_redis_index_drops_long_expired_names_and_deletes_only_under_its_prefix():
    client, ns = _redis(), f"wo-{uuid.uuid4().hex}"
    cache = RedisCache(client)
    index, a, b, old = f"{ns}!index:{{t}}", f"{ns}:{{t}}:a", f"{ns}:{{t}}:b", f"{ns}:{{t}}:old"
    client.zadd(index, {old: int(time.time() * 1000) - 61_000})  # its entry expired over a minute ago
    cache.set_many_indexed({a: b"1", b: b"2"}, ttl=30, index=index)
    assert sorted(client.zrange(index, 0, -1)) == [a.encode(), b.encode()]
    assert 30_000 < client.pttl(index) <= 90_000 and 0 < client.pttl(a) <= 30_000
    client.set(f"{ns}x:other", b"3")
    client.zadd(index, {f"{ns}x:other": time.time() * 1000})  # a name someone else put there
    cache.delete_indexed(index, f"{ns}:{{t}}:")
    assert cache.get_many([a, b, f"{ns}x:other"]) == [None, None, b"3"]
    assert client.zcard(index) == 3  # the names stay until they expire from the index
    client.delete(index, f"{ns}x:other")


def test_a_shared_filter_nested_too_deep_is_a_miss():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.filter_for(BOB)
    deep = '{"in": {"field": "team", "values": ["a"]}}'
    for _ in range(150):
        deep = '{"and": [' + deep + "]}"
    entry = '{"expires": 1e12, "epoch": [null, null], "value": {"filter": ' + deep + "}}"
    shared.data = dict.fromkeys(shared.data, entry.encode())
    assert b.filter_for(BOB) is not None and two.filters == 1


def test_stats_count_shared_hits_and_failures():
    shared = FakeShared()
    _, (_one, _two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(BOB, HITS)  # both answers from the shared cache
    assert (b.stats().shared_hits, b.stats().misses) == (2, 0)
    shared.fail = True
    c = CachingAuthorizer(Counting(), shared=shared, namespace=NS)
    c.check(BOB, HITS)  # the read fails, so the authorizer is asked, and nothing is written
    assert (c.stats().shared_failures, c.stats().misses) == (1, 2)

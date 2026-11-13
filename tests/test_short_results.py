"""Short results: telling a small readable set from an index that missed, and repairing a miss."""

from __future__ import annotations

import asyncio
import time

import pytest
from helpers import corpora, may_read, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import (
    AuthorizationError,
    Document,
    FunctionAuthorizer,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    SearchError,
    Subject,
    matches,
)

BOB = Subject("bob", ["eng"])
ANN = Subject("ann", ["hr"])

# Every chunk mentions leave. Bob may read four of them, Ann eight.
DOCS = [
    *(Document(f"pub-{i}", "leave policy", {"allowed_groups": ["*"]}) for i in range(2)),
    *(Document(f"eng-{i}", "leave leave rota", {"allowed_groups": ["eng"]}) for i in range(2)),
    *(Document(f"hr-{i}", "leave payouts", {"allowed_groups": ["hr"]}) for i in range(6)),
]
BOBS = ["eng-0", "eng-1", "pub-0", "pub-1"]  # best first


class Lossy:
    """An approximate index over the in-memory backend's answers: under a filter it returns only its
    first `keep` hits, as an index that stopped early does. Like pgvector and Qdrant, it counts the
    chunks a filter matches and searches exactly when asked. It records what it was asked."""

    supports_filters = True
    supports_exact = True

    def __init__(self, docs, keep=1):
        self.docs = list(docs)
        self.store = InMemoryBackend(self.docs)
        self.keep = keep
        self.calls = []

    def search(self, query, k, filter=None, *, exact=False):
        self.calls.append("exact" if exact else "search")
        hits = self.store.search(query, k, filter)
        return hits if exact or filter is None else hits[: self.keep]

    def count(self, filter, limit):
        self.calls.append(("count", limit))
        return min(sum(matches(filter, d.metadata) for d in self.docs), limit)  # it may stop at the limit


class Inexact(Lossy):
    supports_exact = False


class Uncounted(Lossy):
    count = None  # no count at all


class Keyword(Lossy):
    def count(self, filter, limit):
        self.calls.append(("count", limit))
        return None  # as for a keyword search: a count of the filter's chunks proves nothing about it


class AsyncLossy(Lossy):
    async def asearch(self, query, k, filter=None, *, exact=False):
        self.calls.append("asearch")
        return self.search(query, k, filter, exact=exact)

    async def acount(self, filter, limit):
        self.calls.append("acount")
        return self.count(filter, limit)


def _both(retriever, subject, k, query="leave"):
    """What `retrieve` returns, once `aretrieve` has returned the same."""
    result = retriever.retrieve(query, subject, k)
    assert asyncio.run(retriever.aretrieve(query, subject, k)) == result
    return result


def _ids(result):
    return [h.id for h in result.hits]


# ---- report, the default -----------------------------------------------------------------------


def test_by_default_a_short_result_is_reported_and_nothing_else_runs():
    backend = Lossy(DOCS)
    result = _both(PermissionedRetriever(backend, MetadataACLAuthorizer()), BOB, 5)
    assert _ids(result) == ["eng-0"] and result.complete is False
    assert (result.shortfall, result.fallback, result.sampled_recall) == ("unknown", None, None)
    assert backend.calls == ["search"] * 2  # one search for each driver, as before


def test_a_full_result_and_a_post_filtered_one_have_no_shortfall():
    full = _both(PermissionedRetriever(Lossy(DOCS, keep=5), MetadataACLAuthorizer(), short_results="exact"), BOB, 2)
    assert _ids(full) == ["eng-0", "eng-1"] and full.complete is True
    assert full.shortfall is None and full.fallback is None
    only_check = FunctionAuthorizer(MetadataACLAuthorizer().check)  # no filter: post-filtering
    post = _both(PermissionedRetriever(Lossy(DOCS), only_check, short_results="exact"), BOB, 5)
    assert post.strategy == "postfilter" and _ids(post) == BOBS
    assert post.shortfall is None and post.fallback is None


def test_a_short_result_from_an_exact_backend_is_exhausted_without_a_count():
    class Exact(InMemoryBackend):
        def count(self, filter, limit):
            raise AssertionError("an exact search needs no count")

    result = _both(PermissionedRetriever(Exact(DOCS), MetadataACLAuthorizer(), short_results="exact"), BOB, 5)
    assert _ids(result) == BOBS
    assert (result.shortfall, result.fallback, result.complete) == ("exhausted", None, True)


# ---- count -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("keep", "shortfall", "complete"), [(1, "missed", False), (4, "exhausted", True)])
def test_count_tells_a_small_readable_set_from_a_miss(keep, shortfall, complete):
    # Bob may read four chunks. An index that returned all four found everything he may read; one
    # that returned one missed three. Without the count the two look the same.
    backend = Lossy(DOCS, keep)
    result = _both(PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="count"), BOB, 5)
    assert _ids(result) == BOBS[:keep] and result.shortfall == shortfall and result.complete is complete
    assert result.fallback is None and backend.calls == ["search", ("count", 5)] * 2


def test_an_exhausted_result_is_complete_and_not_searched_again():
    backend = Lossy(DOCS, keep=4)
    result = _both(PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="exact"), BOB, 5)
    assert _ids(result) == BOBS and (result.shortfall, result.fallback, result.complete) == ("exhausted", None, True)
    assert backend.calls == ["search", ("count", 10_001)] * 2


# ---- exact -------------------------------------------------------------------------------------


def test_exact_repairs_a_miss_with_an_exact_search_under_the_same_filter():
    backend = Lossy(DOCS)
    result = _both(PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="exact"), BOB, 5)
    assert _ids(result) == oracle_top_k(DOCS, BOB, "leave", 5) == BOBS
    assert (result.shortfall, result.fallback, result.complete) == ("missed", "exact", True)
    assert result.violations == 0 and result.examined == 4
    # The count goes one past max_exact_rows (10,000 by default), which bounds the exact search.
    assert backend.calls == ["search", ("count", 10_001), "exact"] * 2


def test_an_exact_search_is_bounded_by_max_exact_rows():
    backend = Lossy(DOCS)
    bounded = PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="exact", max_exact_rows=3)
    result = _both(bounded, BOB, 5)
    assert _ids(result) == ["eng-0"] and result.complete is False
    assert (result.shortfall, result.fallback) == ("missed", "over_limit")
    assert backend.calls == ["search", ("count", 5)] * 2  # k=5 is past the bound of 3
    unbounded = PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer(), short_results="exact", max_exact_rows=None)
    result = _both(unbounded, BOB, 5)
    assert result.fallback == "exact" and result.complete is True


def test_a_count_that_stops_at_k_still_leads_to_an_exact_search_without_a_bound():
    # Ann may read eight chunks; counting stops at k=5, more than the one hit that came back.
    backend = Lossy(DOCS)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="exact", max_exact_rows=None)
    result = _both(retriever, ANN, 5)
    assert _ids(result) == oracle_top_k(DOCS, ANN, "leave", 5) and result.complete is True
    assert backend.calls == ["search", ("count", 5), "exact"] * 2


@pytest.mark.parametrize(
    ("backend", "calls", "shortfall"),
    [
        (Inexact, ["search", ("count", 5)], "missed"),
        (Uncounted, ["search"], "unknown"),
        (Keyword, ["search", ("count", 10_001)], "unknown"),
    ],
    ids=["no exact search", "no count", "a count of None"],
)
def test_a_backend_that_cannot_repair_keeps_the_first_hits_and_says_why(backend, calls, shortfall):
    store = backend(DOCS)
    result = _both(PermissionedRetriever(store, MetadataACLAuthorizer(), short_results="exact"), BOB, 5)
    assert _ids(result) == ["eng-0"] and result.complete is False
    assert (result.shortfall, result.fallback) == (shortfall, "unsupported")
    assert store.calls == calls * 2


def test_with_a_reranker_the_count_covers_its_depth():
    backend = Lossy(DOCS)
    retriever = PermissionedRetriever(
        backend, MetadataACLAuthorizer(), short_results="count", reranker=lambda q, hits: hits, rerank_depth=10
    )
    result = _both(retriever, BOB, 2)
    assert backend.calls == ["search", ("count", 10)] * 2
    assert (result.shortfall, result.complete) == ("missed", False)


def test_aretrieve_uses_native_asearch_and_acount():
    backend = AsyncLossy(DOCS)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="exact")
    result = asyncio.run(retriever.aretrieve("leave", BOB, 5))
    assert _ids(result) == BOBS and result.fallback == "exact" and result.complete is True
    assert backend.calls == ["asearch", "search", "acount", ("count", 10_001), "asearch", "exact"]


# ---- widen -------------------------------------------------------------------------------------


def test_widen_searches_again_with_wider_and_keeps_the_best_hits_of_both():
    backend, wider = Lossy(DOCS, keep=1), Lossy(DOCS, keep=3)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), short_results="widen", wider=wider)
    result = _both(retriever, BOB, 5)
    # Three of Bob's four: better, but the count says one is still missing.
    assert _ids(result) == BOBS[:3]
    assert (result.shortfall, result.fallback, result.complete) == ("missed", "widened", False)
    assert backend.calls == ["search", ("count", 5)] * 2 and wider.calls == ["search"] * 2
    # A wider search that finds all four leaves nothing missing.
    everything = InMemoryBackend(DOCS)
    retriever = PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer(), short_results="widen", wider=everything)
    result = _both(retriever, BOB, 5)
    assert _ids(result) == BOBS and (result.fallback, result.complete) == ("widened", True)


def test_a_wider_search_that_misses_what_the_first_found_does_not_lose_it():
    class Tail(Lossy):
        """Returns the last `keep` of the hits under a filter: a search that went the wrong way."""

        def search(self, query, k, filter=None, *, exact=False):
            self.calls.append("search")
            hits = self.store.search(query, k, filter)
            return hits if filter is None else hits[-self.keep :]

    retriever = PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer(), short_results="widen", wider=Tail(DOCS, 2))
    result = _both(retriever, BOB, 5)
    assert _ids(result) == ["eng-0", "pub-0", "pub-1"] and result.complete is False


# ---- what runs again is checked again, and fails closed ----------------------------------------


class LeakyExact(Lossy):
    """Its exact search ignores the filter."""

    def search(self, query, k, filter=None, *, exact=False):
        return super().search(query, k, None if exact else filter, exact=exact)


@pytest.mark.parametrize("recheck", [True, False])
def test_a_search_run_again_is_rechecked_and_cannot_leak(recheck):
    authorizer = MetadataACLAuthorizer()
    retriever = PermissionedRetriever(LeakyExact(DOCS), authorizer, short_results="exact", recheck=recheck)
    result = _both(retriever, BOB, 5)
    # The unfiltered top five holds three chunks for hr only: removed and counted, never returned.
    assert _ids(result) == ["eng-0", "eng-1"] and all(may_read(BOB, h) for h in result.hits)
    assert result.violations == 3 and result.complete is False
    assert result.examined == (5 if recheck else 0)


def test_hits_of_the_first_search_that_are_not_returned_are_still_checked():
    class LeakyFirst(Lossy):
        """Its first search ignores the filter and returns one chunk Bob may not read."""

        def search(self, query, k, filter=None, *, exact=False):
            if exact or filter is None:
                return super().search(query, k, filter, exact=exact)
            self.calls.append("search")
            return self.store.search("payouts", 1)

    result = _both(PermissionedRetriever(LeakyFirst(DOCS), MetadataACLAuthorizer(), short_results="exact"), BOB, 5)
    assert _ids(result) == BOBS and result.examined == 5
    assert result.violations == 1 and result.complete is False  # the backend ignored the filter once


class Failing(Lossy):
    def __init__(self, docs, part):
        super().__init__(docs)
        self.part = part

    def search(self, query, k, filter=None, *, exact=False):
        if exact and self.part == "exact":
            raise ConnectionError("store unreachable")
        return super().search(query, k, filter, exact=exact)

    def count(self, filter, limit):
        if self.part == "count":
            raise ConnectionError("store unreachable")
        return super().count(filter, limit)


@pytest.mark.parametrize("part", ["count", "exact"])
def test_an_error_in_the_count_or_the_search_run_again_fails_the_retrieval(part):
    # A recall sample catches only its own failures: these come before it, and still fail the call.
    retriever = PermissionedRetriever(
        Failing(DOCS, part), MetadataACLAuthorizer(), short_results="exact", recall_sample_rate=1
    )
    with pytest.raises(SearchError, match=f"^{'count' if part == 'count' else 'search'} failed$") as failure:
        retriever.retrieve("leave", BOB, 5)
    assert isinstance(failure.value.__cause__, ConnectionError)  # the store's own error, for the logs
    with pytest.raises(SearchError) as failure:
        asyncio.run(retriever.aretrieve("leave", BOB, 5))
    assert isinstance(failure.value.__cause__, ConnectionError)


def test_an_authorizer_error_after_a_search_run_again_fails_closed():
    def refuse(subject, hits, action):
        raise ConnectionError("permission service unreachable")

    authorizer = FunctionAuthorizer(refuse, filter_for=MetadataACLAuthorizer().filter_for)
    retriever = PermissionedRetriever(Lossy(DOCS), authorizer, short_results="exact")
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB, 5)
    with pytest.raises(AuthorizationError):
        asyncio.run(retriever.aretrieve("leave", BOB, 5))


@pytest.mark.parametrize("answer", [-1, True, 2.5, "4"])
def test_a_count_that_is_not_a_number_of_chunks_fails_the_retrieval(answer):
    class Garbled(Lossy):
        def count(self, filter, limit):
            return answer

    retriever = PermissionedRetriever(Garbled(DOCS), MetadataACLAuthorizer(), short_results="count")
    with pytest.raises(TypeError, match="count must return"):
        retriever.retrieve("leave", BOB, 5)
    with pytest.raises(TypeError, match="count must return"):
        asyncio.run(retriever.aretrieve("leave", BOB, 5))


# ---- sampled recall ----------------------------------------------------------------------------


class Worst(Lossy):
    """Returns k hits under a filter, but the lowest ranked: a full result that is not the top k."""

    def search(self, query, k, filter=None, *, exact=False):
        self.calls.append("exact" if exact else "search")
        hits = self.store.search(query, len(self.docs), filter)
        return hits[:k] if exact or filter is None else hits[-k:]


def test_a_sampled_retrieval_measures_how_much_of_the_exact_top_k_came_back():
    backend = Lossy(DOCS)
    result = _both(PermissionedRetriever(backend, MetadataACLAuthorizer(), recall_sample_rate=1.0), BOB, 5)
    assert _ids(result) == ["eng-0"] and result.sampled_recall == 0.25  # one of four
    assert result.recall_sample == "measured"
    # A count bounds the exact search by max_exact_rows; the exact hits feed the measure only.
    assert backend.calls == ["search", ("count", 10_001), "exact"] * 2
    # k hits that are not the top k: complete cannot see it, the sample can.
    worst = _both(PermissionedRetriever(Worst(DOCS), MetadataACLAuthorizer(), recall_sample_rate=1), BOB, 2)
    assert _ids(worst) == ["pub-0", "pub-1"] and worst.complete is True and worst.sampled_recall == 0.0


def test_a_sample_counts_only_chunks_the_filter_matches_and_returns_none_of_them():
    result = _both(PermissionedRetriever(LeakyExact(DOCS), MetadataACLAuthorizer(), recall_sample_rate=1), BOB, 5)
    # The exact search returned eng-0, eng-1 and three chunks for hr: only Bob's two are counted.
    assert _ids(result) == ["eng-0"] and result.sampled_recall == 0.5 and result.violations == 0


def test_nothing_is_sampled_where_a_sample_would_say_nothing():
    def sampled(backend, **options):
        return _both(PermissionedRetriever(backend, MetadataACLAuthorizer(), **options), BOB, 5).sampled_recall

    assert sampled(Lossy(DOCS)) is None  # a rate of 0, the default
    assert sampled(InMemoryBackend(DOCS), recall_sample_rate=1) is None  # an exact search, always
    assert sampled(Inexact(DOCS), recall_sample_rate=1) is None  # no exact search to compare with
    repaired = Lossy(DOCS)
    assert sampled(repaired, recall_sample_rate=1, short_results="exact") is None  # the hits are exact already
    assert repaired.calls == ["search", ("count", 10_001), "exact"] * 2
    nothing = PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer(), recall_sample_rate=1)
    found = _both(nothing, BOB, 5, query="salary")
    assert (found.sampled_recall, found.recall_sample) == (None, "measured")  # nothing to find
    assert _both(PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer()), BOB, 5).recall_sample is None


def test_only_the_sampled_share_runs_an_exact_search(monkeypatch):
    backend = Lossy(DOCS)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), recall_sample_rate=0.5)
    monkeypatch.setattr("wardowl.retriever.random.random", lambda: 0.75)
    assert retriever.retrieve("leave", BOB, 5).sampled_recall is None and backend.calls == ["search"]
    monkeypatch.setattr("wardowl.retriever.random.random", lambda: 0.25)
    assert retriever.retrieve("leave", BOB, 5).sampled_recall == 0.25


def test_a_sample_runs_no_exact_search_above_max_exact_rows_or_without_a_count():
    backend = Lossy(DOCS)
    bounded = PermissionedRetriever(backend, MetadataACLAuthorizer(), recall_sample_rate=1, max_exact_rows=3)
    over = _both(bounded, BOB, 5)
    assert _ids(over) == ["eng-0"] and (over.sampled_recall, over.recall_sample) == (None, "over_limit")
    assert backend.calls == ["search", ("count", 4)] * 2  # Bob may read four chunks, one more than the bound
    for uncountable in (Uncounted(DOCS), Keyword(DOCS)):
        result = _both(PermissionedRetriever(uncountable, MetadataACLAuthorizer(), recall_sample_rate=1), BOB, 5)
        assert (result.sampled_recall, result.recall_sample) == (None, "unsupported")
        assert "exact" not in uncountable.calls
    unbounded = Uncounted(DOCS)
    retriever = PermissionedRetriever(unbounded, MetadataACLAuthorizer(), recall_sample_rate=1, max_exact_rows=None)
    result = _both(retriever, BOB, 5)
    assert (result.sampled_recall, result.recall_sample) == (0.25, "measured")
    assert unbounded.calls == ["search", "exact"] * 2


@pytest.mark.parametrize("part", ["count", "exact"])
def test_a_failed_sample_is_recorded_and_the_retrieval_returns_its_hits(part, caplog):
    # On pgvector, an exact search over a broad readable set can outlast statement_timeout: the
    # sample is monitoring, so its failure must not take the hits already found with it.
    events = []
    retriever = PermissionedRetriever(
        Failing(DOCS, part), MetadataACLAuthorizer(), recall_sample_rate=1, observers=[events.append]
    )
    result = _both(retriever, BOB, 5)
    assert _ids(result) == ["eng-0"] and (result.sampled_recall, result.recall_sample) == (None, "failed")
    assert [(e.error, e.recall_sample, e.returned_ids) for e in events] == [(None, "failed", ("eng-0",))] * 2
    assert "recall sample failed" in caplog.text and "store unreachable" in caplog.text


def test_a_sample_whose_count_is_not_a_number_is_recorded_as_failed():
    class Garbled(Lossy):
        def count(self, filter, limit):
            return "4"

    result = _both(PermissionedRetriever(Garbled(DOCS), MetadataACLAuthorizer(), recall_sample_rate=1), BOB, 5)
    assert _ids(result) == ["eng-0"] and result.recall_sample == "failed"


# ---- reporting ---------------------------------------------------------------------------------


class SlowCount(Lossy):
    def count(self, filter, limit):
        time.sleep(0.01)
        return super().count(filter, limit)


@pytest.mark.parametrize("run", ["sync", "async"])
def test_events_report_the_shortfall_what_was_done_and_the_time_it_took(run):
    events = []
    retriever = PermissionedRetriever(
        SlowCount(DOCS), MetadataACLAuthorizer(), short_results="exact", observers=[events.append]
    )
    if run == "sync":
        retriever.retrieve("leave", BOB, 5)
    else:
        asyncio.run(retriever.aretrieve("leave", BOB, 5))
    (event,) = events
    assert (event.shortfall, event.fallback, event.complete, event.searches) == ("missed", "exact", True, 2)
    assert 10 <= event.shortfall_ms <= event.search_ms
    assert event.to_dict()["fallback"] == "exact" and event.sampled_recall is None


def test_a_samples_count_is_search_time_not_shortfall_time():
    events = []
    retriever = PermissionedRetriever(
        SlowCount(DOCS, keep=5), MetadataACLAuthorizer(), recall_sample_rate=1, observers=[events.append]
    )
    result = retriever.retrieve("leave", BOB, 4)  # Bob's four chunks: a full result, nothing short
    (event,) = events
    assert result.shortfall is None and (event.recall_sample, event.sampled_recall) == ("measured", 1.0)
    assert event.shortfall_ms == 0 and event.search_ms >= 10 and event.searches == 2
    assert event.to_dict()["recall_sample"] == "measured"


def _points(reader):
    points = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = list(metric.data.data_points)
    return points


def test_opentelemetry_records_short_results_and_sampled_recall():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from wardowl.otel import RECALL_BUCKETS, OpenTelemetryMetrics, OpenTelemetryObserver

    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    observers = [OpenTelemetryObserver(provider.get_tracer("test")), OpenTelemetryMetrics(meter)]
    repaired = PermissionedRetriever(Lossy(DOCS), MetadataACLAuthorizer(), short_results="exact", observers=observers)
    repaired.retrieve("leave", BOB, 5)
    sampled = PermissionedRetriever(
        Lossy(DOCS), MetadataACLAuthorizer(), short_results="count", recall_sample_rate=1, observers=observers
    )
    sampled.retrieve("leave", BOB, 5)

    first, second = exporter.get_finished_spans()
    assert (first.attributes["wardowl.shortfall"], first.attributes["wardowl.fallback"]) == ("missed", "exact")
    assert first.attributes["wardowl.shortfall.duration_ms"] >= 0 and "wardowl.sampled_recall" not in first.attributes
    assert second.attributes["wardowl.sampled_recall"] == 0.25 and "wardowl.fallback" not in second.attributes
    assert second.attributes["wardowl.recall_sample"] == "measured" and "wardowl.recall_sample" not in first.attributes

    points = _points(reader)
    short = {
        (p.attributes["wardowl.shortfall"], p.attributes.get("wardowl.fallback")): p.value
        for p in points["wardowl.short_results"]
    }
    assert short == {("missed", "exact"): 1, ("missed", None): 1}
    (recall,) = points["wardowl.sampled_recall"]
    assert (recall.count, recall.sum) == (1, 0.25) and tuple(recall.explicit_bounds) == RECALL_BUCKETS
    (sample,) = points["wardowl.recall_samples"]
    assert (sample.attributes["wardowl.recall_sample"], sample.value) == ("measured", 1)


# ---- configuration -----------------------------------------------------------------------------


class NoFilters(InMemoryBackend):
    supports_filters = False


@pytest.mark.parametrize(
    "options",
    [
        {"short_results": "repair"},
        {"short_results": "widen"},  # without wider
        {"wider": InMemoryBackend()},  # without short_results="widen"
        {"short_results": "widen", "wider": NoFilters()},
        {"max_exact_rows": 0},
        {"max_exact_rows": True},
        {"max_exact_rows": 2.5},
        {"recall_sample_rate": -0.1},
        {"recall_sample_rate": 1.5},
        {"recall_sample_rate": float("nan")},
        {"recall_sample_rate": True},
        {"recall_sample_rate": "0.1"},
    ],
)
def test_settings_for_short_results_are_checked_when_the_retriever_is_made(options):
    with pytest.raises(ValueError):
        PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), **options)


# ---- properties --------------------------------------------------------------------------------


def _with_a_shared_word(docs, query):
    # Every chunk shares a word with the query, so every chunk the filter matches can be ranked, as
    # in a vector search: the count of matching chunks is then what an exact search can find.
    return [Document(d.id, f"doc {d.text}", d.metadata) for d in docs], f"doc {query}"


@settings(max_examples=100, deadline=None)
@given(corpora(max_size=20), subjects, queries, st.integers(1, 6), st.integers(0, 6))
def test_exact_returns_the_true_top_k_and_says_so(docs, subject, query, k, keep):
    docs, query = _with_a_shared_word(docs, query)
    retriever = PermissionedRetriever(Lossy(docs, keep), MetadataACLAuthorizer(), short_results="exact")
    result = retriever.retrieve(query, subject, k)
    assert _ids(result) == oracle_top_k(docs, subject, query, k)
    assert result.complete is True and result.violations == 0


@settings(max_examples=100, deadline=None)
@given(corpora(max_size=20), subjects, queries, st.integers(1, 6), st.integers(0, 6))
def test_count_says_complete_only_when_nothing_readable_is_missing(docs, subject, query, k, keep):
    docs, query = _with_a_shared_word(docs, query)
    retriever = PermissionedRetriever(Lossy(docs, keep), MetadataACLAuthorizer(), short_results="count")
    result = retriever.retrieve(query, subject, k)
    readable = oracle_top_k(docs, subject, query, len(docs) + 1)
    assert all(may_read(subject, h) for h in result.hits)
    if len(result.hits) < k:
        assert result.shortfall == ("exhausted" if len(result.hits) == len(readable) else "missed")
    assert result.complete is (len(result.hits) == min(k, len(readable)))

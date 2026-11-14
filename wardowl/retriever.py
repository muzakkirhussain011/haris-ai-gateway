"""PermissionedRetriever: search, but only return what the subject may read.

Strategies
----------
prefilter
    Ask the authorizer for a filter and let the backend apply it inside the search. Results are
    the top k of what the subject may read, as far as the backend's index finds them, and cost one
    search. Needs an authorizer that can express its rules as a filter and a backend that supports
    filters.
postfilter
    Search without a filter, then check each candidate. Fetches `k * overfetch` candidates and
    widens the search until k permitted hits are found, the backend runs out, or `max_candidates`
    is reached. Works with any authorizer; for subjects who may read little, results can be
    incomplete, which `RetrievalResult.complete` reports.
auto (default)
    prefilter when both sides support it, postfilter otherwise.

Short results
-------------
A pre-filtered search on an approximate index can return fewer than k hits though the subject may
read more: the index stopped early under a selective filter. `short_results` decides what happens
then: "report" it (the default); "count" the chunks the filter matches, to tell a small readable
set from a miss; or repair a miss by searching again, exactly ("exact") or with larger search
settings ("widen"). A search run again uses the same filter, and its hits are checked again like
any other.

Guarantees
----------
* Fail closed: if the authorizer errors, `AuthorizationError` is raised and nothing is returned.
  Its message is fixed, without the text of the exception behind it, which is kept as its
  `__cause__`: agent frameworks show tool errors to the model. If the backend's search or count
  fails, `SearchError` is raised the same way, with a fixed message and the backend's exception as
  its `__cause__`; only `InvalidQuery`, which says the query itself cannot be searched, is raised
  as it is.
* Defence in depth: pre-filtered results are checked again with the authorizer (`recheck=True`).
  A backend that ignored the filter cannot leak; the rejected hits are counted in `violations`.
  With `recheck=False` there is no second call to the authorizer, but each pre-filtered hit is
  still matched against the filter locally, so a backend that ignores the filter cannot leak
  either; only a filter that is itself broader than the authorizer's check goes unnoticed.
* A `reranker` sees only hits that passed the check, and can only reorder, score or drop them.
* With `required_observers`, a retrieval that one of them cannot record raises `AuditError`, with a
  fixed message, and returns nothing.

The algorithm is written once, as a generator of operations; `retrieve` runs it synchronously and
`aretrieve` runs it with `await`, so both paths share the same security logic.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import logging
import math
import random
import threading
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from wardowl.authz import AuthorizationError, Authorizer
from wardowl.events import AuditError, Observer, RetrievalEvent, query_fingerprint
from wardowl.filters import And, Filter, In, Or, matches
from wardowl.redaction import RedactFunction, Redactor
from wardowl.types import Hit, InvalidQuery, Query, Subject

__all__ = [
    "PermissionedRetriever",
    "Reranker",
    "RetrievalResult",
    "SearchBackend",
    "SearchError",
    "ShortResults",
    "Strategy",
]

Strategy = Literal["auto", "prefilter", "postfilter"]
ShortResults = Literal["report", "count", "exact", "widen"]
Shortfall = Literal["exhausted", "missed", "unknown"]
Fallback = Literal["exact", "widened", "over_limit", "unsupported"]
RecallSample = Literal["measured", "over_limit", "unsupported", "failed"]


@runtime_checkable
class SearchBackend(Protocol):
    """A vector store or search index. Return hits ordered best first.

    `supports_filters` says whether `search` honours the `filter` argument. Backends may also
    provide `async def asearch(...)` with the same signature; otherwise `search` runs in a thread.

    `query` is what the caller passed to `retrieve`, unchanged: a string, or a `Query` when the
    caller supplies a vector. `Query.of(query)` gives a `Query` either way. A backend that takes
    only strings still works with every string query.

    `exact_search` is optional, and is not part of this protocol's required members: the retriever
    reads it with `getattr`, and anything but `True` counts as False. A backend sets it to True
    when `search` ranks every chunk that matches the filter, instead of walking an approximate
    index that can stop early, so a result with fewer than k hits proves that no other chunk
    matches. The retriever then reports such a result as complete. A backend searched through an
    approximate index (HNSW, IVF and the like) leaves it out or sets it to False, and its short
    results are reported with `complete=False`.

    Two more optional members let the retriever check and repair a short pre-filtered result (see
    `short_results`), and are read the same way. `count(filter, limit)` returns how many chunks
    match the filter, or any number of at least `limit` when that many do; it returns None when a
    count cannot be compared with the search, as for a keyword search, which ranks only the chunks
    that contain the query's words. An async `acount` with the same signature may come with it.
    `supports_exact = True` says that `search`, and `asearch` if there is one, accept `exact=True`
    and then rank every chunk that matches the filter, as an exact search does. The retriever
    passes `exact` only to a backend that declares `supports_exact`, so other backends never see it.
    """

    supports_filters: bool

    def search(self, query: Any, k: int, filter: Filter | None = None) -> list[Hit]: ...


class SearchError(RuntimeError):
    """The backend's search or count failed, so nothing is returned.

    Its message is fixed ("search failed" or "count failed"), never the text of the backend's
    exception, which can name hosts, users or credentials and stays its `__cause__` for the logs:
    agent frameworks show a tool's error to the model. `InvalidQuery` from the backend is raised as
    it is, since it says the query itself cannot be searched.
    """


@contextmanager
def _backend_errors(failed: str) -> Iterator[None]:
    """Raise `SearchError(failed)` for whatever the backend raises inside, but `InvalidQuery`."""
    try:
        yield
    except InvalidQuery:
        raise
    except Exception as exc:
        raise SearchError(failed) from exc


@runtime_checkable
class Reranker(Protocol):
    """Reorders the hits a subject may read: a cross-encoder, a hosted rerank API, any model.

    `rerank` receives the query as it was given to `retrieve` and the permitted hits, best first,
    and returns some or all of those hits in its order, with its scores. It may also provide
    `async def arerank(...)` with the same signature. A plain function with the same signature
    can be passed instead.
    """

    def rerank(self, query: Any, hits: list[Hit]) -> Sequence[Hit]: ...


@dataclass(frozen=True)
class RetrievalResult:
    """What one retrieval returned, with the counts needed to monitor it."""

    hits: list[Hit]
    """Permitted hits, best first, at most k."""
    strategy: Literal["prefilter", "postfilter"]
    """The strategy that was actually used."""
    examined: int
    """Candidates the authorizer checked."""
    denied: int
    """Post-filtered candidates the subject may not read."""
    violations: int
    """Pre-filtered hits that failed the re-check. Should always be 0; anything else means the
    backend did not apply the filter and deserves an alert."""
    complete: bool
    """False when wardowl knows results may be missing. True only when:

    * pre-filtering: the re-check removed no hit, and the backend returned as many hits as were
      asked for (k, or `rerank_depth` with a reranker), or fewer that prove nothing else matches:
      from a backend whose `exact_search` is True, or at least as many as the backend counted
      under the filter (with `short_results` other than "report"), after any search run again;
    * post-filtering: k permitted hits were found, or the backend ran out of candidates before
      `max_candidates`. A candidate list shorter than asked for counts as running out when the
      backend's `exact_search` is True or the search had no filter; under a scope, an approximate
      index may have stopped early, so it does not count.

    A full result trusts the backend's ranking: `complete` cannot see better chunks that an
    approximate index failed to rank. `sampled_recall` can."""
    shortfall: Shortfall | None = None
    """Why the pre-filtered search returned fewer hits than were asked for; None when it returned
    as many, or the result was post-filtered.

    * `"exhausted"`: the subject may read no more matching chunks. The backend searches exactly,
      or it counted the chunks the filter matches and found no more than came back.
    * `"missed"`: the index missed chunks the subject may read. The backend counted more chunks
      matching the filter than came back.
    * `"unknown"`: the search is approximate and nothing was counted, because `short_results` is
      "report" or the backend cannot count, so it may be either.

    It describes the first search, before any `fallback`."""
    fallback: Fallback | None = None
    """What `short_results` did about a short result; None when it did nothing: with "report" or
    "count", or after a shortfall of "exhausted".

    * `"exact"`: the search ran again, exactly, and its hits replaced the first search's.
    * `"widened"`: the search ran again with `wider`, and the best hits of both searches were kept.
    * `"over_limit"`: the backend counted more than `max_exact_rows` matching chunks, so nothing
      ran again and the first search's hits were kept.
    * `"unsupported"`: the backend cannot count, or, for "exact", cannot search exactly, so
      nothing ran again and the first search's hits were kept."""
    sampled_recall: float | None = None
    """For a pre-filtered retrieval that `recall_sample_rate` sampled, the share of an exact
    search's top k, under the same filter and before redaction and reranking, that came back.
    None when the retrieval was not sampled, or the exact search found nothing or did not run
    (see `recall_sample`). It measures what `complete` cannot see: k hits from an approximate index
    that are not the true top k."""
    recall_sample: RecallSample | None = None
    """What happened to a recall sample; None when the retrieval was not sampled. A sample never
    changes the hits, and never fails the retrieval.

    * `"measured"`: the exact search ran, and `sampled_recall` holds the share, or None when the
      exact search found nothing.
    * `"over_limit"`: the backend counted more than `max_exact_rows` chunks matching the filter, so
      the exact search did not run.
    * `"unsupported"`: `max_exact_rows` is set and the backend cannot count, so the exact search,
      which nothing would bound, did not run.
    * `"failed"`: the count or the exact search raised. The exception is logged on the `wardowl`
      logger, and the retrieval returns its hits as if it had not been sampled."""


# Operations the algorithm asks its driver to perform.
@dataclass(frozen=True)
class _FilterFor:
    pass


@dataclass(frozen=True)
class _ScopeFor:
    pass


@dataclass(frozen=True)
class _Search:
    query: str | Query
    k: int
    filter: Filter | None
    mode: Literal["search", "exact", "wider"] = "search"
    """The backend's own search, its exact search, or a search of `wider`."""
    repair: bool = False
    """Run again because the first search came back short: its time counts as shortfall time too."""


@dataclass(frozen=True)
class _Count:
    filter: Filter
    limit: int
    repair: bool = True
    """Counted for a short result, so its time counts as shortfall time; False for a recall sample."""


@dataclass(frozen=True)
class _Check:
    hits: list[Hit]


@dataclass(frozen=True)
class _Rerank:
    query: str | Query
    hits: list[Hit]


@dataclass(frozen=True)
class _Redact:
    hits: list[Hit]


_Op = _FilterFor | _ScopeFor | _Search | _Count | _Check | _Rerank | _Redact
_log = logging.getLogger("wardowl")
_UNRECORDED = "the retrieval could not be recorded, so nothing was returned"
_Plan = Generator[_Op, Any, RetrievalResult]
RerankFunction = Callable[[Any, list[Hit]], Sequence[Hit]]


class _Timings:
    """Seconds spent in the backend, the authorizer and the reranker during one retrieval, and the
    part of the backend's time spent on a short result."""

    def __init__(self) -> None:
        self.search = self.authorize = self.rerank = self.shortfall = 0.0
        self.searches = self.reranked = 0

    @contextmanager
    def measure(self, op: _Op) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:  # a failed operation still took its time
            elapsed = time.perf_counter() - start
            if isinstance(op, _Search):
                self.search += elapsed
                self.searches += 1
                if op.repair:
                    self.shortfall += elapsed
            elif isinstance(op, _Count):
                self.search += elapsed
                if op.repair:
                    self.shortfall += elapsed
            elif isinstance(op, _Rerank):
                self.rerank += elapsed
                self.reranked += len(op.hits)
            elif not isinstance(op, _Redact):  # redaction is not reported as time in the authorizer
                self.authorize += elapsed


class PermissionedRetriever:
    """Searches a backend and returns only the hits the subject may read.

    `strategy` chooses where permissions are applied: `"prefilter"` inside the search, with a
    filter from the authorizer; `"postfilter"` after it, checking each candidate; `"auto"` (the
    default) pre-filters when the backend supports filters and the authorizer returns one.
    `action` is passed to the authorizer on every call.

    Post-filtering fetches `k * overfetch` candidates and multiplies the fetch by `overfetch`
    until k permitted hits are found, the backend has no more, or `max_candidates` is reached;
    it never checks more candidates than it asked for, whatever the backend returns.
    `max_candidates` bounds every retrieval: a `k` above it raises `ValueError`.
    `recheck` checks pre-filtered hits again with the authorizer, so a backend that ignores the
    filter cannot leak; with `recheck=False` they are matched against the filter locally instead,
    which needs the backend to return each chunk's metadata as stored (the conformance kit checks
    that). `observers` receive a `RetrievalEvent` for every call, and `audit_key` (at least 16
    random bytes) adds an HMAC of the query to those events. An observer that raises is logged and
    counted in `observer_failures`, and retrieval goes on: delivery to `observers` is best-effort.
    `required_observers`, such as the audit log a compliance control depends on, receive each
    event first, and must record it: when one raises after a retrieval that found its hits, the
    retrieval raises `AuditError` and returns nothing, and `observers` then receive an event with
    that error. A retrieval that failed raises its own error, whatever the observers do.
    `aretrieve` calls all of them in a worker thread, so they may be called from several threads
    at once.

    `reranker` reorders the result after the permission check: the retriever finds the top
    `rerank_depth` hits the subject may read (or k, if larger, and at most `max_candidates`), the
    reranker orders them, and the first k are returned. The reranker never sees a chunk the
    subject may not read.

    `redactor` (see `wardowl.redaction`) hides parts of the permitted hits, after the check and
    before the reranker: it may change a hit's text and metadata but must return the same hits in
    the same order, and if it fails the retrieval raises `AuthorizationError`. `aretrieve` runs it
    in a thread, off the event loop.

    `short_results` decides what happens when a pre-filtered search on an approximate backend
    returns fewer hits than were asked for (see `RetrievalResult.shortfall`). `"report"` (the
    default) returns them with `complete=False`. `"count"` asks the backend how many chunks match
    the filter (its `count`): no more than came back proves that the subject may read no more, and
    the result is complete; more proves that the index missed some. `"exact"` counts too, and
    after a miss searches again exactly, when the count is at most `max_exact_rows` (10,000 by
    default; None for no bound), so that a large readable set never sets off an unbounded scan.
    `"widen"` counts too, and after a miss searches again with `wider`, the same store with larger
    search settings, keeping the best hits of both searches. A search run again uses the same
    filter, and its hits, with those of the first search, are checked again like any other; an
    error in the count or in the search fails the retrieval. A backend that cannot count, or, for
    `"exact"`, cannot search exactly, keeps the first hits and says why in
    `RetrievalResult.fallback`.

    `recall_sample_rate` (0 by default) is the share of pre-filtered retrievals for which a backend
    that can search exactly, but does not always, runs an exact search as well. The exact hits are
    never returned; `RetrievalResult.sampled_recall` reports how much of their top k came back,
    which measures what `complete` cannot see. Each sampled retrieval costs one more search, and,
    with `max_exact_rows` set, a count first: above the bound, or on a backend that cannot count,
    the exact search does not run. A sample is monitoring: if its count or search fails, the
    failure is logged and recorded in `RetrievalResult.recall_sample`, and the retrieval returns
    its hits.
    """

    def __init__(
        self,
        backend: SearchBackend,
        authorizer: Authorizer,
        *,
        strategy: Strategy = "auto",
        action: str = "read",
        overfetch: int = 4,
        max_candidates: int = 1000,
        recheck: bool = True,
        observers: Sequence[Observer] = (),
        audit_key: bytes | None = None,
        reranker: Reranker | RerankFunction | None = None,
        rerank_depth: int = 50,
        redactor: Redactor | RedactFunction | None = None,
        short_results: ShortResults = "report",
        max_exact_rows: int | None = 10_000,
        wider: SearchBackend | None = None,
        recall_sample_rate: float = 0.0,
        required_observers: Sequence[Observer] = (),
    ) -> None:
        if strategy not in ("auto", "prefilter", "postfilter"):
            raise ValueError(f"unknown strategy {strategy!r}")
        if short_results not in ("report", "count", "exact", "widen"):
            raise ValueError(f"unknown short_results {short_results!r}")
        rows: Any = max_exact_rows
        if rows is not None and (isinstance(rows, bool) or not isinstance(rows, int) or rows < 1):
            raise ValueError("max_exact_rows must be None or a positive integer")
        if short_results == "widen" and wider is None:
            raise ValueError("short_results='widen' needs wider, the backend to search again")
        if wider is not None and short_results != "widen":
            raise ValueError("wider is used only with short_results='widen'")
        if wider is not None and not wider.supports_filters:
            raise ValueError("wider must support filters: it searches again under the permission filter")
        rate: Any = recall_sample_rate
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError("recall_sample_rate must be a number from 0 to 1")
        if strategy == "prefilter" and not backend.supports_filters:
            raise ValueError("strategy='prefilter' needs a backend that supports filters")
        if overfetch < 2:
            raise ValueError("overfetch must be at least 2")
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if reranker is not None and not (hasattr(reranker, "rerank") or callable(reranker)):
            raise TypeError("reranker must have a rerank method or be a function")
        if rerank_depth < 1:
            raise ValueError("rerank_depth must be positive")
        if redactor is not None and not (hasattr(redactor, "redact") or callable(redactor)):
            raise TypeError("redactor must have a redact method or be a function")
        key: Any = audit_key
        if key is not None and not isinstance(key, (bytes, bytearray)):
            # hmac needs bytes: a string would fail inside every event, and observers would hear nothing.
            raise TypeError(f"audit_key must be bytes, such as secrets.token_bytes(32), not {type(key).__name__}")
        if audit_key is not None and len(audit_key) < 16:
            # As for RedisCache's signing_key: a short key makes the fingerprints of short queries guessable.
            raise ValueError("audit_key should be at least 16 random bytes")
        observers, required_observers = tuple(observers), tuple(required_observers)
        if any(o is r for o in observers for r in required_observers):
            raise ValueError("an observer is given either in observers or in required_observers, not both")
        if not all(callable(o) for o in required_observers):
            # Each would fail every retrieval; say so now, not on the first call.
            raise TypeError("a required observer is a callable that takes a RetrievalEvent")
        self.redactor = redactor
        self.reranker = reranker
        self.rerank_depth = rerank_depth
        self.backend = backend
        self.authorizer = authorizer
        self.strategy: Strategy = strategy
        self.action = action
        self.overfetch = overfetch
        self.max_candidates = max_candidates
        self.recheck = recheck
        self.observers = tuple(observers)
        self.required_observers = tuple(required_observers)
        self.audit_key = audit_key
        self.short_results: ShortResults = short_results
        self.max_exact_rows = max_exact_rows
        self.wider = wider
        self.recall_sample_rate = recall_sample_rate
        self._failures = 0
        self._failures_lock = threading.Lock()

    # ---- public API ------------------------------------------------------------------------

    @property
    def observer_failures(self) -> int:
        """How many times an observer, required or not, has raised since this retriever was made.

        Each raise is also logged on the `wardowl` logger. A rising count means events are being
        lost, for example an audit log that can no longer be written; alert on it, or on the
        `wardowl.observer_failures` metric of `OpenTelemetryMetrics`.
        """
        with self._failures_lock:
            return self._failures

    def retrieve(self, query: str | Query, subject: Subject, k: int = 5) -> RetrievalResult:
        """Return at most `k` hits the subject may read, best first.

        `query` is text, or a `Query` carrying a vector you computed. Raises `AuthorizationError`
        when the authorizer fails or gives a malformed answer, `SearchError` when the backend's
        search or count fails, `AuditError` when one of `required_observers` cannot record the
        call, and `ValueError` or `TypeError` for invalid arguments: `InvalidQuery`, a
        `ValueError`, when the query or `k` is at fault rather than the configuration. Nothing is
        returned in any of these cases.
        """
        started, clock, timings = time.time(), time.perf_counter(), _Timings()
        try:
            plan = self._plan(query, subject, k)
            try:
                op = next(plan)
                while True:
                    try:
                        with timings.measure(op):
                            value = self._run(op, subject)
                    except Exception as exc:
                        # The plan decides: only a recall sample catches its own failure, so any
                        # other is raised again from the plan, as it would have been from here.
                        op = plan.throw(exc)
                    else:
                        op = plan.send(value)
            except StopIteration as done:
                result: RetrievalResult = done.value
        except BaseException as exc:  # a cancelled or interrupted call is reported too, then re-raised
            self._emit(query, subject, k, started, clock, timings, None, exc)
            raise
        self._emit(query, subject, k, started, clock, timings, result, None)  # AuditError, if required
        return result

    async def aretrieve(self, query: str | Query, subject: Subject, k: int = 5) -> RetrievalResult:
        """The async form of `retrieve`, with the same arguments and guarantees.

        It uses the backend's `asearch` and the authorizer's `afilter_for` and `acheck` when they
        exist, and runs the sync methods in a thread otherwise. The redactor runs in a thread, and so
        do the observers, which have received the event before `aretrieve` returns or raises.
        """
        started, clock, timings = time.time(), time.perf_counter(), _Timings()
        try:
            plan = self._plan(query, subject, k)
            try:
                op = next(plan)
                while True:
                    try:
                        with timings.measure(op):
                            value = await self._arun(op, subject)
                    except Exception as exc:  # as in retrieve: the plan decides
                        op = plan.throw(exc)
                    else:
                        op = plan.send(value)
            except StopIteration as done:
                result: RetrievalResult = done.value
        except BaseException as exc:  # a cancelled or interrupted call is reported too, then re-raised
            event = self._built(query, subject, k, started, clock, timings, None, exc)
            if isinstance(exc, (Exception, asyncio.CancelledError)):
                await self._adeliver(event, succeeded=False)
            else:  # GeneratorExit, KeyboardInterrupt, SystemExit: nothing more may be awaited
                self._deliver(event, succeeded=False)
            raise
        event = self._built(query, subject, k, started, clock, timings, result, None)  # AuditError, if required
        self._unrecorded(await self._adeliver(event, succeeded=True))  # and here
        return result

    def _emit(
        self,
        query: str | Query,
        subject: Subject,
        k: int,
        started: float,
        clock: float,
        timings: _Timings,
        result: RetrievalResult | None,
        error: BaseException | None,
    ) -> None:
        """Give the event to the observers in this thread. Raises `AuditError` only after a retrieval
        that found its hits (`error` is None), when a required observer could not record it."""
        event = self._built(query, subject, k, started, clock, timings, result, error)
        self._unrecorded(self._deliver(event, succeeded=error is None))

    def _built(
        self,
        query: str | Query,
        subject: Subject,
        k: int,
        started: float,
        clock: float,
        timings: _Timings,
        result: RetrievalResult | None,
        error: BaseException | None,
    ) -> RetrievalEvent | None:
        """The event for the observers, or None when there are none or it cannot be built. After a
        retrieval that found its hits, an event that cannot be built for required observers raises
        `AuditError` instead."""
        if not self.observers and not self.required_observers:
            return None
        try:
            return self._event(query, subject, k, started, clock, timings, result, error)
        except Exception as exc:  # reporting must never replace what retrieval returns or raises
            _log.exception("wardowl could not build the retrieval event")
            if self.required_observers and error is None:
                # ... but nothing returns when the record a required observer must keep cannot be made.
                raise AuditError(_UNRECORDED) from exc
            return None

    @staticmethod
    def _unrecorded(failure: Exception | None) -> None:
        """Raise `AuditError` for what a required observer raised, if it raised."""
        if failure is not None:
            raise AuditError(_UNRECORDED) from failure

    def _deliver(self, event: RetrievalEvent | None, *, succeeded: bool) -> Exception | None:
        """Give `event` to the required observers, then to the others, and tell those that define
        `observer_failed` about each that raised. Returns what the first required observer raised
        when the retrieval `succeeded`, for the caller to raise as `AuditError`, and None otherwise:
        a retrieval that failed raises its own error, so this never raises."""
        if event is None:
            return None
        failures: list[tuple[Observer, RetrievalEvent, Exception]] = []
        for observer in self.required_observers:
            failure = self._tell(observer, event)
            if failure is not None:
                failures.append((observer, event, failure))
        unrecorded = failures[0][2] if failures and succeeded else None
        if unrecorded is not None:
            # The other observers hear what the caller gets: an AuditError and no chunks.
            event = dataclasses.replace(
                event, returned_ids=(), complete=False, error=AuditError.__name__, cause=type(unrecorded).__name__
            )
        for observer in self.observers:
            failure = self._tell(observer, event)
            if failure is not None:
                failures.append((observer, event, failure))
        for failed, seen, failure in failures:
            for listener in (*self.required_observers, *self.observers):
                hook = getattr(listener, "observer_failed", None)
                if hook is None:
                    continue
                try:
                    hook(failed, seen, failure)
                except Exception:
                    _log.exception("wardowl observer %r failed to record a failed observer", listener)
        return unrecorded

    def _tell(self, observer: Observer, event: RetrievalEvent) -> Exception | None:
        """Give `event` to `observer`; what it raised, logged and counted, or None."""
        try:
            observer(event)
        except Exception as exc:  # never raised from here: the caller decides what a failure changes
            _log.exception("wardowl observer %r failed", observer)
            with self._failures_lock:  # observers run in several worker threads at once under aretrieve
                self._failures += 1
            return exc
        return None

    async def _adeliver(self, event: RetrievalEvent | None, *, succeeded: bool) -> Exception | None:
        """`_deliver` in a worker thread, awaited. An audit sink writes to a file, a socket or a
        collector, and can block: on the event loop it would hold up every other request."""
        if event is None:
            return None
        try:
            delivered = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(contextvars.copy_context().run, self._deliver, event, succeeded=succeeded)
            )
        except RuntimeError:  # the executor is shut down, as when the loop closes: deliver here
            return self._deliver(event, succeeded=succeeded)
        # Shielded: a call cancelled while its event is on the way stops waiting, but the event is
        # still delivered, so every call is reported. Such a call raises CancelledError, whatever a
        # required observer then does.
        unrecorded: Exception | None = await asyncio.shield(delivered)
        return unrecorded

    def _event(
        self,
        query: str | Query,
        subject: Subject,
        k: int,
        started: float,
        clock: float,
        timings: _Timings,
        result: RetrievalResult | None,
        error: BaseException | None,
    ) -> RetrievalEvent:
        fingerprint = None
        if self.audit_key and isinstance(query, (str, Query)):  # a query of the wrong type is reported, not hashed
            fingerprint = query_fingerprint(query, self.audit_key)
        return RetrievalEvent(
            subject_id=subject.id,
            action=self.action,
            k=k,
            strategy=result.strategy if result else None,
            returned_ids=tuple(h.id for h in result.hits) if result else (),
            examined=result.examined if result else 0,
            denied=result.denied if result else 0,
            violations=result.violations if result else 0,
            complete=result.complete if result else False,
            duration_ms=round((time.perf_counter() - clock) * 1000, 3),
            error=type(error).__name__ if error else None,
            cause=type(error.__cause__).__name__ if error is not None and error.__cause__ is not None else None,
            query_hmac=fingerprint,
            started_at=started,
            search_ms=round(timings.search * 1000, 3),
            authorize_ms=round(timings.authorize * 1000, 3),
            searches=timings.searches,
            rerank_ms=round(timings.rerank * 1000, 3),
            reranked=timings.reranked,
            shortfall=result.shortfall if result else None,
            fallback=result.fallback if result else None,
            shortfall_ms=round(timings.shortfall * 1000, 3),
            sampled_recall=result.sampled_recall if result else None,
            recall_sample=result.recall_sample if result else None,
        )

    # ---- the algorithm (shared by both drivers) --------------------------------------------

    def _plan(self, query: str | Query, subject: Subject, k: int) -> _Plan:
        if k < 1:
            raise InvalidQuery("k must be at least 1")
        if k > self.max_candidates:
            raise InvalidQuery(f"k={k} is more than max_candidates={self.max_candidates}")
        if not isinstance(query, (str, Query)):
            raise TypeError(f"a query is a string or a Query, not {type(query).__name__}")
        if self.reranker is None:
            return (yield from self._redacted((yield from self._permitted(query, k))))
        depth = min(max(k, self.rerank_depth), self.max_candidates)
        result = yield from self._redacted((yield from self._permitted(query, depth)))
        if not result.hits:
            return result
        # Only hits that passed the check reach the reranker, which can only reorder, score or drop them.
        ranked: list[Hit] = yield _Rerank(query, result.hits)
        return dataclasses.replace(result, hits=ranked[:k])

    def _redacted(self, result: RetrievalResult) -> _Plan:
        """`result` with the redactor applied to its hits, which must stay the same hits in order."""
        if self.redactor is None or not result.hits:
            return result
        hits: list[Hit] = yield _Redact(result.hits)
        if not all(isinstance(h, Hit) for h in hits) or [h.id for h in hits] != [h.id for h in result.hits]:
            raise AuthorizationError("the redactor must return the same hits, in the same order")
        return dataclasses.replace(result, hits=hits)

    def _permitted(self, query: str | Query, k: int) -> _Plan:
        """The top k hits the subject may read."""
        f: Filter | None = None
        if self.strategy != "postfilter" and self.backend.supports_filters:
            f = yield _FilterFor()
            if f is None and self.strategy == "prefilter":
                raise ValueError("strategy='prefilter' needs an authorizer that returns a filter")
        if f is not None:
            return (yield from self._prefilter(query, k, f))
        scope: Filter | None = None
        if self.backend.supports_filters and hasattr(self.authorizer, "scope_for"):
            # A necessary condition without the full answer (for example the subject's tenant):
            # post-filtering then searches only inside it, and still checks every candidate.
            scope = yield _ScopeFor()
        return (yield from self._postfilter(query, k, scope))

    def _exact_search(self) -> bool:
        """Whether the backend declares exact search (see `SearchBackend`). Only True counts."""
        return getattr(self.backend, "exact_search", False) is True

    def _supports_exact(self) -> bool:
        """Whether the backend searches exactly when asked to (see `SearchBackend`). Only True counts."""
        return getattr(self.backend, "supports_exact", False) is True

    def _prefilter(self, query: str | Query, k: int, f: Filter) -> _Plan:
        first: list[Hit] = yield _Search(query, k, f)
        first = list(first)[:k]
        hits: list[Hit] = first  # what may be returned
        others: list[Hit] = []  # checked with the hits, never returned
        shortfall: Shortfall | None = None
        fallback: Fallback | None = None
        matching: int | None = None
        if len(first) < k:
            if self._exact_search():
                shortfall = "exhausted"
            else:
                # An approximate index can stop early under a selective filter (pgvector's iterative
                # scan at hnsw.max_scan_tuples, for one) and return few hits or none. A count of the
                # chunks the filter matches tells that apart from a subject who may read little.
                if self.short_results != "report" and getattr(self.backend, "count", None) is not None:
                    matching = yield _Count(f, self._count_limit(k))
                shortfall = "unknown" if matching is None else "exhausted" if matching <= len(first) else "missed"
                fallback = self._fallback(shortfall, matching)
                if fallback in ("exact", "widened"):
                    mode: Literal["exact", "wider"] = "exact" if fallback == "exact" else "wider"
                    again: list[Hit] = yield _Search(query, k, f, mode, repair=True)
                    hits, others = self._repaired(first, list(again)[:k], k, merge=fallback == "widened")
        # k hits are taken at the backend's word. Fewer prove that nothing else matches only when
        # the search was exact, or the backend counted no more matching chunks than there are hits.
        # Truncating to k does not change this test: a backend that returned more has k here. A
        # count stops at its limit, which is at least k, so a count that stopped never settles it.
        settled = len(hits) == k or self._exact_search() or (matching is not None and len(hits) >= matching)
        # A search run again leaves the first search's other hits unreturned, but they are checked
        # too: a backend that ignored the filter the first time is still counted in violations.
        candidates = hits + others
        kept: list[Hit] = []
        examined = violations = 0
        if candidates:
            if self.recheck:
                decisions: list[bool] = yield _Check(candidates)
            else:
                # No call to the authorizer, but each hit must still match the filter the backend was
                # given: one that does not means the backend ignored it, and is dropped and counted.
                decisions = [matches(f, h.metadata) for h in candidates]
            kept = [h for h, ok in zip(hits, decisions[: len(hits)], strict=True) if ok]
            violations = decisions.count(False)
            examined = len(candidates) if self.recheck else 0
        recall, sample = yield from self._sampled_recall(query, k, f, kept, fallback)
        return RetrievalResult(
            kept,
            "prefilter",
            examined=examined,
            denied=0,
            violations=violations,
            complete=settled and violations == 0,
            shortfall=shortfall,
            fallback=fallback,
            sampled_recall=recall,
            recall_sample=sample,
        )

    def _count_limit(self, k: int) -> int:
        """How far the backend must count: past `max_exact_rows`, when that bounds an exact search,
        and otherwise to k, which tells whether more chunks match than a short result holds."""
        if self.short_results == "exact" and self.max_exact_rows is not None and self._supports_exact():
            return max(k, self.max_exact_rows + 1)
        return k

    def _fallback(self, shortfall: Shortfall, matching: int | None) -> Fallback | None:
        """What `short_results` does about a short result from an approximate search."""
        if self.short_results in ("report", "count") or shortfall == "exhausted":
            return None
        if matching is None:
            return "unsupported"  # without a count there is no miss to repair, nor a bound on a scan
        if self.short_results == "widen":
            return "widened"
        if not self._supports_exact():
            return "unsupported"
        if self.max_exact_rows is not None and matching > self.max_exact_rows:
            return "over_limit"
        return "exact"

    @staticmethod
    def _repaired(first: list[Hit], again: list[Hit], k: int, *, merge: bool) -> tuple[list[Hit], list[Hit]]:
        """The hits to return after a search run again, and the first search's other hits.

        An exact search ranks every chunk the filter matches, so its hits replace the first ones. A
        wider search can still miss chunks the first one found, so both are merged, best first.
        """
        returned = {h.id for h in again}
        rest = [h for h in first if h.id not in returned]
        if not merge:
            return again, rest
        both = sorted(again + rest, key=lambda h: h.score, reverse=True)  # stable: ties keep again first
        return both[:k], both[k:]

    def _samples(self) -> bool:
        """Whether `recall_sample_rate` applies: to a backend that can search exactly, but does not always."""
        return self.recall_sample_rate > 0 and self._supports_exact() and not self._exact_search()

    def _sampled_recall(
        self, query: str | Query, k: int, f: Filter, kept: list[Hit], fallback: Fallback | None
    ) -> Generator[_Op, Any, tuple[float | None, RecallSample | None]]:
        """For a sampled share of retrievals, the share of an exact search's top k found in `kept`,
        and what happened to the sample (see `RetrievalResult.recall_sample`)."""
        if fallback == "exact" or not self._samples():
            return None, None  # nothing to sample, or the hits already come from an exact search
        # A sample for a metric: nothing secret depends on the choice.
        if random.random() >= self.recall_sample_rate:  # noqa: S311
            return None, None
        # A sample is monitoring: whatever its count or search raises is recorded, and the hits found
        # so far are returned as if it had not run. The driver throws a failed operation in here.
        try:
            if self.max_exact_rows is not None:
                # The bound that keeps short_results="exact" from scanning a large readable set.
                if getattr(self.backend, "count", None) is None:
                    return None, "unsupported"
                matching: int | None = yield _Count(f, self.max_exact_rows + 1, repair=False)
                if matching is None:
                    return None, "unsupported"
                if matching > self.max_exact_rows:
                    return None, "over_limit"
            exact: list[Hit] = yield _Search(query, k, f, "exact")
            # The exact search's hits are never returned, and only those the filter matches are counted.
            best = [h.id for h in list(exact)[:k] if matches(f, h.metadata)]
        except Exception:
            _log.warning("wardowl's recall sample failed; the retrieval returns its hits", exc_info=True)
            return None, "failed"
        if not best:
            return None, "measured"  # there was nothing to find
        found = {h.id for h in kept}
        return sum(i in found for i in best) / len(best), "measured"

    def _postfilter(self, query: str | Query, k: int, scope: Filter | None = None) -> _Plan:
        fetch = min(k * self.overfetch, self.max_candidates)
        seen: set[str] = set()
        kept: list[Hit] = []
        examined = denied = 0
        # A candidate list shorter than the fetch ends the search, as it always has: asking the same
        # index again is not expected to find more. Whether it proves the store ran out, and so makes
        # the result complete, depends on the search. An exact backend has ranked everything in the
        # scope. An approximate index under a scope can stop early, as under a pre-filter, so its
        # short list proves nothing. Without a filter, a short list is still read as the end of the
        # store: with no filter to apply, an approximate index returns min(n, fetch) chunks, as an
        # HNSW search does when its candidate list (ef) is at least fetch long. pgvector with
        # iterative scans off is the known exception: its index scan returns at most hnsw.ef_search
        # rows (40 by default).
        short_means_exhausted = scope is None or self._exact_search()
        while True:
            candidates: list[Hit] = yield _Search(query, fetch, scope)
            candidates = list(candidates)[:fetch]  # a backend that ignores k must not widen the checks
            fresh = [h for h in candidates if h.id not in seen]
            seen.update(h.id for h in fresh)
            if fresh:
                decisions: list[bool] = yield _Check(fresh)
                for hit, ok in zip(fresh, decisions, strict=True):
                    if ok:
                        kept.append(hit)
                    else:
                        denied += 1
                examined += len(fresh)
            short = len(candidates) < fetch
            if len(kept) >= k or short or fetch >= self.max_candidates:
                break
            fetch = min(fetch * self.overfetch, self.max_candidates)
        kept.sort(key=lambda h: h.score, reverse=True)  # stable: equal scores keep backend order
        return RetrievalResult(
            kept[:k],
            "postfilter",
            examined=examined,
            denied=denied,
            violations=0,
            complete=len(kept) >= k or (short and short_means_exhausted),
        )

    # ---- drivers ---------------------------------------------------------------------------

    def _redact(self, subject: Subject, hits: list[Hit]) -> list[Hit]:
        redact: Any = getattr(self.redactor, "redact", self.redactor)
        try:
            return list(redact(subject, list(hits)))
        except Exception as exc:
            raise AuthorizationError("redaction failed") from exc

    def _target(self, op: _Search) -> tuple[Any, dict[str, Any]]:
        """The backend `op` searches, and the options its search is given: none but for an exact
        search, so that a backend without them sees the same call as ever."""
        if op.mode == "wider":
            return self.wider, {}
        options: dict[str, Any] = {"exact": True} if op.mode == "exact" else {}
        return self.backend, options

    @staticmethod
    def _counted(value: Any) -> int | None:
        """A backend's count: a whole number of chunks, or None when it cannot count them."""
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise TypeError(f"count must return a whole number of chunks or None, not {type(value).__name__}")

    def _run(self, op: _Op, subject: Subject) -> Any:
        if isinstance(op, _Search):
            backend, options = self._target(op)
            with _backend_errors("search failed"):
                return backend.search(op.query, op.k, op.filter, **options)
        if isinstance(op, _Count):
            counter: Any = self.backend
            with _backend_errors("count failed"):
                counted = counter.count(op.filter, op.limit)
            return self._counted(counted)
        if isinstance(op, _Rerank):
            rerank: Any = getattr(self.reranker, "rerank", self.reranker)
            return self._reranked(op.hits, rerank(op.query, list(op.hits)))
        if isinstance(op, _Redact):
            return self._redact(subject, op.hits)
        try:
            if isinstance(op, _FilterFor):
                return self._filter(self.authorizer.filter_for(subject, self.action))
            if isinstance(op, _ScopeFor):
                return self._filter(self.authorizer.scope_for(subject, self.action))  # type: ignore[attr-defined]
            return self._validated(op, self.authorizer.check(subject, op.hits, self.action))
        except AuthorizationError:
            raise
        except Exception as exc:  # fail closed: any authorizer failure means no results
            # A fixed message: the exception's text can name hosts or credentials, and an agent
            # framework shows the message to the model. It stays in __cause__, for the logs.
            raise AuthorizationError("authorizer failed") from exc

    async def _arun(self, op: _Op, subject: Subject) -> Any:
        if isinstance(op, _Search):
            backend, options = self._target(op)
            asearch = getattr(backend, "asearch", None)
            with _backend_errors("search failed"):
                if asearch is not None:
                    return await asearch(op.query, op.k, op.filter, **options)
                return await asyncio.to_thread(backend.search, op.query, op.k, op.filter, **options)
        if isinstance(op, _Count):
            counter: Any = self.backend
            acount = getattr(counter, "acount", None)
            with _backend_errors("count failed"):
                if acount is not None:
                    counted = await acount(op.filter, op.limit)
                else:
                    counted = await asyncio.to_thread(counter.count, op.filter, op.limit)
            return self._counted(counted)
        if isinstance(op, _Rerank):
            arerank = getattr(self.reranker, "arerank", None)
            if arerank is not None:
                return self._reranked(op.hits, await arerank(op.query, list(op.hits)))
            rerank: Any = getattr(self.reranker, "rerank", self.reranker)
            return self._reranked(op.hits, await asyncio.to_thread(rerank, op.query, list(op.hits)))
        if isinstance(op, _Redact):
            # Redactors are plain functions, often regular expressions over every hit's text: not
            # on the event loop, where they would hold up every other request.
            return await asyncio.to_thread(self._redact, subject, op.hits)
        try:
            if isinstance(op, (_FilterFor, _ScopeFor)):
                name = "filter_for" if isinstance(op, _FilterFor) else "scope_for"
                native = getattr(self.authorizer, f"a{name}", None)
                if native is not None:
                    return self._filter(await native(subject, self.action))
                return self._filter(await asyncio.to_thread(getattr(self.authorizer, name), subject, self.action))
            acheck = getattr(self.authorizer, "acheck", None)
            if acheck is not None:
                decisions = await acheck(subject, op.hits, self.action)
            else:
                decisions = await asyncio.to_thread(self.authorizer.check, subject, op.hits, self.action)
            return self._validated(op, decisions)
        except AuthorizationError:
            raise
        except Exception as exc:
            raise AuthorizationError("authorizer failed") from exc

    @staticmethod
    def _filter(value: Any) -> Filter | None:
        if value is None or isinstance(value, (In, And, Or)):
            return value
        raise AuthorizationError(f"authorizer returned {type(value).__name__}, not a filter")

    @staticmethod
    def _reranked(hits: list[Hit], ranked: Sequence[Hit]) -> list[Hit]:
        """The reranker's order and scores, applied to the chunks it was given, exactly as they were."""
        given = {h.id: h for h in hits}
        reordered: list[Hit] = []
        for hit in ranked:
            doc_id = getattr(hit, "id", None)
            original = given.pop(doc_id, None) if isinstance(doc_id, str) else None
            if original is None:
                raise ValueError("the reranker returned a chunk it was not given, or one chunk twice")
            score = float(hit.score)
            if not math.isfinite(score):
                raise ValueError("the reranker returned a score that is not a finite number")
            reordered.append(Hit(original.id, original.text, score, original.metadata))
        return reordered

    @staticmethod
    def _validated(op: _Check, decisions: Sequence[bool]) -> list[bool]:
        decisions = list(decisions)
        if len(decisions) != len(op.hits):
            raise AuthorizationError(f"authorizer returned {len(decisions)} decisions for {len(op.hits)} hits")
        if not all(isinstance(d, bool) for d in decisions):
            raise AuthorizationError("authorizer decisions must be booleans")
        return decisions

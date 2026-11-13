"""Retrieval events, for audit logs and monitoring.

Every `retrieve`/`aretrieve` call, including failed ones, produces one `RetrievalEvent` and passes
it to each observer given to `PermissionedRetriever(observers=[...])`.

Events never contain the query text, the query vector or any chunk text. If the retriever is given
an `audit_key`, the event carries an HMAC-SHA256 of the query, which lets you match repeated queries
without storing them; without a key there is no fingerprint at all, because a plain hash of a short
query can be reversed by guessing.

An observer that raises is logged on the `wardowl` logger, counted in the retriever's
`observer_failures`, and does not affect retrieval: delivery to `observers` is best-effort. An
observer given in `PermissionedRetriever(required_observers=[...])` must record every call
instead: when one raises after a retrieval that found its hits, the retrieval raises `AuditError`
and returns nothing.

An observer may also define `observer_failed(observer, event, error)`. After every observer has
had an event, the retriever calls it once for each observer that raised on that event, with that
observer, the event it was given and the exception. `wardowl.otel.OpenTelemetryMetrics` counts
them in `wardowl.observer_failures`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import IO, Any

from wardowl.types import Query

__all__ = ["AuditError", "JsonLinesAudit", "Observer", "RetrievalEvent", "query_fingerprint"]


class AuditError(RuntimeError):
    """A required observer could not record a retrieval, so nothing is returned.

    Raised by `retrieve` and `aretrieve` when an observer given in `required_observers` raises
    after the retrieval found its hits. Its message is fixed; the observer's exception, which can
    name a file or a host, stays its `__cause__` for the logs.
    """


@dataclass(frozen=True)
class RetrievalEvent:
    """One retrieval, for audit logs and monitoring. Never contains query or chunk text."""

    subject_id: str
    action: str
    k: int
    strategy: str | None
    """The strategy that ran, or None if the call failed before choosing one."""
    returned_ids: tuple[str, ...] = ()
    examined: int = 0
    denied: int = 0
    violations: int = 0
    complete: bool = False
    duration_ms: float = 0.0
    error: str | None = None
    """The exception type when the call failed, for example "AuthorizationError"."""
    query_hmac: str | None = None
    started_at: float = 0.0
    """Unix time when the call started."""
    search_ms: float = 0.0
    """Time spent in the backend, over every search (and count) of this call."""
    authorize_ms: float = 0.0
    """Time spent in the authorizer: filters, scopes and checks."""
    searches: int = 0
    """Backend searches made: one when pre-filtering, more when post-filtering fetched again, or
    when a short result or a recall sample searched again."""
    rerank_ms: float = 0.0
    """Time spent in the reranker."""
    reranked: int = 0
    """Permitted chunks given to the reranker; 0 when there is none."""
    cause: str | None = None
    """The type of the exception behind `error`, when there is one: for an `AuthorizationError`,
    what the call to the permission service raised, such as "TimeoutError" or
    "ConnectionRefusedError", or "CircuitOpenError" when an open `CircuitBreaker` refused the call.
    None when the error has no cause, as for a malformed answer."""
    shortfall: str | None = None
    """Why a pre-filtered search returned fewer hits than were asked for, as
    `RetrievalResult.shortfall`: "exhausted", "missed" or "unknown". None when it did not."""
    fallback: str | None = None
    """What `short_results` did about it, as `RetrievalResult.fallback`: "exact", "widened",
    "over_limit" or "unsupported". None when it did nothing."""
    shortfall_ms: float = 0.0
    """Time spent in the backend on a short result: counting the chunks the filter matches, and
    searching again. It is part of `search_ms`."""
    sampled_recall: float | None = None
    """For a retrieval that `recall_sample_rate` sampled, the share of an exact search's top k that
    came back, as `RetrievalResult.sampled_recall`."""
    recall_sample: str | None = None
    """What happened to a recall sample, as `RetrievalResult.recall_sample`: "measured",
    "over_limit", "unsupported" or "failed". None when the retrieval was not sampled."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["returned_ids"] = list(self.returned_ids)
        return data


Observer = Callable[[RetrievalEvent], None]


def query_fingerprint(query: str | Query, key: bytes) -> str:
    """HMAC-SHA256 of the query under `key`, in hex. Equal queries give equal fingerprints.

    A `Query` with text is fingerprinted by its text, so the same question matches whether or not
    a vector came with it. A vector-only query is fingerprinted by its vector.
    """
    q = Query.of(query)
    if q.text is not None:
        message = q.text.encode("utf-8", "surrogatepass")  # a lone surrogate must not raise here
    else:
        assert q.vector is not None
        # 0xFF never occurs in UTF-8, so no text query has the same message as a vector.
        message = b"\xff" + struct.pack(f"<{len(q.vector)}d", *q.vector)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class JsonLinesAudit:
    """Writes one JSON object per retrieval to a text stream (a file, stdout, a socket wrapper).

    It records who asked, what came back and what was denied, never the query or chunk text. A
    write or flush that fails (a full disk, a closed stream) raises: among `observers` that is
    logged and counted, and among `required_observers` it fails the retrieval with `AuditError`.
    """

    def __init__(self, stream: IO[str]) -> None:
        self.stream = stream
        self._lock = threading.Lock()

    def __call__(self, event: RetrievalEvent) -> None:
        line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()

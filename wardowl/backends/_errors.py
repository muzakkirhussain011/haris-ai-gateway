"""Errors shared by the backends."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

__all__ = ["NothingMatches", "StaleRevision"]

_T = TypeVar("_T")

_SHOWN = 5  # chunk ids a StaleRevision message names


class NothingMatches(ValueError):
    """The filter can match no chunk (an `In` with no values, or an `And` holding one), so there is
    no query to send.

    The public filter translators (`filter_to_where`, `filter_to_query`, `filter_to_qdrant`, ...)
    raise it rather than return `None` or `{}`, which store clients read as "no filter", that is,
    as "match everything". The backends catch it and return no hits without asking the store;
    code that sends a translated filter itself should do the same.
    """

    def __init__(self, message: str = "the filter matches nothing, so there is no query to send") -> None:
        super().__init__(message)


class StaleRevision(RuntimeError):
    """A write carried an older revision than one already stored for some of its chunks.

    Raised by `add` and `set_metadata` given `revision=`, on the backends that take it
    (`PgVectorBackend` and `MongoDBBackend`). `ids` are the chunks stored with a newer revision,
    which the write did not replace, and `revision` is the one the write carried. Read the source
    document again and write what it says now, with its current revision. Each backend's page says
    what a refused call leaves written.
    """

    def __init__(self, ids: Iterable[str], revision: int) -> None:
        self.ids = tuple(dict.fromkeys(ids))
        self.revision = revision
        shown = ", ".join(repr(i) for i in self.ids[:_SHOWN])
        more = f" and {len(self.ids) - _SHOWN} more" if len(self.ids) > _SHOWN else ""
        super().__init__(f"a revision newer than {revision} is stored for {shown}{more}")


def required(query: _T | None) -> _T:
    """A translation that can match something: `query` itself, or `NothingMatches` for None."""
    if query is None:
        raise NothingMatches()
    return query


def revision_number(revision: object) -> int:
    """`revision`, checked to be an integer that PostgreSQL's `bigint` and BSON's int64 hold."""
    if isinstance(revision, bool) or not isinstance(revision, int) or not -(2**63) <= revision < 2**63:
        raise ValueError("revision must be an integer from -2**63 to 2**63 - 1")
    return revision

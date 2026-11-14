"""Authorizers decide which chunks a subject may see.

An authorizer can answer two questions:

* `check(subject, hits, action)`: may the subject see each of these hits? Every authorizer must
  answer this. It is used for post-filtering and to re-check pre-filtered results.
* `filter_for(subject, action)`: a filter selecting exactly what the subject may see, or `None` if
  the rules cannot be expressed as a filter. When available, it lets the vector store do the
  filtering inside the search (pre-filtering).
* `scope_for(subject, action)` (optional): a filter every readable chunk matches, though not
  every chunk it matches is readable, such as the subject's tenant. When there is no exact
  filter, post-filtering searches only inside the scope and still checks each candidate.

An authorizer whose answers can come from data older than now, such as SpiceDB's under
`minimizeLatency`, may declare `max_staleness`: the most seconds old that data can be.
`CachingAuthorizer` reads it to know how long after `invalidate()` an answer can still predate the
change. Without the attribute, answers count as current.

`AllOf` and `AnyOf` combine authorizers, and `TenantAuthorizer` keeps each subject inside its
tenant. Any failure to decide must raise `AuthorizationError`. The retriever then returns nothing: it fails
closed and never falls back to unfiltered results.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from wardowl.filters import And, Filter, In, Or, matches
from wardowl.types import Hit, Subject

__all__ = [
    "AllOf",
    "AnyOf",
    "AuthorizationError",
    "Authorizer",
    "FunctionAuthorizer",
    "MetadataACLAuthorizer",
    "TenantAuthorizer",
    "UnsupportedSubject",
]


class AuthorizationError(RuntimeError):
    """The authorizer could not make a decision. No results are returned."""


class UnsupportedSubject(AuthorizationError, ValueError):
    """The permission service cannot represent the subject's id or one of its groups, so it was not
    asked: it would refuse every request that named them.

    An `AuthorizationError` like any other failure to decide, so retrieval fails closed, and a
    `ValueError`, since the subject is at fault rather than the service.
    """


def _named(value: Any) -> bool:
    """False for a string that is empty or only whitespace, which names no user or group."""
    return not isinstance(value, str) or bool(value.strip())


def _shown(value: str, limit: int = 64) -> str:
    """`value` for an error message: its repr, cut short after `limit` characters."""
    return repr(value) if len(value) <= limit else f"{value[:limit]!r}... ({len(value):,} characters)"


def _staleness(value: Any) -> float:
    """A declared staleness: a finite number of seconds, zero or more. None, undeclared, is 0."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"a staleness is a finite number of seconds, zero or more, not {value!r}")
    return float(value)


def _stalest(authorizers: Sequence[Any]) -> float:
    """The largest `max_staleness` that any of `authorizers` declares (0 when none does)."""
    return max(_staleness(getattr(a, "max_staleness", None)) for a in authorizers)


@runtime_checkable
class Authorizer(Protocol):
    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        """One decision per hit, in the same order."""
        ...

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """A filter matching exactly the chunks the subject may access, or None."""
        ...


class MetadataACLAuthorizer:
    """Permissions stored on each chunk, in two metadata fields.

    `allowed_users` lists user ids and `allowed_groups` lists group names. A chunk whose
    `allowed_groups` contains the `everyone` marker (default `"*"`) is readable by every subject.
    Chunks without either field are readable by nobody: missing permissions mean no access.

    A group name that is empty or only whitespace is no group, wherever it appears: the subject's
    such groups are left out of the filter, and so is an `everyone` marker of that kind, so a chunk
    that lists one is never readable through it. `Subject` accepts such names, since identity
    providers can send them; here they grant nothing.

    This is the simplest model and needs no external service. It suits documents whose
    permissions are copied onto the chunks at ingestion time.
    """

    def __init__(
        self,
        *,
        users_field: str = "allowed_users",
        groups_field: str = "allowed_groups",
        everyone: str = "*",
        actions: Sequence[str] = ("read",),
    ) -> None:
        self.users_field = users_field
        self.groups_field = groups_field
        self.everyone = everyone
        self.actions = frozenset(actions)

    def filter_for(self, subject: Subject, action: str = "read") -> Filter:
        if action not in self.actions:
            raise AuthorizationError(f"action {action!r} is not supported by this authorizer")
        # A blank name is no group: "" in a token's groups must not match "" in a chunk's.
        groups: set[str | int] = {g for g in subject.groups if _named(g)}
        if _named(self.everyone):
            groups.add(self.everyone)
        return Or(In(self.users_field, {subject.id}), In(self.groups_field, groups))

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        f = self.filter_for(subject, action)
        return [matches(f, hit.metadata) for hit in hits]


class FunctionAuthorizer:
    """Adapts your own permission logic.

    `check(subject, hits, action) -> list[bool]` is required. Pass `filter_for` as well if your
    rules can be turned into a filter; otherwise results are post-filtered.
    """

    def __init__(
        self,
        check: Callable[[Subject, Sequence[Hit], str], Sequence[bool]],
        filter_for: Callable[[Subject, str], Filter | None] | None = None,
    ) -> None:
        self._check = check
        self._filter_for = filter_for

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        # No bool() coercion: a buggy function returning "no" must not become True. The retriever
        # rejects anything that is not a real bool and fails closed.
        return list(self._check(subject, hits, action))

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        return self._filter_for(subject, action) if self._filter_for else None


# ---- composition -------------------------------------------------------------------------------


def _answer(authorizer: Authorizer, decisions: Sequence[bool], expected: int) -> list[bool]:
    answer = list(decisions)
    if len(answer) != expected or not all(isinstance(d, bool) for d in answer):
        raise AuthorizationError(f"{type(authorizer).__name__} gave {len(answer)} decisions, or non-booleans")
    return answer


async def _acall(authorizer: Authorizer, name: str, *args: Any) -> Any:
    native = getattr(authorizer, f"a{name}", None)
    if native is not None:
        return await native(*args)
    method = getattr(authorizer, name, None)
    return None if method is None else await asyncio.to_thread(method, *args)


def _join(op: type[And] | type[Or], filters: list[Any]) -> Filter:
    """The parts' filters joined with `op`, once each is checked to be a filter.

    `And` and `Or` take any clause, so a part's answer that is not a filter would otherwise pass
    inside them as one. The retriever checks the whole tree it is given as well; this check also
    covers code that calls `filter_for` or `scope_for` itself.
    """
    for f in filters:
        if not isinstance(f, (In, And, Or)):
            combined = "AllOf" if op is And else "AnyOf"
            raise AuthorizationError(f"an authorizer in {combined} returned {type(f).__name__}, not a filter")
    return filters[0] if len(filters) == 1 else op(*filters)


def _exact_or_scope(authorizer: Authorizer, subject: Subject, action: str) -> Filter | None:
    exact = authorizer.filter_for(subject, action)
    if exact is not None:
        return exact
    scope = getattr(authorizer, "scope_for", None)
    return scope(subject, action) if scope is not None else None


class AllOf:
    """Allows a chunk only when every authorizer allows it.

    For example tenant isolation plus fine-grained permissions:
    `AllOf(TenantAuthorizer(), OpenFGAAuthorizer(...))`. Authorizers are asked in order, each only
    about the chunks every earlier one allowed, so put cheap ones first. The pre-filter is the
    conjunction of every authorizer's filter, or none when one of them has none; `scope_for` then
    gives the conjunction of the filters that do exist, so post-filtering searches only inside
    them (for example only the subject's tenant). `max_staleness` is the largest any of them
    declares.
    """

    def __init__(self, *authorizers: Authorizer) -> None:
        if not authorizers:
            raise ValueError("AllOf needs at least one authorizer")
        self.authorizers = authorizers

    @property
    def max_staleness(self) -> float:
        """The largest `max_staleness` of the authorizers combined: an answer is as old as its oldest part."""
        return _stalest(self.authorizers)

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [True] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if ok]
            if not todo:
                break
            answer = _answer(authorizer, authorizer.check(subject, [hits[i] for i in todo], action), len(todo))
            for i, ok in zip(todo, answer, strict=True):
                allowed[i] = ok
        return allowed

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [a.filter_for(subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(And, exact) if len(exact) == len(filters) else None

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        parts = [f for a in self.authorizers if (f := _exact_or_scope(a, subject, action)) is not None]
        return _join(And, parts) if parts else None

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [True] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if ok]
            if not todo:
                break
            decisions = await _acall(authorizer, "check", subject, [hits[i] for i in todo], action)
            for i, ok in zip(todo, _answer(authorizer, decisions, len(todo)), strict=True):
                allowed[i] = ok
        return allowed

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [await _acall(a, "filter_for", subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(And, exact) if len(exact) == len(filters) else None


class AnyOf:
    """Allows a chunk when at least one authorizer allows it.

    For example documents shared directly or through a policy:
    `AnyOf(MetadataACLAuthorizer(), OPAAuthorizer(...))`. Authorizers are asked in order, each only
    about the chunks every earlier one denied. The pre-filter is the disjunction of every
    authorizer's filter, or none when one of them has none; `scope_for` is the disjunction of
    every authorizer's filter or scope, or none when one has neither. `max_staleness` is the
    largest any of them declares.
    """

    def __init__(self, *authorizers: Authorizer) -> None:
        if not authorizers:
            raise ValueError("AnyOf needs at least one authorizer")
        self.authorizers = authorizers

    @property
    def max_staleness(self) -> float:
        """The largest `max_staleness` of the authorizers combined: an answer is as old as its oldest part."""
        return _stalest(self.authorizers)

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [False] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if not ok]
            if not todo:
                break
            answer = _answer(authorizer, authorizer.check(subject, [hits[i] for i in todo], action), len(todo))
            for i, ok in zip(todo, answer, strict=True):
                allowed[i] = ok
        return allowed

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [a.filter_for(subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(Or, exact) if len(exact) == len(filters) else None

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        parts = [_exact_or_scope(a, subject, action) for a in self.authorizers]
        known = [p for p in parts if p is not None]
        return _join(Or, known) if len(known) == len(parts) else None

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [False] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if not ok]
            if not todo:
                break
            decisions = await _acall(authorizer, "check", subject, [hits[i] for i in todo], action)
            for i, ok in zip(todo, _answer(authorizer, decisions, len(todo)), strict=True):
                allowed[i] = ok
        return allowed

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [await _acall(a, "filter_for", subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(Or, exact) if len(exact) == len(filters) else None


class TenantAuthorizer:
    """Chunks are readable only within the subject's tenant.

    A chunk is readable when its metadata field `field` holds the subject's attribute `attribute`
    (both `"tenant"` by default). A subject without that attribute raises `AuthorizationError`, so
    a request that lost its tenant returns nothing rather than everything. Combine it with
    fine-grained permissions: `AllOf(TenantAuthorizer(), OpenFGAAuthorizer(...))`.
    """

    def __init__(self, field: str = "tenant", attribute: str | None = None) -> None:
        if not field:
            raise ValueError("field must be non-empty")
        self.field = field
        self.attribute = attribute or field

    def filter_for(self, subject: Subject, action: str = "read") -> Filter:
        tenant = subject.attributes.get(self.attribute)
        if isinstance(tenant, bool) or not isinstance(tenant, (str, int)) or tenant == "":
            raise AuthorizationError(f"subject {subject.id!r} has no {self.attribute!r} attribute")
        return In(self.field, [tenant])

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        f = self.filter_for(subject, action)
        return [matches(f, hit.metadata) for hit in hits]

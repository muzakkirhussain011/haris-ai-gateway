"""SpiceDB authorizer (Zanzibar-style relationship-based access control).

No extra dependency: it calls SpiceDB's HTTP API (`spicedb serve --http-enabled`) with the standard
library.

* `check` asks CheckBulkPermissions whether `user:<subject id>` has the permission (default `view`)
  on `document:<resource id>` for every chunk, in batches.
* `filter_for` asks LookupResources for every document the user may view and turns the answer into
  an `In` filter, so the vector store pre-filters. If the answer reaches `lookup_limit` it may be
  truncated, so no filter is returned and the retriever post-filters instead. An answer with no
  document returns no filter either: SpiceDB marks no end to the stream, so one that a proxy or a
  fault ended early cannot be told from a subject who may read nothing, and post-filtering lets
  CheckBulkPermissions decide every candidate (`trust_empty_lookup=True` takes it as nothing).
* Only the user is checked by default (`group_subjects=False`): SpiceDB follows the group
  memberships it stores, and applies exclusions (`viewer - banned`) and intersections to them.
  With `group_subjects=True` the subject's groups (from your identity provider, say) are also
  checked as subject sets, `group:<name>#member`, and a chunk is allowed if the user or any group
  has the permission, for memberships that live only in the token. Each group is then asked about
  on its own, so a permission that excludes or intersects is wrong: a banned user still reads what
  their group may read, and a grant that needs two conditions together can be refused. Do not use
  it with such permissions.
* A conditional answer (a caveat whose context was not supplied) is not a decision: `check` raises
  `AuthorizationError`, and `filter_for` returns no filter so the chunks are checked one by one.
  Supply caveat context with `caveat_context`.

The resource id is `hit.metadata[resource_field]` (a document id shared by its chunks), or the chunk
id when `resource_field` is None; a chunk without it, or whose field is not one string, is denied.
The `In` pre-filter matches a list field by any of its elements, which `check` never allows, so keep
the field one string on every chunk: with `recheck=False` the retriever trusts the filter alone.
Subject ids and group names travel as separate JSON fields, never inside a `type:id#relation`
string, so a `:` or `#` in one cannot change its type or relation. SpiceDB object ids are 1 to 1024
letters, digits and `/_|-=+`: store such an id in `resource_field` and use such subject ids. A chunk
whose resource id is not one is denied without being sent, since SpiceDB would refuse the whole
request. A subject id that is not one, or with `group_subjects=True` a group name, raises
`UnsupportedSubject` (an `AuthorizationError`) before anything is sent, since SpiceDB would refuse
every request that named it. Every failure raises `AuthorizationError`. Connections are reused,
transient failures are retried once, and a `CircuitBreaker` can pause requests to a failing server;
see `wardowl.authorizers`.

Consistency: the default, `{"minimizeLatency": true}`, lets SpiceDB answer from a recent snapshot,
so a revoked permission can keep granting access for a while. SpiceDB documents the bound as its
revision quantization interval plus a share of it (`--datastore-revision-quantization-interval`,
5 s, and `--datastore-revision-quantization-max-staleness-percent`, 10%: 5.5 s by default), plus
the follower read delay when it reads from replicas. Measured against SpiceDB 1.56 on Postgres with
the defaults, it reached 8.7 s, and successive answers moved backwards: a revoked permission granted
access again after it had stopped. `max_staleness` declares twice the documented bound, 11 s, which
`CachingAuthorizer` reads; pass `max_staleness=` for a server with other settings. Pass
`consistency={"fullyConsistent": True}` (`max_staleness` 0) for answers that include every change.
`{"atLeastAsFresh": {"token": zedtoken}}` gets answers from data at least as fresh as the write the
token came from, and SpiceDB guarantees nothing more
(https://authzed.com/docs/spicedb/concepts/consistency): a token fixed when the authorizer is
created covers no later write. For the token of the latest write, pass `consistency` a function of
no arguments that returns the requirement. It is called once for each `check` or `filter_for` that
sends a request, and all the requests of that call use what it returned. It must return exactly one
of SpiceDB's four requirements, with a non-empty token for `atLeastAsFresh` and `atExactSnapshot`;
anything else, or an exception, raises `AuthorizationError` before anything is sent. A mapping is
sent as given, as before. `max_staleness` stays 11 with a function unless you pass it. Declare 0
only when every request carries a token at least as new as the latest change you report with
`CachingAuthorizer.invalidate()`, kept before you call it; a token fixed at creation does not do
that (docs/integrations/spicedb.md, "The token of the latest write").
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wardowl.authorizers._http import DEFAULT_MAX_RESPONSE_BYTES, CircuitBreaker, JsonClient
from wardowl.authz import AuthorizationError, UnsupportedSubject, _shown, _staleness
from wardowl.filters import Filter, In
from wardowl.types import Hit, Subject

__all__ = ["SpiceDBAuthorizer"]

# How stale a read that is not fullyConsistent can be, with `spicedb serve`'s defaults. SpiceDB
# rounds the revision such a read uses down to --datastore-revision-quantization-interval (5 s), and
# may pick one older still by --datastore-revision-quantization-max-staleness-percent of it (0.1):
# 5.5 s in all (https://authzed.com/docs/spicedb/concepts/commands). SpiceDB 1.56.2 on Postgres was
# measured up to 8.7 s stale with those defaults, so twice that bound is declared: 2 x 5 x 1.1 s.
_DEFAULT_STALENESS = 11.0

_HAS = "PERMISSIONSHIP_HAS_PERMISSION"
_NO = "PERMISSIONSHIP_NO_PERMISSION"
_LOOKUP_HAS = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
_LOOKUP_CONDITIONAL = "LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION"
# ObjectReference.object_id in SpiceDB's API (authzed/api, core.proto), less "*", the subjects' wildcard.
_OBJECT_ID = re.compile(r"[a-zA-Z0-9/_|\-=+]{1,1024}")
# The Consistency message in SpiceDB's API (authzed/api, permission_service.proto): a required oneof
# of two flags that must be true and two ZedTokens, whose token must be at least one byte (core.proto).
_FLAGS = ("minimizeLatency", "fullyConsistent")
_AT_TOKEN = ("atLeastAsFresh", "atExactSnapshot")


def _requirement(value: object) -> dict[str, Any]:
    """A fresh copy of `value`, a consistency a function returned, checked to be one SpiceDB accepts.

    Anything else raises `AuthorizationError`, so nothing is sent: None, for one, would go out as no
    consistency, which SpiceDB answers with its default for checks and lookups, `minimizeLatency`.
    """
    if isinstance(value, Mapping) and len(value) == 1:
        ((option, setting),) = value.items()
        if option in _FLAGS and setting is True:
            return {option: True}
        if option in _AT_TOKEN and isinstance(setting, Mapping) and set(setting) == {"token"}:
            token = setting["token"]
            if isinstance(token, str) and token:
                return {option: {"token": token}}
    kind = "mapping" if isinstance(value, Mapping) else type(value).__name__
    raise AuthorizationError(
        f"consistency returned a {kind}, which is not one SpiceDB consistency: it must hold exactly one of"
        " minimizeLatency or fullyConsistent set to True, or atLeastAsFresh or atExactSnapshot with a"
        " non-empty token string"
    )


def _object_id(kind: str, value: str) -> str:
    """`value`, checked to be a SpiceDB object id: SpiceDB refuses every request that names another."""
    if not _OBJECT_ID.fullmatch(value):
        raise UnsupportedSubject(
            f"{kind} {_shown(value)} cannot be sent to SpiceDB: object ids are 1 to 1024 letters, digits and /_|-=+"
        )
    return value


def _key(resource: Mapping[str, Any], subject: Mapping[str, Any]) -> tuple[Any, ...]:
    obj = subject.get("object") or {}
    return (resource.get("objectId"), obj.get("objectType"), obj.get("objectId"), subject.get("optionalRelation") or "")


class SpiceDBAuthorizer:
    """Asks SpiceDB for decisions over its HTTP API.

    `token` is SpiceDB's preshared key (sent as a bearer token). `permissions` maps actions to
    SpiceDB permissions (default `{"read": "view"}`). Resources are `<resource_type>:<resource id>`
    and subjects `<subject_type>:<subject id>`, plus `<group_type>:<group>#<group_relation>` for
    each of the subject's groups when `group_subjects` is true (False by default; never with a
    permission that excludes or intersects, as the module docstring explains).
    `caveat_context(subject)` returns the context for caveated relationships. `timeout`,
    `retries`, `circuit_breaker`, `ssl_context`, `max_response_bytes` and `allow_insecure` work as
    in `OPAAuthorizer`: a `token` sent over http:// to a host other than this machine needs
    `allow_insecure=True`. Every failure raises `AuthorizationError`.

    A `check` sends one CheckBulkPermissions request per `batch_size` (resource, subject) pairs,
    and `filter_for` one LookupResources request per subject (the user, and each group with
    `group_subjects`), one after another. `timeout` bounds each request; `total_timeout` (seconds;
    None, the default, for no bound) bounds all the requests of one `check` or `filter_for`
    together, so that the call raises `AuthorizationError` once it runs out.

    `consistency` is the requirement sent with every request: a mapping (by default
    `{"minimizeLatency": True}`), sent as given; or a function of no arguments that returns one,
    called once for each `check` or `filter_for` that sends SpiceDB a request, before the first,
    so that all the requests of the call (every batch, every subject's lookup) use the same one.
    What the function returns must be exactly one of `{"minimizeLatency": True}`,
    `{"fullyConsistent": True}`, `{"atLeastAsFresh": {"token": t}}` or
    `{"atExactSnapshot": {"token": t}}`, with `t` a non-empty string; anything else, or an
    exception from it, raises `AuthorizationError` and nothing is sent. It is called from the
    threads that serve retrievals, so what it reads must be safe to share between threads.

    `max_staleness` is how many seconds old SpiceDB's answers can be: 0 with the mapping
    `{"fullyConsistent": True}`, and otherwise 11 by default, a function included whatever it
    returns: twice the bound SpiceDB documents for its default quantization settings (see the
    module docstring). Pass it for a server with other settings, or 0 when every request carries
    `atLeastAsFresh` with a token at least as new as the latest change you report with
    `CachingAuthorizer.invalidate()`, kept before you call it. A function can return such a token;
    a mapping fixed when the authorizer is created cannot. `trust_empty_lookup=True` takes a
    LookupResources answer with no document as "nothing is readable" and returns a filter that
    matches nothing, as earlier versions did; by default it returns no filter, so the retriever
    post-filters.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        permissions: Mapping[str, str] | None = None,
        resource_type: str = "document",
        subject_type: str = "user",
        resource_field: str | None = "doc_id",
        group_type: str = "group",
        group_relation: str = "member",
        group_subjects: bool = False,
        consistency: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        max_staleness: float | None = None,
        caveat_context: Callable[[Subject], Mapping[str, Any]] | None = None,
        lookup_limit: int = 1000,
        trust_empty_lookup: bool = False,
        batch_size: int = 100,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
        total_timeout: float | None = None,
    ) -> None:
        if batch_size < 1 or lookup_limit < 1:
            raise ValueError("batch_size and lookup_limit must be positive")
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = JsonClient(
            url,
            service="SpiceDB",
            timeout=timeout,
            headers={**auth, **(headers or {})},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
            max_response_bytes=max_response_bytes,
            allow_insecure=allow_insecure,
            total_timeout=total_timeout,
        )
        self.permissions = dict(permissions or {"read": "view"})
        self.resource_type = resource_type
        self.subject_type = subject_type
        self.resource_field = resource_field
        self.group_type = group_type
        self.group_relation = group_relation
        self.group_subjects = group_subjects
        self.consistency: dict[str, Any] | Callable[[], Mapping[str, Any]]
        if isinstance(consistency, Mapping) or not callable(consistency):
            self.consistency = dict(consistency or {"minimizeLatency": True})  # as before
        else:
            self.consistency = consistency  # called for each check and filter_for
        self._max_staleness = None if max_staleness is None else _staleness(max_staleness)
        self.caveat_context = caveat_context
        self.lookup_limit = lookup_limit
        self.trust_empty_lookup = trust_empty_lookup
        self.batch_size = batch_size

    @property
    def max_staleness(self) -> float:
        """How many seconds old SpiceDB's answers can be: as passed, or 0 for the mapping
        `{"fullyConsistent": True}` and 11 for any other consistency, which may answer from a
        quantized revision. A function is never called here: what it returns can change, so it
        declares 11 whatever it returns now."""
        if self._max_staleness is not None:
            return self._max_staleness
        consistency = self.consistency
        if not isinstance(consistency, dict):  # a function
            return _DEFAULT_STALENESS
        return 0.0 if consistency.get("fullyConsistent") is True else _DEFAULT_STALENESS

    # ---- helpers ---------------------------------------------------------------------------

    def _permission(self, action: str) -> str:
        if action not in self.permissions:
            raise AuthorizationError(f"no SpiceDB permission is configured for action {action!r}")
        return self.permissions[action]

    def _subjects(self, subject: Subject) -> list[dict[str, Any]]:
        """The subjects to ask about: the user, then each group when `group_subjects` is true. Raises
        `UnsupportedSubject` for an id or group name SpiceDB would refuse, before anything is sent."""
        user = _object_id("subject id", subject.id)
        refs: list[dict[str, Any]] = [{"object": {"objectType": self.subject_type, "objectId": user}}]
        if self.group_subjects:
            refs += [
                {
                    "object": {"objectType": self.group_type, "objectId": _object_id("group", g)},
                    "optionalRelation": self.group_relation,
                }
                for g in sorted(subject.groups)
            ]
        return refs

    def _consistency(self) -> dict[str, Any]:
        """The requirement for every request of one call: the mapping as given, or what the
        function returns now, checked. Raises `AuthorizationError` rather than send a weaker one."""
        consistency = self.consistency
        if isinstance(consistency, dict):
            return consistency
        try:
            value = consistency()
        except Exception as exc:
            raise AuthorizationError("consistency failed") from exc
        return _requirement(value)

    def _context(self, subject: Subject) -> dict[str, Any]:
        if self.caveat_context is None:
            return {}
        try:
            return {"context": dict(self.caveat_context(subject))}
        except Exception as exc:
            raise AuthorizationError("caveat_context failed") from exc

    def _resource(self, hit: Hit) -> str | None:
        """The chunk's resource id, or None (the chunk is denied without asking) when it is missing,
        not one string, or not a SpiceDB object id, which would fail the whole request with it."""
        value = hit.id if self.resource_field is None else hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and _OBJECT_ID.fullmatch(value) else None

    # ---- Authorizer ------------------------------------------------------------------------

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        permission = self._permission(action)
        subjects = self._subjects(subject)
        resources = [self._resource(h) for h in hits]
        unique = sorted({r for r in resources if r is not None})
        context = self._context(subject)
        pairs = [(r, s) for r in unique for s in subjects]
        if not pairs:
            return [False] * len(resources)  # no chunk has a resource id to ask about
        allowed = dict.fromkeys(unique, False)
        consistency = self._consistency()  # one for every batch of this check
        deadline = self._client.deadline()  # one for every batch of this check
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            items = [
                {
                    "resource": {"objectType": self.resource_type, "objectId": r},
                    "permission": permission,
                    "subject": s,
                    **context,
                }
                for r, s in batch
            ]
            answer = self._client.post_json(
                "/v1/permissions/checkbulk",
                {"consistency": consistency, "items": items},
                "SpiceDB CheckBulkPermissions",
                deadline=deadline,
            )
            results = answer.get("pairs") if isinstance(answer, dict) else None
            if not isinstance(results, list):
                raise AuthorizationError("SpiceDB CheckBulkPermissions returned an unexpected response")
            # Match answers to questions by the request each pair echoes, not by position.
            decided: dict[tuple[Any, ...], Any] = {}
            for pair in results:
                request = pair.get("request") if isinstance(pair, dict) else None
                if not isinstance(request, dict):
                    raise AuthorizationError("SpiceDB CheckBulkPermissions returned a pair without its request")
                key = _key(request.get("resource") or {}, request.get("subject") or {})
                if pair.get("error"):
                    raise AuthorizationError(f"SpiceDB could not decide {key!r}: {pair['error']}")
                decided[key] = (pair.get("item") or {}).get("permissionship")
            for r, s in batch:
                permissionship = decided.get(_key({"objectId": r}, s))
                if permissionship == _HAS:
                    allowed[r] = True
                elif permissionship != _NO:
                    raise AuthorizationError(
                        f"SpiceDB returned {permissionship!r} for {r!r}: no answer, or a caveat that needs context"
                    )
        return [r is not None and allowed[r] for r in resources]

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if self.resource_field is None:
            return None
        permission = self._permission(action)
        context = self._context(subject)
        subjects = self._subjects(subject)
        found: set[str] = set()
        consistency = self._consistency()  # one for every subject's lookup
        deadline = self._client.deadline()  # one for every subject's lookup
        for s in subjects:
            payload = {
                "consistency": consistency,
                "resourceObjectType": self.resource_type,
                "permission": permission,
                "subject": s,
                "optionalLimit": self.lookup_limit,
                **context,
            }
            lines = self._client.post_json(
                "/v1/permissions/resources", payload, "SpiceDB LookupResources", lines=True, deadline=deadline
            )
            for line in lines:
                result = line.get("result") if isinstance(line, dict) else None
                if not isinstance(result, dict) or line.get("error"):
                    raise AuthorizationError(f"SpiceDB LookupResources failed: {line!r}")
                resource, permissionship = result.get("resourceObjectId"), result.get("permissionship")
                if not isinstance(resource, str):
                    raise AuthorizationError("SpiceDB LookupResources returned a result without a resource id")
                if permissionship == _LOOKUP_CONDITIONAL:
                    return None  # decided per chunk, where the caveat is evaluated (or refused)
                if permissionship != _LOOKUP_HAS:
                    raise AuthorizationError(f"SpiceDB LookupResources returned {permissionship!r}")
                found.add(resource)
            if len(lines) >= self.lookup_limit:
                return None  # possibly truncated: post-filter instead of trusting an incomplete list
        if not found and not self.trust_empty_lookup:
            # The stream has no end marker: one a proxy or a server fault cut off before its first
            # result reads as no readable document. Post-filtering lets the check decide instead.
            return None
        return In(self.resource_field, found)

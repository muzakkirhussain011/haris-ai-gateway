"""An HTTP service for permission-aware retrieval, for applications in any language.

Needs the `server` extra (`pip install "wardowl[server]" uvicorn`). Build the retriever in
Python, as anywhere else, and serve it:

    from wardowl.server import OIDCAuthenticator, create_app

    authenticate = OIDCAuthenticator(
        issuer="https://login.example.com/",
        audience="knowledge-api",
        jwks_url="https://login.example.com/.well-known/jwks.json",
    )
    app = create_app(retriever, authenticate)  # then: uvicorn service:app

`POST /v1/retrieve` takes `{"query": "...", "k": 5}` (or a `vector` you computed) with an
`Authorization: Bearer <token>` header, and returns the hits the token's subject may read with
`complete`. `GET /healthz` answers liveness probes, and `/openapi.json` describes the API for
generating clients in other languages.

How it holds:

1. The subject comes from the verified bearer token alone. The request body cannot name a user,
   a group or a tenant: unknown fields are refused with 422.
2. A missing or invalid token answers 401, and the caller is not told why; `OIDCAuthenticator` logs
   the check a token failed at INFO on the `wardowl` logger, never the token. A permission check
   that fails answers 503 with no details (they go to the `wardowl` logger), and nothing is
   returned. A query the backend cannot search as sent (`InvalidQuery`: a vector of the wrong
   length, say) answers 422, echoing nothing. A search or count the store could not complete
   (`SearchError`) answers 500 with a fixed message, and the store's error goes to the `wardowl`
   logger; any other failure is a server fault and answers 500 too.
3. Hits carry no metadata unless a field is listed in `metadata_fields`.
4. `k`, the query's length and the vector's length are bounded, the vector holds finite numbers
   only, a body over `max_body_bytes` is refused with 413 before it is parsed, and the query text
   is never logged.
5. A synchronous authenticator, such as `OIDCAuthenticator`, runs in a worker thread, so a slow
   identity provider never stalls the event loop.
6. With `retrieval_timeout`, a retrieval that runs longer is cancelled and answers 503, and nothing
   is returned. `max_workers` sizes the thread pool that synchronous stores, authorizers and
   observers run in.
"""

# No `from __future__ import annotations`: FastAPI resolves string annotations against the module's
# globals, so the request models defined inside `create_app` must be real annotations.

import asyncio
import contextlib
import datetime
import http.client
import inspect
import logging
import math
import ssl
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

from wardowl._timeout import TimedOut, checked, within
from wardowl.authz import AuthorizationError
from wardowl.retriever import PermissionedRetriever, SearchError
from wardowl.types import InvalidQuery, Query, Subject

try:
    import jwt
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - exercised without the extra installed
    raise ImportError('wardowl.server needs the server extra: pip install "wardowl[server]"') from exc

__all__ = ["Authenticator", "InvalidToken", "OIDCAuthenticator", "create_app"]

_log = logging.getLogger("wardowl")

# Turns a bearer token into the `Subject` it was issued for, or raises `InvalidToken`.
Authenticator = Callable[[str], Subject | Awaitable[Subject]]


class InvalidToken(Exception):
    """The bearer token is missing, malformed, expired or not issued for this service."""


def _refused(reason: str, message: str | None = None) -> InvalidToken:
    """An `InvalidToken` to raise, logged first with `reason`: the check the token failed, which
    never holds the token or a claim's value."""
    _log.info("wardowl refused a token: %s", reason)
    return InvalidToken(reason if message is None else message)


def _failed_check(exc: jwt.PyJWTError) -> str:
    """The check PyJWT refused a token for: its exception's class, and for a missing claim the
    claim's name, which this module asked for. Never PyJWT's message, nor anything from the token."""
    if isinstance(exc, jwt.MissingRequiredClaimError):
        return f"{type(exc).__name__} ({exc.claim})"
    return type(exc).__name__


class OIDCAuthenticator:
    """Verifies JWT access tokens from an OpenID Connect provider against its published keys.

    The token must name (`kid`) a key in `jwks_url` whose algorithm is one of `algorithms`
    (asymmetric only, and never taken from the token), be signed by it, and be issued by `issuer`
    for `audience`. Its times are checked against this machine's clock: `exp` (expiry) must be in
    the future, and `nbf` (not before) and `iat` (issued at), when the token has them, must not be.
    `leeway` (seconds, 0 by default) widens all three checks: a token is accepted up to `leeway`
    seconds after its `exp` and up to `leeway` seconds before its `nbf` or `iat`. With the default,
    an identity provider whose clock runs ahead of this machine's issues tokens that are refused
    until this clock catches up. Where the clocks are not tightly synchronized, a few seconds of
    leeway avoids that, at the cost of accepting expired tokens for those seconds.

    The subject is the `sub` claim, and its groups the list in `groups_claim`. A token without that
    claim gives a subject with no groups, and the absence is logged at DEBUG on the `wardowl`
    logger; with `require_groups`, such a token is refused instead. An empty list is a claim, and
    passes either way. When `tenant_claim` is set, the subject's `tenant` attribute is the string in
    that claim, which must then be present.

    A token that fails any check raises `InvalidToken`, and the check it failed is logged at INFO
    on the `wardowl` logger: PyJWT's exception class, such as `ExpiredSignatureError` or
    `ImmatureSignatureError` (with the claim's name for `MissingRequiredClaimError`), or wardowl's
    own reason. The token and its claims' values are never logged.

    The key set is fetched with a `timeout` (seconds) and kept for `lifespan` seconds, so a key the
    provider has rotated out stops verifying within that time. A token naming a key that is not in
    the set fetches it again, at most once every `cooldown` seconds; until then such a token is
    invalid at once. A key endpoint that cannot be reached, or that returns no usable keys, raises
    `AuthorizationError`. Calls are synchronous: `create_app` runs them in a worker thread.

    The key set is fetched over HTTPS with the system's trust store; `ssl_context` replaces it, for
    an identity provider whose certificate a private CA signed.
    """

    def __init__(
        self,
        issuer: str,
        audience: str | Sequence[str],
        jwks_url: str,
        *,
        algorithms: Sequence[str] = ("RS256", "ES256"),
        groups_claim: str = "groups",
        require_groups: bool = False,
        tenant_claim: str | None = None,
        leeway: float | datetime.timedelta = 0,
        lifespan: float = 300,
        cooldown: float = 30,
        timeout: float = 5,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not jwks_url.startswith("https://"):
            raise ValueError("jwks_url must be an https:// URL")
        if not algorithms or any(a.upper().startswith(("HS", "NONE")) for a in algorithms):
            raise ValueError("algorithms must be asymmetric, such as RS256 or ES256")
        if not (timeout > 0 and cooldown > 0 and lifespan >= cooldown):
            raise ValueError("timeout and cooldown must be positive, and lifespan at least cooldown")
        if isinstance(leeway, datetime.timedelta):  # PyJWT takes either
            leeway = leeway.total_seconds()
        # NaN makes every one of PyJWT's time comparisons false, so expired tokens would verify;
        # an infinite leeway turns the expiry check off. Neither is a clock-skew allowance.
        if not math.isfinite(leeway):
            raise ValueError("leeway must be a finite number of seconds")
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms)
        self.groups_claim = groups_claim
        self.require_groups = require_groups
        self.tenant_claim = tenant_claim
        self.leeway = leeway
        self.lifespan = lifespan
        self.cooldown = cooldown
        # The key set is cached here, not by PyJWKClient: its per-key cache never expires, and
        # older releases refetch the whole set for every unknown key id.
        self._client = jwt.PyJWKClient(
            jwks_url, cache_keys=False, cache_jwk_set=False, timeout=timeout, ssl_context=ssl_context
        )
        self._clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires = -math.inf  # when the cached key set goes stale
        self._next_fetch = -math.inf  # the earliest time the key endpoint may be asked again

    def __call__(self, token: str) -> Subject:
        key = self._signing_key(token)
        algorithm = key.algorithm_name
        if not isinstance(algorithm, str) or algorithm not in self.algorithms:
            raise _refused(f"the signing key's algorithm {algorithm} is not allowed")
        # PyJWT refuses a required claim that is absent or null; an empty list passes.
        required = ["exp", "iss", "aud", "sub"]
        if self.tenant_claim:
            required.append(self.tenant_claim)
        if self.require_groups:
            required.append(self.groups_claim)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],  # the key's own algorithm, whatever the token's header says
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,  # for exp, and for nbf and iat when the token has them
                options={"require": required},
            )
        except jwt.PyJWTError as exc:
            raise _refused(_failed_check(exc), str(exc)) from exc
        return self.subject(claims)

    def _signing_key(self, token: str) -> jwt.PyJWK:
        """The published key the token names, refetching the key set within the limits above."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise _refused(_failed_check(exc), str(exc)) from exc
        if not isinstance(kid, str) or not kid:
            raise _refused("the token names no signing key (kid)")
        keys = self._keys
        if self._clock() < self._expires and kid in keys:
            return keys[kid]
        with self._lock:
            now = self._clock()
            stale = now >= self._expires
            if (stale or kid not in self._keys) and now >= self._next_fetch:
                self._next_fetch = now + self.cooldown  # also after a failed fetch
                self._keys = self._fetch()
                self._expires = self._clock() + self.lifespan
            elif stale:  # the last fetch failed, and the next is not due yet: fail closed
                raise AuthorizationError("the identity provider's keys could not be fetched; retrying shortly")
            key = self._keys.get(kid)
        if key is None:
            raise _refused("no key published by the identity provider matches the token's kid")
        return key

    def _fetch(self) -> dict[str, jwt.PyJWK]:
        try:
            published = self._client.get_signing_keys(refresh=True)
        except (jwt.PyJWTError, OSError, ValueError, http.client.HTTPException) as exc:
            raise AuthorizationError("the identity provider's keys could not be fetched") from exc
        keys: dict[str, jwt.PyJWK] = {}
        for key in published:
            kid = key.key_id
            if kid:
                keys[kid] = key
        return keys

    def subject(self, claims: Mapping[str, Any]) -> Subject:
        """The subject a verified token's claims describe.

        Without the groups claim, the subject has no groups, or, with `require_groups`, the claims
        raise `InvalidToken`.
        """
        user = claims.get("sub")
        if not isinstance(user, str) or not user:
            raise _refused("the token's sub claim is not a user id")
        if self.groups_claim in claims:
            groups = claims[self.groups_claim]
        elif self.require_groups:
            raise _refused(f"the token has no {self.groups_claim} claim")
        else:
            # A provider that sends no groups for a user is indistinguishable here from one whose
            # mapper went missing, or that left a long group list out: say so, for the operator.
            _log.debug("wardowl found no %s claim in a token; its subject has no groups", self.groups_claim)
            groups = []
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise _refused(f"the token's {self.groups_claim} claim is not a list of strings")
        attributes: dict[str, str] = {}
        if self.tenant_claim:
            tenant = claims.get(self.tenant_claim)
            if not isinstance(tenant, str) or not tenant:
                raise _refused(f"the token's {self.tenant_claim} claim is not a tenant")
            attributes["tenant"] = tenant
        return Subject(user, groups, attributes)


class _BodyLimit:
    """Refuses a request body over `limit` bytes with 413 before the application parses it, by its
    Content-Length and by the bytes actually received, then hands the body on."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = JSONResponse({"detail": "the request body is too large"}, status_code=413)
        declared = dict(scope.get("headers", [])).get(b"content-length", b"")
        if declared.isdigit() and (len(declared) > 18 or int(declared) > self.limit):
            await too_large(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            body += message.get("body", b"")
            if len(body) > self.limit:
                await too_large(scope, receive, send)
                return
            more = bool(message.get("more_body", False))
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def create_app(
    retriever: PermissionedRetriever,
    authenticate: Authenticator,
    *,
    max_k: int = 50,
    max_query_length: int = 4000,
    max_vector_length: int = 4096,
    max_body_bytes: int = 256 * 1024,
    metadata_fields: Collection[str] = (),
    title: str = "wardowl",
    docs: bool = False,
    retrieval_timeout: float | None = None,
    max_workers: int | None = None,
) -> FastAPI:
    """A FastAPI application serving `retriever` to callers that `authenticate` identifies.

    `authenticate` receives the bearer token and returns the `Subject`, or raises `InvalidToken`;
    `OIDCAuthenticator` is one. An async authenticator is awaited, and a synchronous one runs in a
    worker thread. `metadata_fields` lists the metadata returned with each hit (none by default).
    A request body over `max_body_bytes` is refused with 413. `docs` serves the interactive API
    pages at `/docs`.

    `retrieval_timeout` (seconds; None, the default, sets no bound) bounds each retrieval: one that
    takes longer is cancelled, answers 503 and returns nothing. A synchronous call it was making in
    a worker thread cannot be stopped and runs on to its own client's timeout, holding that thread.

    `max_workers` (None, the default, leaves the event loop's default executor alone) replaces the
    event loop's default executor, while the application's lifespan runs, with a thread pool of that
    many threads. `aretrieve` runs every synchronous store, authorizer, reranker and redactor call,
    and its observers, in that executor, as `CachingAuthorizer` runs its Redis calls, so its size
    bounds how many of them run at once. A synchronous authenticator runs in Starlette's own thread
    pool instead.
    """
    if min(max_k, max_query_length, max_vector_length, max_body_bytes) < 1:
        raise ValueError("max_k, max_query_length, max_vector_length and max_body_bytes must be positive")
    retrieval_timeout = checked(retrieval_timeout)
    workers: Any = max_workers
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise ValueError("max_workers must be None or a positive integer")
    fields = frozenset(metadata_fields)
    bearer = HTTPBearer(auto_error=False)

    @contextlib.asynccontextmanager
    async def sized_executor(_app: FastAPI) -> AsyncIterator[None]:
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wardowl")
        asyncio.get_running_loop().set_default_executor(pool)
        try:
            yield
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title=title,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        # Only when asked for: an application given a lifespan no longer runs on_event handlers.
        lifespan=sized_executor if max_workers is not None else None,
    )
    app.add_middleware(_BodyLimit, limit=max_body_bytes)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # The rejected input is never echoed back: it may not be JSON at all (NaN), and it may be sensitive.
        errors = [{key: error[key] for key in ("type", "loc", "msg") if key in error} for error in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    # Anything else runs in a worker thread; a callable that still returns an awaitable is awaited.
    authenticate_is_async = inspect.iscoroutinefunction(authenticate)

    class RetrieveRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")  # a body naming a user, group or tenant is refused

        query: str | None = Field(default=None, min_length=1, max_length=max_query_length)
        vector: list[FiniteFloat] | None = Field(default=None, min_length=1, max_length=max_vector_length)
        k: int = Field(default=5, ge=1, le=max_k)

    class Hit(BaseModel):
        id: str
        text: str
        score: float
        metadata: dict[str, Any]

    class RetrieveResponse(BaseModel):
        hits: list[Hit]
        complete: bool

    async def caller(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Subject:
        """The verified caller, from the token alone."""
        refused = HTTPException(401, "a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})
        if credentials is None:
            raise refused
        try:
            if authenticate_is_async:
                answer: object = authenticate(credentials.credentials)
            else:  # it may block, on a key endpoint or a database: keep it off the event loop
                answer = await run_in_threadpool(authenticate, credentials.credentials)
            if isinstance(answer, Awaitable):
                answer = await answer
        except InvalidToken:
            raise refused from None
        except AuthorizationError:
            _log.exception("wardowl could not verify a token; the request was refused")
            raise HTTPException(503, "the caller could not be verified, so nothing was returned") from None
        if not isinstance(answer, Subject):
            _log.error("wardowl's authenticator returned %s, not a Subject", type(answer).__name__)
            raise HTTPException(503, "the caller could not be verified, so nothing was returned")
        return answer

    @app.post("/v1/retrieve")
    async def retrieve(body: RetrieveRequest, subject: Annotated[Subject, Depends(caller)]) -> RetrieveResponse:
        if body.query is None and body.vector is None:
            raise HTTPException(422, "a request needs a query, a vector, or both")
        query = Query(body.query, body.vector) if body.vector is not None else body.query or ""
        try:
            result = await within(retriever.aretrieve(query, subject, body.k), retrieval_timeout)
        except TimedOut:
            _log.error(
                "wardowl's retrieval took longer than %s s; it was cancelled and nothing was returned",
                retrieval_timeout,
            )
            raise HTTPException(503, "the retrieval did not finish in time, so nothing was returned") from None
        except AuthorizationError:
            _log.exception("wardowl's permission check failed; nothing was returned")
            raise HTTPException(503, "the permission check could not be completed, so nothing was returned") from None
        except InvalidQuery:
            # The request's own fault: k beyond the retriever's limit, a vector of the wrong length, or
            # text or a vector the backend cannot search with. Any other ValueError is the server's,
            # and answers 500. The message echoes nothing from the request or the backend.
            raise HTTPException(
                422, "this query cannot be searched here; check k, the vector's length, or send text"
            ) from None
        except SearchError:
            # The store's search or count failed: unreachable, misconfigured, or refusing the query
            # (a database refuses a vector of the wrong length). A server fault, so 500, answered
            # here: left to escape, it reached uvicorn, which logged it and closed the connection,
            # and the client's next request on that connection was reset. The store's error is the
            # cause, logged with it, and never sent.
            _log.exception("wardowl's search failed; nothing was returned")
            raise HTTPException(500, "the search could not be completed, so nothing was returned") from None
        hits = [
            Hit(
                id=h.id,
                text=h.text,
                score=h.score,
                metadata={name: value for name, value in h.metadata.items() if name in fields},
            )
            for h in result.hits
        ]
        return RetrieveResponse(hits=hits, complete=result.complete)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app

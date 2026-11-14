"""The HTTP service: identity from the token alone, failing closed, and OIDC token verification."""

import asyncio
import base64
import datetime
import logging
import math
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")

from fastapi.testclient import TestClient  # noqa: E402
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError  # noqa: E402

from wardowl import (  # noqa: E402
    AuthorizationError,
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    InvalidQuery,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    SearchError,
    Subject,
)
from wardowl._timeout import TimedOut, within  # noqa: E402
from wardowl.server import InvalidToken, OIDCAuthenticator, _BodyLimit, create_app  # noqa: E402

DOCS = [
    Document("eng-plan", "the engineering plan for the launch", {"allowed_groups": ["eng"], "source": "wiki"}),
    Document("hr-pay", "the pay bands for the launch team", {"allowed_groups": ["hr"], "source": "hr"}),
    Document("all-hands", "the launch date for everyone", {"allowed_groups": ["*"], "source": "mail"}),
]
USERS = {"ann-token": Subject("ann", ["eng"]), "bob-token": Subject("bob", ["hr"])}


def tokens(token):
    if token not in USERS:
        raise InvalidToken("unknown token")
    return USERS[token]


def service(authenticate=tokens, authorizer=None, **kwargs):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), authorizer or MetadataACLAuthorizer())
    return create_app(retriever, authenticate, **kwargs)


def client(authenticate=tokens, authorizer=None, **kwargs):
    return TestClient(service(authenticate, authorizer, **kwargs))


def ask(c, body, who="ann-token"):
    headers = {"Authorization": f"Bearer {who}"} if who else {}
    return c.post("/v1/retrieve", json=body, headers=headers)


def test_a_caller_gets_only_what_their_token_permits():
    c = client()
    ann = ask(c, {"query": "launch", "k": 5}).json()
    bob = ask(c, {"query": "launch", "k": 5}, who="bob-token").json()
    assert {h["id"] for h in ann["hits"]} == {"eng-plan", "all-hands"} and ann["complete"] is True
    assert {h["id"] for h in bob["hits"]} == {"hr-pay", "all-hands"}


def test_hits_carry_only_the_metadata_fields_named():
    assert all(h["metadata"] == {} for h in ask(client(), {"query": "launch"}).json()["hits"])
    hits = ask(client(metadata_fields=["source"]), {"query": "launch"}).json()["hits"]
    assert {h["id"]: h["metadata"] for h in hits} == {"eng-plan": {"source": "wiki"}, "all-hands": {"source": "mail"}}


@pytest.mark.parametrize("token", [None, "forged"])
def test_a_missing_or_invalid_token_is_refused(token):
    response = ask(client(), {"query": "launch"}, who=token)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "body",
    [
        {"query": "launch", "user": "bob"},  # the body cannot name a subject
        {"query": "launch", "groups": ["hr"]},
        {"query": "launch", "k": 51},
        {"query": "x" * 4001},
        {"query": ""},
        {"k": 3},
        {"vector": []},
        {"vector": [0.1] * 4097},
        {"vector": [0.1, "x"]},
    ],
)
def test_bad_requests_are_refused(body):
    assert ask(client(), body).status_code == 422


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_vector_must_hold_finite_numbers(number):
    body = f'{{"query": "launch", "vector": [0.1, {number}]}}'.encode()
    headers = {"Authorization": "Bearer ann-token", "Content-Type": "application/json"}
    assert client().post("/v1/retrieve", content=body, headers=headers).status_code == 422


def test_a_query_with_a_vector_is_accepted():
    hits = ask(client(), {"query": "launch", "vector": [0.1, 0.2], "k": 2}).json()["hits"]
    assert {h["id"] for h in hits} <= {"eng-plan", "all-hands"}
    assert ask(client(), {"query": "launch", "vector": [0.1] * 4096}).status_code == 200


def test_a_body_over_the_limit_is_refused_before_it_is_parsed_or_authenticated():
    c = client(max_body_bytes=1024)
    assert ask(c, {"query": "launch " * 50}).status_code == 200
    assert ask(c, {"query": "launch " * 200}).status_code == 413
    assert ask(c, {"query": "launch " * 200}, who=None).status_code == 413  # before authentication
    # Streamed without a Content-Length, the bytes received are counted instead.
    chunked = iter([b'{"query": "', b"x" * 2000, b'"}'])
    headers = {"Authorization": "Bearer ann-token", "Content-Type": "application/json"}
    assert c.post("/v1/retrieve", content=chunked, headers=headers).status_code == 413


def test_the_body_limit_counts_streamed_chunks_and_never_calls_the_app():
    called, sent = [], []

    async def app(scope, receive, send):
        called.append(scope)

    messages = [{"type": "http.request", "body": b"x" * 600, "more_body": True} for _ in range(3)]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/retrieve", "headers": []}
    asyncio.run(_BodyLimit(app, 1024)(scope, receive, send))
    assert called == [] and sent[0]["status"] == 413 and len(messages) == 1


def test_the_body_reaches_the_app_unchanged_under_the_limit():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())  # then whatever the server sends next

    messages = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    async def send(message):
        raise AssertionError("the app sends nothing here")

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"4")]}
    asyncio.run(_BodyLimit(app, 1024)(scope, receive, send))
    assert seen == [{"type": "http.request", "body": b"abcd", "more_body": False}, {"type": "http.disconnect"}]


def test_the_body_limit_passes_other_scopes_on_and_stops_when_the_client_leaves():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    async def gone():
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError("nothing is sent to a client that left")

    asyncio.run(_BodyLimit(app, 10)({"type": "lifespan"}, gone, send))
    asyncio.run(_BodyLimit(app, 10)({"type": "http", "method": "POST", "path": "/", "headers": []}, gone, send))
    assert called == ["lifespan"]


class Failing:
    def check(self, subject, hits, action="read"):
        raise AuthorizationError("the policy service at http://opa.internal:8181 timed out")

    def filter_for(self, subject, action="read"):
        return None


def test_a_failing_permission_check_returns_nothing_and_no_details():
    response = ask(client(authorizer=Failing()), {"query": "launch"})
    assert response.status_code == 503 and "opa.internal" not in response.text


class Dimensioned:
    """A vector store that knows its vectors' length, as FaissBackend does."""

    supports_filters = False

    def search(self, query, k, filter=None):
        q = Query.of(query)
        if q.vector is not None and len(q.vector) != 2:
            raise InvalidQuery(f"the query vector has {len(q.vector)} dimensions, not 2")
        return [Hit("all-hands", "the launch date for everyone", 1.0, {"allowed_groups": ["*"]})]


def test_a_query_the_service_cannot_search_is_a_422_that_echoes_nothing():
    words_only = ask(client(), {"vector": [0.1, 0.2]})  # InMemoryBackend matches words: it needs text
    assert words_only.status_code == 422 and "only a vector" not in words_only.text
    sized = TestClient(create_app(PermissionedRetriever(Dimensioned(), MetadataACLAuthorizer()), tokens))
    assert ask(sized, {"query": "launch", "vector": [0.1, 0.2]}).status_code == 200
    wrong = ask(sized, {"query": "launch", "vector": [0.1, 0.2, 0.3]})
    assert wrong.status_code == 422 and "dimensions" not in wrong.text
    capped = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), max_candidates=3)
    assert ask(TestClient(create_app(capped, tokens)), {"query": "launch", "k": 4}).status_code == 422
    assert ask(TestClient(create_app(capped, tokens)), {"query": "launch", "k": 3}).status_code == 200


class Misconfigured:
    """A store whose own error, a ValueError naming its host, is no fault of the request."""

    supports_filters = True

    def search(self, query, k, filter=None):
        raise ValueError("field 'team.name' cannot be filtered on db.internal")


@pytest.mark.parametrize(
    "retriever",
    [
        PermissionedRetriever(Misconfigured(), MetadataACLAuthorizer()),
        # Pre-filtering with an authorizer that gives no filter: the configuration is wrong.
        PermissionedRetriever(
            InMemoryBackend(DOCS), FunctionAuthorizer(lambda s, hits, a: [True] * len(hits)), strategy="prefilter"
        ),
    ],
    ids=["store", "strategy"],
)
def test_a_server_fault_is_a_500_not_a_422(retriever):
    response = ask(TestClient(create_app(retriever, tokens), raise_server_exceptions=False), {"query": "launch"})
    assert response.status_code == 500 and "db.internal" not in response.text and "prefilter" not in response.text


DRIVER_ERROR = "could not connect to postgresql://admin:hunter2@db.internal:5432/rag"


class Unreachable:
    """A store whose driver error names its host and credentials, as real drivers' do."""

    supports_filters = True

    def search(self, query, k, filter=None):
        raise ConnectionError(DRIVER_ERROR)


class CountFails(InMemoryBackend):
    """A store that searches, then fails to count a short result."""

    exact_search = False  # so a short result is counted, under short_results="count"

    def count(self, filter, limit):
        raise ConnectionError(DRIVER_ERROR)


@pytest.mark.parametrize(
    ("retriever", "failed"),
    [
        (PermissionedRetriever(Unreachable(), MetadataACLAuthorizer()), "search failed"),
        (PermissionedRetriever(CountFails(DOCS), MetadataACLAuthorizer(), short_results="count"), "count failed"),
    ],
    ids=["search", "count"],
)
def test_a_failed_search_answers_500_itself_echoes_nothing_and_logs_the_cause(caplog, retriever, failed):
    # The test client raises whatever leaves the application, so getting a response proves that the
    # handler answered. Left to escape, the error reached uvicorn, which closed the connection.
    c = TestClient(create_app(retriever, tokens))
    with caplog.at_level(logging.ERROR, logger="wardowl"):
        response = ask(c, {"query": "launch"})
    assert response.status_code == 500
    assert response.json() == {"detail": "the search could not be completed, so nothing was returned"}
    assert not any(s in response.text for s in ("hunter2", "db.internal", failed))
    (record,) = [r for r in caplog.records if r.name == "wardowl" and r.levelno >= logging.ERROR]
    assert record.getMessage() == "wardowl's search failed; nothing was returned"
    assert record.exc_info is not None
    error = record.exc_info[1]
    assert isinstance(error, SearchError) and str(error) == failed and str(error.__cause__) == DRIVER_ERROR
    # The same client goes on to its next request.
    assert c.get("/healthz").json() == {"status": "ok"}


def test_an_authenticator_may_be_async():
    async def later(token):
        return tokens(token)

    assert ask(client(authenticate=later), {"query": "launch"}).status_code == 200
    # A plain function that returns an awaitable is awaited too.
    assert ask(client(authenticate=lambda token: later(token)), {"query": "launch"}).status_code == 200


def test_a_synchronous_authenticator_runs_in_a_worker_thread():
    where = []

    def blocking(token):
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("worker thread")
        return tokens(token)

    assert ask(client(authenticate=blocking), {"query": "launch"}).status_code == 200
    assert where == ["worker thread"]


def test_a_slow_authenticator_does_not_stall_other_requests():
    entered, release = threading.Event(), threading.Event()

    def slow(token):  # an identity provider that takes its time
        entered.set()
        release.wait(10)
        return tokens(token)

    async def run():
        transport = httpx.ASGITransport(app=service(authenticate=slow))
        async with httpx.AsyncClient(transport=transport, base_url="http://wardowl.test") as c:
            headers = {"Authorization": "Bearer ann-token"}
            pending = asyncio.ensure_future(c.post("/v1/retrieve", json={"query": "launch"}, headers=headers))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            health = await asyncio.wait_for(c.get("/healthz"), 5)
            stalled = not pending.done()
            release.set()
            return health, stalled, await pending

    try:
        health, stalled, answer = asyncio.run(run())
    finally:
        release.set()
    assert health.status_code == 200 and stalled and answer.status_code == 200


@pytest.mark.parametrize("answer", [AuthorizationError("keys unreachable"), "ann"])
def test_an_authenticator_that_fails_or_answers_wrongly_refuses_with_503(answer):
    def broken(token):
        if isinstance(answer, Exception):
            raise answer
        return answer

    response = ask(client(authenticate=broken), {"query": "launch"})
    assert response.status_code == 503 and "unreachable" not in response.text


def test_health_and_docs():
    assert client().get("/healthz").json() == {"status": "ok"}
    assert client().get("/docs").status_code == 404
    assert client(docs=True).get("/docs").status_code == 200
    assert "/v1/retrieve" in client().get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("limit", ["max_k", "max_query_length", "max_vector_length", "max_body_bytes"])
def test_limits_must_be_positive(limit):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(ValueError):
        create_app(retriever, tokens, **{limit: 0})


@pytest.mark.parametrize(
    "setting",
    [
        {"retrieval_timeout": 0},
        {"retrieval_timeout": -1},
        {"retrieval_timeout": float("nan")},
        {"retrieval_timeout": float("inf")},
        {"retrieval_timeout": True},
        {"retrieval_timeout": "5"},
        {"max_workers": 0},
        {"max_workers": True},
        {"max_workers": 2.5},
    ],
)
def test_time_and_thread_bounds_must_be_valid(setting):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(ValueError):
        create_app(retriever, tokens, **setting)


# --- time bounds, the executor and observers ---------------------------------------------------


class Held:
    """A store whose search waits until released, like a database behind a proxy that stopped answering."""

    supports_filters = False

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def search(self, query, k, filter=None):
        self.entered.set()
        self.release.wait(10)
        return [Hit("all-hands", "the launch date for everyone", 1.0, {"allowed_groups": ["*"]})]


def test_a_retrieval_that_runs_out_of_time_answers_503_returns_nothing_and_is_reported(caplog):
    store, events = Held(), []
    retriever = PermissionedRetriever(store, MetadataACLAuthorizer(), observers=[events.append])
    app = create_app(retriever, tokens, retrieval_timeout=0.2)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wardowl.test") as c:
            headers = {"Authorization": "Bearer ann-token"}
            started = time.monotonic()
            answer = await asyncio.wait_for(c.post("/v1/retrieve", json={"query": "launch"}, headers=headers), 5)
            took = time.monotonic() - started
            store.release.set()
            for _ in range(500):  # the cancelled retrieval reports its event as it unwinds
                if events:
                    break
                await asyncio.sleep(0.01)
            return answer, took

    try:
        answer, took = asyncio.run(run())
    finally:
        store.release.set()
    # The store was still searching when the bound ran out: nothing it found reaches the caller.
    assert store.entered.is_set() and took < 5
    assert answer.status_code == 503
    assert answer.json() == {"detail": "the retrieval did not finish in time, so nothing was returned"}
    assert "took longer than 0.2 s" in caplog.text
    assert [e.error for e in events] == ["CancelledError"] and events[0].returned_ids == ()


class Sleeping(InMemoryBackend):
    """A store with a native async search that never answers in time."""

    async def asearch(self, query, k, filter=None):
        await asyncio.sleep(30)
        return []


def test_the_time_bound_cancels_a_native_async_search_and_spares_quick_ones():
    slow = PermissionedRetriever(Sleeping(DOCS), MetadataACLAuthorizer())
    started = time.monotonic()
    assert ask(TestClient(create_app(slow, tokens, retrieval_timeout=0.1)), {"query": "launch"}).status_code == 503
    assert time.monotonic() - started < 10
    quick = ask(client(retrieval_timeout=5), {"query": "launch"})
    assert quick.status_code == 200 and {h["id"] for h in quick.json()["hits"]} == {"eng-plan", "all-hands"}


def test_max_workers_sizes_the_thread_pool_the_retrieval_runs_in():
    names = []

    class Recorded(InMemoryBackend):
        def search(self, query, k, filter=None):
            names.append(threading.current_thread().name)
            return super().search(query, k, filter)

    events = []
    retriever = PermissionedRetriever(Recorded(DOCS), MetadataACLAuthorizer(), observers=[events.append])
    with TestClient(create_app(retriever, tokens, max_workers=1)) as c:  # the lifespan runs
        for _ in range(3):
            assert ask(c, {"query": "launch"}).status_code == 200
    # One thread, the pool's, ran every search, and the observers too.
    assert len(names) == 3 and set(names) == {"wardowl_0"} and len(events) == 3


def test_a_slow_observer_does_not_stall_other_requests():
    entered, release = threading.Event(), threading.Event()

    def slow(event):  # an audit sink whose collector stopped reading
        entered.set()
        release.wait(10)

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[slow])

    async def run():
        transport = httpx.ASGITransport(app=create_app(retriever, tokens))
        async with httpx.AsyncClient(transport=transport, base_url="http://wardowl.test") as c:
            headers = {"Authorization": "Bearer ann-token"}
            pending = asyncio.ensure_future(c.post("/v1/retrieve", json={"query": "launch"}, headers=headers))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            health = await asyncio.wait_for(c.get("/healthz"), 5)
            stalled = not pending.done()
            release.set()
            return health, stalled, await pending

    try:
        health, stalled, answer = asyncio.run(run())
    finally:
        release.set()
    # The request waits for its own observer; the service goes on answering others.
    assert entered.is_set() and health.status_code == 200 and stalled and answer.status_code == 200


def test_the_time_bound_runs_the_retrieval_as_its_own_task_and_cancels_it_with_the_caller():
    async def answer(value, delay=0.0):
        await asyncio.sleep(delay)
        return value

    async def stubborn():  # a client that turns its cancellation into an error of its own
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise RuntimeError("cancelled") from None

    async def run():
        assert await within(answer("now"), None) == "now"
        assert await within(answer("soon", 0.01), 5) == "soon"
        with pytest.raises(TimedOut):
            await within(stubborn(), 0.05)
        inner = []

        async def watched():
            inner.append(asyncio.current_task())
            await asyncio.sleep(30)

        outer = asyncio.ensure_future(within(watched(), 30))
        while not inner:
            await asyncio.sleep(0.01)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0.01)
        return inner[0]

    assert asyncio.run(run()).cancelled()


# --- OIDCAuthenticator -------------------------------------------------------------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NEW_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
SECRET = b"s" * 32
ISSUER, AUDIENCE = "https://login.example.com/", "knowledge-api"


def jwk(key, kid):
    return {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "use": "sig"}


SHARED = {"kty": "oct", "kid": "shared", "k": base64.urlsafe_b64encode(SECRET).rstrip(b"=").decode()}


class KeyEndpoint:
    """A stand-in for the provider's JWKS endpoint: what it publishes now, and how often it is asked."""

    def __init__(self, *published):
        self.published = list(published)
        self.fetches = 0
        self.error = None

    def get_signing_keys(self, refresh=False):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return [jwt.PyJWK(data) for data in self.published]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def token(kid="k1", key=KEY, algorithm="RS256", **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "ann", "groups": ["eng"], "tid": "acme", "exp": now + 60}
    claims.update(overrides)
    headers = {"kid": kid} if kid else {}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=algorithm, headers=headers)


def oidc(*published, **kwargs):
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.example.com/jwks.json", **kwargs)
    authenticate._client = KeyEndpoint(*(published or [jwk(KEY, "k1")]))
    authenticate._clock = Clock()
    return authenticate


def test_a_valid_token_gives_its_subject():
    assert oidc(tenant_claim="tid")(token()) == Subject("ann", ["eng"], {"tenant": "acme"})
    assert oidc()(token(groups=None)) == Subject("ann")
    unnamed = {name: value for name, value in jwk(NEW_KEY, "-").items() if name != "kid"}  # never used
    assert oidc(unnamed, jwk(KEY, "k1"))(token()) == Subject("ann", ["eng"])


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "another-api"},
        {"iss": "https://evil.example.com/"},
        {"sub": None},
        {"sub": 7},
        {"groups": "eng"},
        {"tid": None},
    ],
)
def test_a_token_failing_any_check_is_invalid(claims):
    with pytest.raises(InvalidToken):
        oidc(tenant_claim="tid")(token(**claims))


@pytest.mark.parametrize("claim", ["iat", "nbf"])
def test_a_token_from_a_clock_ahead_is_refused_without_leeway_and_accepted_within_it(claim):
    # An identity provider whose clock runs ahead of the service's issues tokens from its future.
    soon = int(time.time()) + 10
    with pytest.raises(InvalidToken):
        oidc()(token(**{claim: soon}))
    with pytest.raises(InvalidToken):
        oidc(leeway=2)(token(**{claim: soon}))
    assert oidc(leeway=60)(token(**{claim: soon})) == Subject("ann", ["eng"])
    # Times already passed need no leeway.
    assert oidc()(token(**{claim: int(time.time()) - 5})) == Subject("ann", ["eng"])


def test_the_leeway_applies_to_the_expiry_too():
    expired = token(exp=int(time.time()) - 30)
    with pytest.raises(InvalidToken):
        oidc()(expired)
    with pytest.raises(InvalidToken):
        oidc(leeway=10)(expired)
    assert oidc(leeway=60)(expired) == Subject("ann", ["eng"])


@pytest.mark.parametrize("leeway", [math.nan, math.inf, -math.inf])
def test_a_leeway_that_is_not_finite_is_refused(leeway):
    # NaN would make PyJWT's expiry comparison false, so an expired token would verify.
    with pytest.raises(ValueError, match="leeway"):
        oidc(leeway=leeway)


def test_the_leeway_may_be_a_timedelta_as_pyjwt_allows():
    expired = token(exp=int(time.time()) - 30)
    assert oidc(leeway=datetime.timedelta(minutes=1))(expired) == Subject("ann", ["eng"])
    with pytest.raises(InvalidToken):
        oidc(leeway=datetime.timedelta(seconds=10))(expired)


def wardowl_records(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "wardowl"]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (lambda: token(iat=int(time.time()) + 30), "ImmatureSignatureError"),
        (lambda: token(nbf=int(time.time()) + 30), "ImmatureSignatureError"),
        (lambda: token(exp=int(time.time()) - 30), "ExpiredSignatureError"),
        (lambda: token(aud="another-api"), "InvalidAudienceError"),
        (lambda: token(iss="https://evil.example.com/"), "InvalidIssuerError"),
        (lambda: token(tid=None), "MissingRequiredClaimError (tid)"),
        (lambda: token(key=NEW_KEY), "InvalidSignatureError"),
        (lambda: "not-a-jwt", "DecodeError"),
        (lambda: token(kid="made-up"), "no key published by the identity provider matches the token's kid"),
        (lambda: token(groups="eng"), "the token's groups claim is not a list of strings"),
    ],
)
def test_a_refused_token_logs_the_check_it_failed_and_nothing_from_the_token(caplog, bad, reason):
    refused = bad()
    with caplog.at_level(logging.INFO, logger="wardowl"), pytest.raises(InvalidToken):
        oidc(tenant_claim="tid")(refused)
    assert wardowl_records(caplog) == [(logging.INFO, f"wardowl refused a token: {reason}")]
    for fragment in (refused, refused.split(".")[-1], "another-api", "evil.example.com"):
        assert fragment not in caplog.text


def test_the_service_tells_the_caller_nothing_about_why_a_token_was_refused(caplog):
    c = client(authenticate=oidc(require_groups=True))
    with caplog.at_level(logging.INFO, logger="wardowl"):
        early = ask(c, {"query": "launch"}, who=token(iat=int(time.time()) + 30))
        late = ask(c, {"query": "launch"}, who=token(exp=int(time.time()) - 30))
        groupless = ask(c, {"query": "launch"}, who=token(groups=None))
    for answer in (early, late, groupless):
        assert answer.status_code == 401 and answer.json() == {"detail": "a valid bearer token is required"}
        assert answer.headers["www-authenticate"] == "Bearer"
    assert [message for _, message in wardowl_records(caplog)] == [
        "wardowl refused a token: ImmatureSignatureError",
        "wardowl refused a token: ExpiredSignatureError",
        "wardowl refused a token: MissingRequiredClaimError (groups)",
    ]


def test_require_groups_refuses_a_token_without_the_claim(caplog):
    authenticate = oidc(require_groups=True)
    assert authenticate(token()) == Subject("ann", ["eng"])
    assert authenticate(token(groups=[])) == Subject("ann")  # an empty list is a claim: no groups
    with caplog.at_level(logging.DEBUG, logger="wardowl"), pytest.raises(InvalidToken):
        authenticate(token(groups=None))
    assert wardowl_records(caplog) == [(logging.INFO, "wardowl refused a token: MissingRequiredClaimError (groups)")]
    # A renamed claim is the one required.
    renamed = oidc(require_groups=True, groups_claim="roles")
    assert renamed(token(groups=None, roles=["eng"])) == Subject("ann", ["eng"])
    with pytest.raises(InvalidToken):
        renamed(token())  # it has groups, not roles
    # `subject`, which a subclass may call with claims of its own, holds to it too.
    with pytest.raises(InvalidToken):
        authenticate.subject({"sub": "ann"})
    assert authenticate.subject({"sub": "ann", "groups": []}) == Subject("ann")


def test_without_require_groups_a_token_without_the_claim_has_no_groups_and_says_so_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="wardowl"):
        assert oidc()(token(groups=None)) == Subject("ann")
    assert wardowl_records(caplog) == [
        (logging.DEBUG, "wardowl found no groups claim in a token; its subject has no groups")
    ]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="wardowl"):
        assert oidc()(token(groups=[])) == Subject("ann")  # present, and empty: nothing to report
        assert oidc()(token()) == Subject("ann", ["eng"])
    assert wardowl_records(caplog) == []
    with pytest.raises(InvalidToken):  # null is not a list of groups, as before
        oidc().subject({"sub": "ann", "groups": None})


def test_a_token_signed_with_a_shared_secret_or_garbage_is_invalid():
    for bad in (
        token(kid=None, key="secret", algorithm="HS256"),
        token(key=SECRET, algorithm="HS256"),  # names the RSA key, signed with a shared secret
        token(kid=None),  # names no key
        "not-a-jwt",
    ):
        with pytest.raises(InvalidToken):
            oidc()(bad)


def test_a_token_must_use_its_keys_own_algorithm():
    # An EC signature over the RSA key's kid is invalid, not a server error.
    with pytest.raises(InvalidToken):
        oidc()(token(key=EC_KEY, algorithm="ES256"))
    # A shared-secret key in the published set is never used, even when a token names it.
    with pytest.raises(InvalidToken):
        oidc(jwk(KEY, "k1"), SHARED)(token(kid="shared", key=SECRET, algorithm="HS256"))
    # An algorithm the service does not allow is refused even for a published key.
    with pytest.raises(InvalidToken):
        oidc(algorithms=["ES256"])(token())


def test_unknown_key_ids_fetch_the_keys_at_most_once_per_cooldown():
    authenticate = oidc()
    endpoint, clock = authenticate._client, authenticate._clock
    assert authenticate(token()) == Subject("ann", ["eng"])
    for i in range(50):  # made-up key ids cost nothing once the keys were fetched
        with pytest.raises(InvalidToken):
            authenticate(token(kid=f"made-up-{i}"))
    assert endpoint.fetches == 1
    clock.now += 31
    with pytest.raises(InvalidToken):
        authenticate(token(kid="made-up-again"))
    with pytest.raises(InvalidToken):
        authenticate(token(kid="and-again"))
    assert endpoint.fetches == 2
    # A key the provider adds is found on the next fetch the cooldown allows.
    endpoint.published.append(jwk(NEW_KEY, "k2"))
    clock.now += 31
    assert authenticate(token(kid="k2", key=NEW_KEY)) == Subject("ann", ["eng"])
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 3


def test_a_key_rotated_out_stops_verifying_once_the_set_expires():
    authenticate = oidc(lifespan=300)
    endpoint, clock = authenticate._client, authenticate._clock
    assert authenticate(token()) == Subject("ann", ["eng"])
    endpoint.published = [jwk(NEW_KEY, "k2")]
    clock.now += 299
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 1
    clock.now += 2
    with pytest.raises(InvalidToken):
        authenticate(token())
    assert endpoint.fetches == 2
    assert authenticate(token(kid="k2", key=NEW_KEY)) == Subject("ann", ["eng"])


@pytest.mark.parametrize(
    "error",
    [
        PyJWKClientConnectionError("timed out"),
        PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
        PyJWKSetError("The JWK Set did not contain any usable keys"),
        ValueError("Expecting value: line 1 column 1"),
        OSError("connection reset"),
    ],
)
def test_a_key_endpoint_that_fails_is_a_503_and_is_not_hammered(error):
    authenticate = oidc()
    endpoint, clock = authenticate._client, authenticate._clock
    endpoint.error = error
    with pytest.raises(AuthorizationError):
        authenticate(token())
    with pytest.raises(AuthorizationError):  # no keys yet, and the next fetch is not due
        authenticate(token())
    assert endpoint.fetches == 1
    endpoint.error = None
    clock.now += 31
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 2


def test_the_key_endpoint_is_asked_with_a_short_timeout_and_no_unbounded_cache():
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.example.com/jwks.json")
    assert authenticate._client.timeout == 5 and authenticate._client.jwk_set_cache is None
    assert OIDCAuthenticator(ISSUER, AUDIENCE, "https://x.example/jwks", timeout=2)._client.timeout == 2


def test_the_keys_are_fetched_with_the_ssl_context_given(monkeypatch):
    # An identity provider behind a private CA: its trust, not the process-wide SSL_CERT_FILE.
    tls, used = ssl.create_default_context(), []

    class Opener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("login.internal: certificate verify failed")

    def build_opener(*handlers):
        used.extend(h._context for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.internal/jwks.json", ssl_context=tls)
    with pytest.raises(AuthorizationError) as failure:
        authenticate(token())
    assert used == [tls]
    assert "login.internal" not in str(failure.value) and "login.internal" in str(failure.value.__cause__)
    default = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.internal/jwks.json")
    with pytest.raises(AuthorizationError):
        default(token())
    assert used == [tls]  # no context given: urllib's default HTTPS handler and the system's trust store


@pytest.mark.parametrize(
    "kwargs",
    [
        {"jwks_url": "http://login.example.com/jwks.json"},
        {"algorithms": ["HS256"]},
        {"algorithms": []},
        {"timeout": 0},
        {"cooldown": 0},
        {"lifespan": 10, "cooldown": 30},
    ],
)
def test_unsafe_configuration_is_refused(kwargs):
    settings = {"jwks_url": "https://login.example.com/jwks.json", **kwargs}
    with pytest.raises(ValueError):
        OIDCAuthenticator(ISSUER, AUDIENCE, settings.pop("jwks_url"), **settings)


def test_the_service_with_oidc_tokens():
    c = client(authenticate=oidc())
    assert {h["id"] for h in ask(c, {"query": "launch"}, who=token()).json()["hits"]} == {"eng-plan", "all-hands"}
    assert ask(c, {"query": "launch"}, who=token(aud="another-api")).status_code == 401
    assert ask(c, {"query": "launch"}, who=token(key=EC_KEY, algorithm="ES256")).status_code == 401

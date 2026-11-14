# HTTP service

`wardowl.server` serves a `PermissionedRetriever` over HTTP, so applications in any language
(TypeScript, Java, .NET, Go) get the same permission-aware retrieval. You build the retriever in
Python, with any backend and authorizer, and run the service next to your stores.

```bash
pip install "wardowl[server]" uvicorn
```

```python
from wardowl import PermissionedRetriever
from wardowl.server import OIDCAuthenticator, create_app

retriever = PermissionedRetriever(backend, authorizer)
authenticate = OIDCAuthenticator(
    issuer="https://login.example.com/",
    audience="knowledge-api",
    jwks_url="https://login.example.com/.well-known/jwks.json",
    tenant_claim="tid",  # optional: the subject's tenant, for TenantAuthorizer
)
app = create_app(retriever, authenticate, metadata_fields=["source", "title"])
```

```bash
uvicorn service:app --host 0.0.0.0 --port 8000 --workers 4
```

## The API

`POST /v1/retrieve`, with the caller's access token:

```http
POST /v1/retrieve HTTP/1.1
Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
Content-Type: application/json

{"query": "launch plan", "k": 5}
```

```json
{
  "hits": [
    {"id": "plan-3", "text": "...", "score": 0.82, "metadata": {"source": "wiki", "title": "Launch"}}
  ],
  "complete": true
}
```

- `query` is the text; `vector` (a list of numbers) searches with an embedding you computed; give
  either or both. `k` is between 1 and `max_k` (50 by default).
- The query holds at most `max_query_length` characters (4,000 by default) and the vector at most
  `max_vector_length` numbers (4,096 by default), all finite: `NaN` and infinities are refused.
- A body over `max_body_bytes` (256 KiB by default) is refused with 413 before it is parsed, by its
  `Content-Length` and by the bytes actually received. If you raise `max_vector_length` well past
  4,096, raise `max_body_bytes` with it.
- `GET /healthz` answers liveness probes. `GET /openapi.json` describes the API, for generating a
  client in your language; `create_app(..., docs=True)` also serves the interactive pages at `/docs`.

| Status | When |
|---|---|
| 200 | The hits the caller may read, best first, and whether the result is `complete` |
| 401 | No bearer token, or one that fails verification. The answer is the same whatever the reason; `OIDCAuthenticator` logs the check that failed ([Why a token was refused](#why-a-token-was-refused)) |
| 413 | A body over `max_body_bytes` |
| 422 | A malformed body, including one with a field the API does not define, or a value over its limit. Also a well-formed query the service cannot search as sent (`InvalidQuery`): `k` over the retriever's `max_candidates`, only a vector for a keyword backend, only text for a backend without an embed function, or a vector of the wrong length where the backend checks it (`FaissBackend`). The answer echoes nothing from the request or the backend |
| 500 | A fault on the server's side, such as a misconfigured backend or reranker. Nothing is returned, and the error goes to the server's log |
| 503 | The permission check, or the token's verification, could not be completed, or the retrieval took longer than `retrieval_timeout`: nothing is returned, and the reason goes to the `wardowl` logger, not to the caller |

A vector of the wrong length for a store that does not check it before searching (pgvector, Qdrant,
Chroma and the other database clients report it with their own errors) answers 500: only the backend
can tell that the request was at fault, and it says so by raising `wardowl.InvalidQuery`.

## Who the caller is

The subject comes from the verified token alone. The body cannot name a user, a group or a tenant:
any field the API does not define is refused, which is the same rule the
[MCP tool](mcp.md#whose-permissions-apply) follows.

`OIDCAuthenticator` verifies JWT access tokens against your identity provider's published keys:
the signature (RS256 or ES256 by default; shared-secret algorithms are refused), the issuer, the
audience, and the token's times: `exp`, and `nbf` and `iat` when the token has them
([Clock skew and leeway](#clock-skew-and-leeway)). The token must name its key (`kid`), and is
verified with that key's own algorithm, which must be one of `algorithms`; the algorithm in the
token's header does not choose it. The subject is the `sub` claim, its groups the `groups` claim
(`groups_claim` renames it; see [Tokens without groups](#tokens-without-groups)), and its tenant
the claim named by `tenant_claim`, which every token must then carry.

The published keys are fetched with a 5-second `timeout` and kept for `lifespan` (300 seconds), so
a key your provider rotates out stops verifying within that time. A token naming a key that is not
in the set fetches the keys again, at most once every `cooldown` (30 seconds); until then such a
token answers 401 at once, so made-up key ids cannot make the service hammer your provider. If the
key endpoint cannot be reached or returns no usable keys, requests answer 503.

`jwks_url` must be `https://`. The keys are fetched with the system's trust store; for an identity
provider whose certificate your own CA signed, pass `ssl_context`, as for the
[permission services](../operations.md#resilience-of-remote-permission-services), instead of
setting `SSL_CERT_FILE` for the whole process:

```python
import ssl

tls = ssl.create_default_context(cafile="/etc/ssl/internal-ca.pem")
authenticate = OIDCAuthenticator(
    issuer="https://login.internal/",
    audience="knowledge-api",
    jwks_url="https://login.internal/.well-known/jwks.json",
    ssl_context=tls,
)
```

For other token formats, pass your own function: it receives the bearer token and returns the
`Subject`, or raises `wardowl.server.InvalidToken`. It may be async; a synchronous function, such
as `OIDCAuthenticator`, runs in a worker thread, so a slow identity provider or database delays
only the requests waiting on it, never the whole service.

```python
from wardowl import Subject
from wardowl.server import InvalidToken, create_app


async def authenticate(token: str) -> Subject:
    session = await sessions.verify(token)  # your session store or identity provider
    if session is None:
        raise InvalidToken("unknown session")
    return Subject(session.user_id, session.groups, {"tenant": session.tenant})


app = create_app(retriever, authenticate)
```

### Clock skew and leeway

The token's times are checked against the service's own clock. `exp` (expiry) must be in the
future; `nbf` (not before) and `iat` (issued at), when the token has them, must not be. `leeway`
(seconds, 0 by default) widens all three checks: a token is accepted up to `leeway` seconds after
its `exp`, and up to `leeway` seconds before its `nbf` or `iat`.

With the default, a provider whose clock runs ahead of the service's issues tokens that the service
refuses until its own clock catches up. With the provider's clock one second ahead, a token used
within a second of being issued answers 401, and the same token works a moment later: the 401s
come and go, and the response does not say why ([Why a token was refused](#why-a-token-was-refused)
shows where to look). Keep both clocks synchronized, with NTP for example, and where they can
drift apart, allow a few seconds:

```python
authenticate = OIDCAuthenticator(
    issuer="https://login.example.com/",
    audience="knowledge-api",
    jwks_url="https://login.example.com/.well-known/jwks.json",
    leeway=5,  # seconds, for exp, nbf and iat alike
)
```

The cost is the same leeway on the expiry: an expired token is still accepted for those 5 seconds.
That is why the default stays 0: a non-zero default would let every token outlive its `exp`, and a
minor release never weakens a default that keeps a protection on ([support policy](../support.md)).

### Tokens without groups

A token without the groups claim gives a subject with no groups. With an authorizer that decides by
the subject's groups, such as `MetadataACLAuthorizer`, the user then loses every access that came
through a group. That is right for a provider that sends no groups, and wrong when the claim went
missing: a group mapper removed from the client, or a provider that leaves a long group list out of
the token. Microsoft Entra ID, for one, leaves the `groups` claim out of a JWT for a user in more
than 200 groups and sends an overage claim instead
([access token claims](https://learn.microsoft.com/entra/identity-platform/access-token-claims-reference#payload-claims)).
The request then answers 200 with fewer hits, and nothing fails. `OIDCAuthenticator` logs each such
token at DEBUG on the `wardowl` logger:

```text
wardowl found no groups claim in a token; its subject has no groups
```

To refuse those tokens instead, pass `require_groups=True`: a token without the claim then answers
401, logged as `MissingRequiredClaimError (groups)`. An empty list is a claim, and passes: it says
the user is in no groups. Before you turn it on, check what your provider sends for a user in no
groups: if it leaves the claim out for them, `require_groups` refuses those users. A claim that is
not a list of strings, `null` included, is refused either way.

### Why a token was refused

A refused token answers 401 with the same body whatever the reason, so the caller learns nothing
about which check failed. `OIDCAuthenticator` logs the check at INFO on the `wardowl` logger, one
line per refused token, with nothing from the token itself: not the token, its signature, or the
values of its claims.

```text
wardowl refused a token: ImmatureSignatureError
wardowl refused a token: ExpiredSignatureError
wardowl refused a token: MissingRequiredClaimError (tid)
wardowl refused a token: no key published by the identity provider matches the token's kid
```

A check PyJWT made is named by its exception's class: `ImmatureSignatureError` for an `nbf` or
`iat` later than the service's clock plus `leeway`, which is what a provider's clock running ahead
looks like; `ExpiredSignatureError`; `InvalidAudienceError`; `InvalidIssuerError`;
`InvalidSignatureError`; `MissingRequiredClaimError` with the claim's name; and `DecodeError` for
a token that is not a JWT. wardowl's own checks (the key id, the key's algorithm, `sub`, and the
groups and tenant claims) are described in words.

Unless your logging configuration says otherwise, Python shows only warnings and errors, so these
lines do not appear by default. To see them without turning on INFO for every library:

```python
import logging

logging.basicConfig()  # a handler on the root logger, if your application sets none up
logging.getLogger("wardowl").setLevel(logging.INFO)  # DEBUG adds the tokens without groups
```

A line is written for every refused request, made-up tokens included. `create_app` itself logs
nothing for a 401, so an authenticator of your own that raises `InvalidToken` logs its reasons only
if it does so itself.

### Mapping a real provider's tokens

What lands in `sub`, `groups` and the tenant claim is set by your identity provider, and the
defaults rarely match the ids in your permissions. With Keycloak, for example:

- **`sub` is Keycloak's own user id**, a generated UUID, not the username. Write your permissions
  with that id, or map it to your own ids by overriding `subject`, which receives the verified
  claims. A username or email address is a safe id only if users cannot change it.
- **Groups come with their full path by default** (`/hr`, `/acme/hr`) from the group membership
  mapper. Write your permissions the same way. Turning "Full group path" off sends `hr`, and then
  `/acme/hr` and `/globex/hr` arrive as the same group.
- **A claim that decides access must be one only administrators can set.** A user attribute
  mapped into the token, such as a tenant, must be declared in the realm's user profile with only
  administrators allowed to edit it; otherwise users can change it in the account console.
- **The access token's `aud` names your API only through an audience mapper.** Without one, no
  token passes verification, since `audience` must be in it.

The client's mappers, as in an exported realm:

```json
[
  {"name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper",
   "config": {"claim.name": "groups", "full.path": "true", "access.token.claim": "true"}},
  {"name": "tenant", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper",
   "config": {"user.attribute": "tenant", "claim.name": "tenant", "jsonType.label": "String",
              "access.token.claim": "true"}},
  {"name": "audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
   "config": {"included.custom.audience": "knowledge-api", "access.token.claim": "true"}}
]
```

```python
from wardowl import Subject
from wardowl.server import OIDCAuthenticator

REALM = "https://keycloak.example.com/realms/example"


class KeycloakAuthenticator(OIDCAuthenticator):
    def subject(self, claims):
        found = super().subject(claims)  # checks sub, groups and the tenant claim
        return Subject(directory.user_id_for(found.id), found.groups, found.attributes)


authenticate = KeycloakAuthenticator(
    issuer=REALM,
    audience="knowledge-api",
    jwks_url=f"{REALM}/protocol/openid-connect/certs",
    tenant_claim="tenant",
)
```

`directory` stands for wherever you map Keycloak's ids to yours; leave `subject` alone if your
permissions already use Keycloak's ids.

## Time bounds and threads

```python
app = create_app(retriever, authenticate, retrieval_timeout=10, max_workers=32)
```

`retrieval_timeout` (seconds) bounds each retrieval. One that takes longer is cancelled and answers
503, "the retrieval did not finish in time, so nothing was returned": nothing it found reaches the
caller. The `wardowl` logger records the timeout, and the retrieval's `RetrievalEvent` records
`error = "CancelledError"`. The bound covers the retrieval, not the token's verification. None, the
default, sets no bound: a store or permission service that never answers then holds the request
for as long as it does.

A synchronous store or authorizer call runs in a worker thread, and a thread cannot be stopped from
outside. After the timeout the call runs on until its own client gives up, holding its thread, and
its answer is discarded. Give every store and permission service its own timeouts as well
([pgvector](pgvector.md#timeouts), [the permission services](../operations.md#resilience-of-remote-permission-services)):
`retrieval_timeout` answers the caller on time, and theirs free the thread.

`aretrieve` runs every synchronous call, the store's search and count, the authorizer's filters and
checks, a reranker, a redactor and `CachingAuthorizer`'s Redis calls, in the event loop's default
executor, and the observers too. Unless something replaces it, that executor has CPython's default
size for a `ThreadPoolExecutor`, `min(32, CPUs + 4)` threads, which bounds how many of those calls
run at once in each worker process; the rest wait for a free thread, and the wait counts against
`retrieval_timeout`. `max_workers` replaces the default executor with a pool of that many threads while the
application's lifespan runs, as it does under uvicorn. A mounted sub-application's lifespan does not
run, so there, set the executor in your own application's lifespan with
`asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=...))`. A
synchronous authenticator runs in Starlette's own thread pool, not in this one.

`GET /healthz` answers that the process is up. It does not touch the retriever, the stores or the
threads, so it keeps answering while every retrieval is stuck; use it as a liveness probe only. With
`retrieval_timeout`, stuck retrievals show up as 503 answers from `/v1/retrieve`, which your
monitoring can count.

## In production

- Terminate TLS in front of the service, and keep it on a private network with your stores and
  permission services; the service checks callers, not network access.
- Hits carry no metadata unless named in `metadata_fields`: metadata can name people, groups and
  projects. Keyword search scores can hint at what forbidden chunks contain (threat
  [T17](../threat-model.md)); do not pass them on to end users.
- Every retrieval produces a `RetrievalEvent` for your observers, as anywhere else
  ([Operations](../operations.md)); the service logs no query text. The observers run in a worker
  thread, so an audit sink that blocks delays only the request whose event it is writing, not the
  others.
- Set `retrieval_timeout`, and size the thread pool with `max_workers` for the concurrency you
  expect ([Time bounds and threads](#time-bounds-and-threads)).
- Scale it out like any stateless web service. With a `CachingAuthorizer`, share its cache across
  replicas through Redis ([Revocation](../revocation.md)).

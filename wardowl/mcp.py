"""MCP integration: a search tool for AI agents that returns only what the calling user may read.

Install with `pip install "wardowl[mcp]"` (the MCP Python SDK, version 2).

    from mcp.server import MCPServer
    from wardowl.mcp import add_search_tool

    server = MCPServer("Company knowledge", token_verifier=verifier, auth=auth_settings)
    add_search_tool(server, retriever, subject_for_token)
    server.run(transport="streamable-http")

Whose permissions apply comes from the access token the server verified for the request, never
from the tool's arguments: the model chooses what to search for, not who is asking. Over
Streamable HTTP with a token verifier, `subject_for(token)` turns the verified token (its subject,
client id, scopes and claims) into a wardowl `Subject`, and a call without a verified token is
refused. A synchronous `subject_for` runs in a worker thread, so a directory lookup never stalls
the server's event loop. For a server only one person can reach, such as a local stdio server,
pass that person's `Subject` instead of a function.

The tool returns each permitted chunk's id, text and score, and `complete`. Metadata is left out
unless its keys are named in `metadata`, because permission fields describe who may read what. A
failure of the permission service reaches the model as a tool error without internal details, and
nothing is returned. Any other failure (the vector store, or `subject_for` itself) reaches it only
as "Error executing tool search": the SDK keeps the exception, with whatever hosts or credentials
its text names, in the server's log. Tests hold both to that. With `retrieval_timeout`, a search
that runs longer is cancelled and ends with a tool error, and nothing is returned.
"""

# No `from __future__ import annotations`: the SDK reads the tool's annotations, which refer to
# values (the result limit) known only when the tool is added.

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any

import anyio.to_thread
from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INVALID_REQUEST, ToolAnnotations
from pydantic import BaseModel, Field

from wardowl._timeout import TimedOut, checked, within
from wardowl.authz import AuthorizationError
from wardowl.retriever import PermissionedRetriever
from wardowl.types import Subject

__all__ = ["SearchHit", "SearchResults", "SubjectFor", "add_search_tool"]

_log = logging.getLogger("wardowl")

SubjectFor = Callable[[AccessToken], Subject | Awaitable[Subject]]
"""Turns the verified access token of a request into the subject whose permissions apply."""

DESCRIPTION = (
    "Search the organisation's documents. Returns only passages the calling user is allowed to read, "
    "most relevant first, each with its id, text and relevance score."
)


class SearchHit(BaseModel):
    """A passage the caller may read."""

    id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResults(BaseModel):
    """The passages found for the caller."""

    results: list[SearchHit]
    complete: bool = Field(description="False when passages the caller may read could be missing.")


def add_search_tool(
    server: MCPServer[Any],
    retriever: PermissionedRetriever,
    subject: Subject | SubjectFor,
    *,
    name: str = "search",
    description: str = DESCRIPTION,
    limit: int = 5,
    max_limit: int = 20,
    max_query_length: int = 4000,
    metadata: Sequence[str] = (),
    retrieval_timeout: float | None = None,
) -> None:
    """Register a permission-aware search tool on `server`.

    `subject` is a function from the request's verified `AccessToken` to a `Subject`, or one fixed
    `Subject` for a server only that person can reach; an async function is awaited, and a
    synchronous one runs in a worker thread. The tool takes `query` (at most `max_query_length`
    characters) and `limit` (default `limit`, at most `max_limit`); `metadata` names the metadata
    keys included in each result.

    `retrieval_timeout` (seconds; None, the default, sets no bound) bounds each search: one that
    takes longer is cancelled and ends with a tool error, and nothing is returned. A synchronous
    call it was making in a worker thread cannot be stopped and runs on to its own client's timeout.
    """
    if not 1 <= limit <= max_limit:
        raise ValueError("limit must be at least 1 and at most max_limit")
    if max_query_length < 1:
        raise ValueError("max_query_length must be positive")
    retrieval_timeout = checked(retrieval_timeout)
    if not isinstance(subject, Subject) and not callable(subject):
        raise TypeError("subject must be a wardowl Subject or a function of the access token")
    keys = tuple(metadata)

    async def caller() -> Subject:
        if isinstance(subject, Subject):
            return subject
        token = get_access_token()
        if token is None:  # stdio, or HTTP without a token verifier: nobody to apply permissions for
            raise MCPError(code=INVALID_REQUEST, message="this tool needs an authenticated caller")
        found: Subject | Awaitable[Subject]
        if inspect.iscoroutinefunction(subject):
            found = subject(token)
        else:  # it may block, on a directory or a database: keep it off the event loop
            found = await anyio.to_thread.run_sync(subject, token)
        if inspect.isawaitable(found):
            found = await found
        if not isinstance(found, Subject):
            raise TypeError("subject_for must return a wardowl Subject")
        return found

    async def search(
        query: Annotated[
            str,
            Field(min_length=1, max_length=max_query_length, description="What to look for, in words or a question."),
        ],
        limit: Annotated[int, Field(ge=1, le=max_limit, description="How many passages to return.")] = limit,
    ) -> SearchResults:
        who = await caller()
        try:
            result = await within(retriever.aretrieve(query, who, limit), retrieval_timeout)
        except TimedOut:
            _log.error(
                "wardowl's search took longer than %s s; it was cancelled and nothing was returned", retrieval_timeout
            )
            raise ToolError("the search did not finish in time, so nothing was returned") from None
        except AuthorizationError:
            raise ToolError("the permission check could not be completed, so nothing was returned") from None
        hits = [
            SearchHit(
                id=h.id,
                text=h.text,
                score=h.score,
                metadata={key: h.metadata[key] for key in keys if key in h.metadata},
            )
            for h in result.hits
        ]
        return SearchResults(results=hits, complete=result.complete)

    server.tool(
        name=name,
        description=description,
        # All four hints set explicitly, so a host never falls back to a default for any of them.
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )(search)

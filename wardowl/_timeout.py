"""A time bound on one retrieval, for the HTTP service and the MCP tool.

`within` runs the retrieval as a task of its own and waits for it at most the given number of
seconds. When the time runs out it cancels the task and raises `TimedOut` at once, without waiting
for the task to unwind: the retrieval returns nothing, and its observers still receive its event,
from the cancelled task. A synchronous call the retrieval was making in a worker thread cannot be
stopped from outside; it runs to its end, bounded only by its own client's timeouts, and its answer
is discarded.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Coroutine
from typing import Any, TypeVar

__all__ = ["TimedOut", "checked", "within"]

_T = TypeVar("_T")


class TimedOut(Exception):
    """The retrieval did not finish within its time bound. It was cancelled; nothing is returned."""


def checked(seconds: float | None, name: str = "retrieval_timeout") -> float | None:
    """`seconds` as a float, after checking that it is None or a positive, finite number."""
    value: Any = seconds
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < math.inf:
        raise ValueError(f"{name} must be None or a positive number of seconds")
    return float(value)


async def within(retrieval: Coroutine[Any, Any, _T], seconds: float | None) -> _T:
    """The result of `retrieval`, or `TimedOut` when it takes longer than `seconds` (None: no bound)."""
    if seconds is None:
        return await retrieval
    task = asyncio.create_task(retrieval)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except BaseException:  # the caller was cancelled: the retrieval goes with it
        _abandon(task)
        raise
    if not done:
        _abandon(task)
        raise TimedOut
    return task.result()


# The event loop keeps only weak references to tasks: a cancelled retrieval that nobody awaits is
# held here until it has unwound and reported its event.
_abandoned: set[asyncio.Task[Any]] = set()


def _abandon(task: asyncio.Task[Any]) -> None:
    _abandoned.add(task)
    task.add_done_callback(_consume)
    task.cancel()


def _consume(task: asyncio.Task[Any]) -> None:
    _abandoned.discard(task)
    if not task.cancelled():
        task.exception()  # read, so asyncio never reports it as an exception nobody retrieved

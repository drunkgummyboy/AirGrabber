"""Running coroutines from Tk callbacks.

The Tk event loop is driven by async-tkinter-loop, so every widget callback already executes
inside the running asyncio loop: a handler can start a coroutine, and that coroutine can await
I/O and then update widgets directly.

These helpers replace async_tkinter_loop.async_handler because that one never surfaces task
exceptions (they vanish until garbage collection); here every failure is logged and reported.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

log = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

_running: set[asyncio.Task[Any]] = set()
_error_listeners: list[Callable[[BaseException], None]] = []


def spawn(coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
    """Start `coro` on the running loop and keep a reference until it finishes."""
    try:
        task = asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:
        coro.close()  # avoid a second "coroutine was never awaited" warning
        raise
    _running.add(task)
    task.add_done_callback(_finished)
    return task


def async_command(func: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Adapt an async function into a Tk `command=` / event-binding callback."""

    @wraps(func)
    def handler(*args: P.args, **kwargs: P.kwargs) -> None:
        spawn(func(*args, **kwargs), name=func.__qualname__)

    return handler


def on_task_error(listener: Callable[[BaseException], None]) -> None:
    """Register a callback for failures of tasks started with `spawn`."""
    _error_listeners.append(listener)


async def cancel_all() -> None:
    """Cancel every pending task on the running loop except the caller, and wait for them.

    Used at shutdown. Covers tasks the services start themselves with `asyncio.create_task`
    (image downloads, schedule fetches, sync), not only those started via `spawn`; anything
    left pending when the loop closes would print "Task was destroyed" warnings.
    """
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _finished(task: asyncio.Task[Any]) -> None:
    _running.discard(task)
    if task.cancelled() or (error := task.exception()) is None:
        return
    log.error("Task %s failed", task.get_name(), exc_info=error)
    for listener in _error_listeners:
        try:
            listener(error)
        except Exception:
            log.exception("Task error listener failed")

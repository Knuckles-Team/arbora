"""Bounded canonical ASGI requests on the existing FastMCP owner loop."""

import asyncio
import contextvars
import threading
from concurrent.futures import Future
from typing import Any

_capacity = threading.BoundedSemaphore(16)


async def _unavailable(scope: Any, receive: Any, send: Any) -> None:
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1013})
        return
    from starlette.responses import JSONResponse

    await JSONResponse(
        {"error": "Identity unavailable"},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )(scope, receive, send)


async def dispatch_browser_request(
    app: Any, scope: Any, receive: Any, send: Any
) -> None:
    """Cap before scheduling; keep admission/invoke in one owner-loop task."""
    from arbora.fleet.shared_multiplexer import run_on_served_multiplexer

    if not _capacity.acquire(blocking=False):
        await _unavailable(scope, receive, send)
        return

    async def owner_loop(_multiplexer: Any) -> Any:
        return asyncio.get_running_loop()

    try:
        owner = await run_on_served_multiplexer(owner_loop)
    except asyncio.CancelledError:
        _capacity.release()
        raise
    except RuntimeError:
        _capacity.release()
        await _unavailable(scope, receive, send)
        return
    # Ownership of the capacity slot transfers to the actual task completion,
    # not cancellation acknowledgement or the WebUI response's lifetime.
    await _on_owner(app, scope, receive, send, owner, release=_capacity.release)


async def _on_owner(
    app: Any,
    scope: Any,
    receive: Any,
    send: Any,
    owner: Any,
    *,
    release: Any = lambda: None,
) -> None:
    source = asyncio.get_running_loop()
    if source is owner:
        try:
            await app(scope, receive, send)
        finally:
            release()
        return
    completion: Future[None] = Future()
    finished: Future[None] = Future()
    tasks: list[Any] = []
    cancelled = threading.Event()
    context = contextvars.copy_context()

    async def bridged_receive() -> Any:
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(receive(), source)
        )

    async def bridged_send(message: Any) -> None:
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(send(message), source)
        )

    def done(task: Any) -> None:
        try:
            completion.set_result(task.result())
        except BaseException as exc:
            completion.set_exception(exc)
        finally:
            release()
            finished.set_result(None)

    def submit() -> None:
        operation = None
        try:
            operation = app(scope, bridged_receive, bridged_send)
            task = owner.create_task(operation, context=context)
        except BaseException as exc:
            if asyncio.iscoroutine(operation):
                operation.close()
            # No task owns the slot when construction itself fails.
            completion.set_exception(exc)
            release()
            finished.set_result(None)
            return
        tasks.append(task)
        task.add_done_callback(done)
        if cancelled.is_set():
            task.cancel()

    def cancel() -> None:
        cancelled.set()
        for task in tasks:
            task.cancel()

    try:
        owner.call_soon_threadsafe(submit, context=context)
    except RuntimeError:
        release()
        await _unavailable(scope, receive, send)
        return
    wrapped = asyncio.wrap_future(completion)
    try:
        await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            owner.call_soon_threadsafe(cancel)
        except RuntimeError:
            # Shutdown may close the loop between dispatch and disconnect.
            # Preserve cancellation; never release capacity without completion.
            wrapped.add_done_callback(_consume_result)
            raise asyncio.CancelledError from None
        try:
            async with asyncio.timeout(2):
                await asyncio.shield(asyncio.wrap_future(finished))
        except TimeoutError:
            import logging

            logging.getLogger(__name__).warning(
                "Browser owner cleanup exceeded shutdown bound; capacity remains held"
            )
        # Retrieve the eventual exception even when WebUI has disconnected.
        wrapped.add_done_callback(_consume_result)
        raise


def _consume_result(result: Any) -> None:
    if not result.cancelled():
        result.exception()

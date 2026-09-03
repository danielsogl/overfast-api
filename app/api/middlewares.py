import os
import subprocess
import tempfile
import tracemalloc
from abc import ABC, abstractmethod
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from anyio import to_thread
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response

from app.config import settings
from app.infrastructure.helpers import compute_etag

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI, Request

# Profiling packages aren't installed on production environment
with suppress(ModuleNotFoundError):
    import memray
    import objgraph
    import pyinstrument


def _if_none_match(header: str | None, etag: str) -> bool:
    """Whether *header* lists *etag*, using weak comparison (RFC 9110 §8.8.3.2).

    Weak comparison ignores the ``W/`` prefix, which is what a GET with
    ``If-None-Match`` calls for. Clients may send several tags, comma-separated.
    """
    if not header:
        return False

    wanted = etag.removeprefix("W/")
    return any(
        value.strip().removeprefix("W/") == wanted for value in header.split(",")
    )


class ETagMiddleware(BaseHTTPMiddleware):
    """Tag cacheable responses with a weak ETag and answer ``If-None-Match`` with 304.

    Only responses carrying ``X-Cache-Status`` are tagged. That header comes from
    ``apply_swr_headers`` and from nowhere else, so it marks exactly the cacheable
    payload routes — players, heroes, maps, gamemodes, roles, patch notes — and
    never an error body, the docs page or ``/openapi.json``.

    Also requires the client to send ``settings.conditional_get_header``,
    declaring it stores the ETag, resends it as ``If-None-Match``, and resolves
    a 304 against its own cached body. A client built before this feature
    existed can still have its HTTP stack revalidate against ours on its own
    (matching ``Cache-Control``/``ETag`` alone is enough for that, with no
    cooperation from the client's own code) — and since it never learned to
    resolve a bodyless 304, that surfaced to its users as "no data". Without
    the header this never tags a response, so that client's HTTP stack has no
    ETag to revalidate against in the first place: the same "no tag" case
    below, on every request from it.

    This covers the cache-*miss* path only, because a cache hit never reaches
    FastAPI: nginx serves it straight from Valkey and reads the ETag out of the
    cache envelope (``build/nginx/lua/valkey_handler.lua.template``), where the
    Valkey adapter put one computed by ``compute_etag`` over the bytes it stored.
    That path carries the identical client-declaration gate, keyed off the same
    ``settings.conditional_get_header``.

    Those two bytestreams are not always identical. FastAPI renders through the
    ``response_model``, which reorders and completes fields; the cache holds the
    service's own JSON. So the two paths can hand out different tags for the same
    resource — correct rather than unfortunate, since each tag describes the body
    actually sent. It costs one extra full transfer when a client's polling
    crosses from the miss path to the hit path; every poll after that is a 304.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        if (
            response.status_code != HTTPStatus.OK
            or "X-Cache-Status" not in response.headers
            or settings.conditional_get_header not in request.headers
        ):
            return response

        # BaseHTTPMiddleware hands back a streaming response; the body has to be
        # drained before it can be hashed. Cheap here: these payloads are already
        # fully buffered by JSONResponse, and this path is the rare cache miss.
        body = b"".join([chunk async for chunk in response.body_iterator])
        etag = compute_etag(body)
        response.headers["ETag"] = etag
        headers = dict(response.headers)

        if _if_none_match(request.headers.get("if-none-match"), etag):
            # A 304 has no body, so the 200's Content-Length would be a lie that
            # leaves some clients waiting for octets that never arrive.
            headers.pop("content-length", None)
            return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)

        return Response(content=body, status_code=response.status_code, headers=headers)


class OverFastMiddleware(BaseHTTPMiddleware, ABC):  # pragma: no cover
    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> HTMLResponse | JSONResponse:
        # Don't make any profiling if query param is not here
        if not request.query_params.get("profile", False):
            return await call_next(request)

        # Proceed if requested
        return await self._dispatch(request, call_next)

    @abstractmethod
    async def _dispatch(
        self, request: Request, call_next: Callable
    ) -> HTMLResponse | JSONResponse:
        """Concrete dispatch method"""


class MemrayInMemoryMiddleware(OverFastMiddleware):  # pragma: no cover
    async def _dispatch(self, request: Request, call_next: Callable) -> HTMLResponse:
        # Create a temporary file path using an async-friendly thread call
        fd, tmp_bin_name = await to_thread.run_sync(
            lambda: tempfile.mkstemp(suffix=".bin")
        )
        os.close(fd)
        tmp_bin_path = Path(tmp_bin_name)

        # Start Memray Tracker with the in-memory buffer
        destination = memray.FileDestination(path=tmp_bin_path, overwrite=True)
        with memray.Tracker(destination=destination):
            await call_next(request)

        # Convert the binary profiling data to an HTML report
        html_report = self.generate_html_report(tmp_bin_path)

        # Return the HTML report to the user directly
        return HTMLResponse(content=html_report, media_type="text/html")

    def generate_html_report(self, tmp_bin_path: Path) -> str:
        """
        Converts the binary tracking data in `buffer` to an HTML report.
        """
        # Create a temporary file for the HTML output
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete_on_close=True
        ) as tmp_html_file:
            tmp_html_path = Path(tmp_html_file.name)

        # Use subprocess to call memray CLI to generate HTML flamegraph
        subprocess.run(  # noqa: S603
            [
                "/bin/uv",
                "run",
                "memray",
                "flamegraph",
                "-o",
                str(tmp_html_path),
                str(tmp_bin_path),
            ],
            check=True,
        )
        return tmp_html_path.read_text()


class PyInstrumentMiddleware(OverFastMiddleware):  # pragma: no cover
    async def _dispatch(self, request: Request, call_next: Callable) -> HTMLResponse:
        with pyinstrument.Profiler(interval=0.001, async_mode="enabled") as profiler:
            await call_next(request)

        return HTMLResponse(profiler.output_html())


class TraceMallocMiddleware(OverFastMiddleware):  # pragma: no cover
    def __init__(self, app: FastAPI):
        super().__init__(app)
        tracemalloc.start()

    async def _dispatch(self, request: Request, call_next: Callable) -> JSONResponse:
        # Take a snapshot before the request
        snapshot_before = tracemalloc.take_snapshot()

        # Process the request
        await call_next(request)

        # Take a snapshot after the request
        snapshot_after = tracemalloc.take_snapshot()

        # Compute the difference
        top_stats = snapshot_after.compare_to(snapshot_before, "lineno")

        # Log the top memory usage changes
        memory_report = [
            {
                "file": stat.traceback[0].filename,
                "line": stat.traceback[0].lineno,
                "size_diff": stat.size_diff,
                "size": stat.size,
                "count": stat.count_diff,
            }
            for stat in top_stats[:10]  # Top 10 memory diffs
        ]

        return JSONResponse(content=memory_report)


class ObjGraphMiddleware(OverFastMiddleware):  # pragma: no cover
    async def _dispatch(self, request: Request, call_next: Callable) -> JSONResponse:
        # Capture common object types before processing
        objects_before = objgraph.most_common_types(limit=10)
        objgraph_count_before = {obj[0]: obj[1] for obj in objects_before}

        # Process the request
        await call_next(request)

        # Capture common object types after processing
        objects_after = objgraph.most_common_types(limit=10)
        objgraph_count_after = {obj[0]: obj[1] for obj in objects_after}

        # Calculate new objects
        new_objects = {
            obj: count - objgraph_count_before.get(obj, 0)
            for obj, count in objgraph_count_after.items()
            if count > objgraph_count_before.get(obj, 0)
        }

        # Compare and create a report of differences in object types
        memory_report = {
            "before": objgraph_count_before,
            "after": objgraph_count_after,
            "new_objects": new_objects,
        }

        return JSONResponse(content=memory_report)

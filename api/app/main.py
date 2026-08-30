"""FlightPortrait network public API.

App factory in the house pattern: everything injectable through app.state
(settings, sessionmaker, readsb client), a module-level `app` for uvicorn.
The service reads the local aggregator readsb and persists the
stations registry in its own Postgres. Apache-2.0; the data it
serves is ODbL 1.0. Published so the station privacy handling is
auditable — there is one network, and this is the code it runs.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import make_sessionmaker
from . import errors
from . import openapi as spec
from .readsb import ReadsbClient
from .routes_history import router as history_router
from .routes_live import router as live_router
from .routes_refdata import router as refdata_router
from .routes_stations import router as stations_router
from .routes_db import RouteBook
from .settings import Settings
from .snapshot import Snapshot
from .traces import TraceBook

log = logging.getLogger("network-api")


def create_network_api_app(settings=None, sessionmaker=None, readsb=None,
                           start_pollers=True) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if start_pollers:
            from .poller import start_pollers as _start
            tasks = _start(app)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await app.state.readsb.aclose()

    app = FastAPI(
        title="FlightPortrait network API",
        version="1.0.0",
        description=spec.DESCRIPTION,
        servers=spec.SERVERS,
        openapi_tags=spec.TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker or make_sessionmaker(
        settings.database_url)
    app.state.readsb = readsb or ReadsbClient(
        settings.upstream_url, settings.upstream_timeout_s)
    app.state.snapshot = Snapshot()
    app.state.traces = TraceBook(
        retention_s=settings.trace_retention_s,
        max_points=settings.trace_max_points,
        max_aircraft=settings.trace_max_aircraft)
    app.state.presence = {}
    app.state.presence_available = True
    app.state.presence_at = 0.0
    app.state.routes = RouteBook(settings.routes_path)
    from .legs_db import LegBook
    app.state.legs = LegBook(settings.legs_path)

    # Credential-less open-data API: wildcard origins are safe because no
    # cookie or token ever grants more than anonymous access.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

    # One error envelope, decided here: {"error": code, "detail": text},
    # always no-store. Framework-raised errors (unknown paths, path-type
    # mismatches) are normalized into it too, so no default shape leaks.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        headers = dict(exc.headers or {})
        headers["Cache-Control"] = "no-store"
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": errors.code_for(exc), "detail": detail},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(loc) for loc in first.get("loc", ())
                         if loc != "path")
        message = first.get("msg", "invalid request")
        detail = "%s: %s" % (where, message) if where else message
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": detail},
            headers={"Cache-Control": "no-store"},
        )

    # Any uncaught error is still the documented envelope, never a
    # stack trace or a stringified row (which could carry a UUID). The
    # cause is logged server-side; the client gets an opaque code.
    @app.exception_handler(Exception)
    async def unhandled_error(request, exc):
        log.exception("unhandled error on %s %s",
                      request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "internal error"},
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(live_router)
    app.include_router(history_router)
    app.include_router(stations_router)
    app.include_router(refdata_router)

    @app.get("/healthz", tags=["Meta"], summary="Health",
             description="Liveness. Does not check the aggregator; a sick "
                         "readsb shows up as 503 on the data routes.",
             operation_id="healthz",
             responses=spec.ok(spec.EX_HEALTHZ, schema=spec.SCH_HEALTHZ),
             openapi_extra=spec.HIDDEN)
    def healthz():
        return {"ok": True}

    @app.get("/", tags=["Meta"], summary="Index",
             description="Name, docs, spec, source, terms, attribution, "
                         "and the feed address.",
             operation_id="index",
             responses=spec.ok(spec.EX_INDEX, schema=spec.SCH_INDEX),
             openapi_extra=spec.HIDDEN)
    def index():
        return {
            "name": "FlightPortrait network API",
            "docs": "https://docs.flightportrait.com/api/reference",
            "openapi": "/openapi.json",
            "swagger": "/docs",
            "source": settings.source_url,
            "terms": settings.terms_url,
            "attribution": settings.attribution,
            "feed": "feed.flightportrait.com:30004 (beast_reduce_plus_out)",
        }

    return app


app = create_network_api_app()

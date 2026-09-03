import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from agent_data_oracle.config import database_url_from_environment
from agent_data_oracle.database import Database
from agent_data_oracle.observability import request_log_fields

request_logger = logging.getLogger("agent_data_oracle.http")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "<unmatched>"


def create_app(*, database_url: str | None = None) -> FastAPI:
    database = Database(database_url or database_url_from_environment())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await database.close()

    app = FastAPI(title="Agent Data Oracle", lifespan=lifespan)
    app.state.database = database

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/ready")
    async def ready() -> Response:
        try:
            is_ready = await database.is_ready()
        except SQLAlchemyError:
            is_ready = False
        if not is_ready:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.middleware("http")
    async def log_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = str(uuid4())
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            request_logger.info(
                "request_completed",
                extra=request_log_fields(
                    correlation_id=correlation_id,
                    method=request.method,
                    path=_route_template(request),
                    status=status,
                ),
            )

    return app

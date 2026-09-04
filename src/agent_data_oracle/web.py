import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from agent_data_oracle.auth import (
    AuthenticatedOperator,
    EmailProvider,
    HumanAccess,
    SecondFactorResult,
    email_provider_from_environment,
    utc_now,
)
from agent_data_oracle.config import (
    auth_secret_from_environment,
    database_url_from_environment,
    founder_emails_from_environment,
    public_origin_from_environment,
    secure_cookies_from_environment,
    validated_public_origin,
)
from agent_data_oracle.database import Database
from agent_data_oracle.observability import request_log_fields

request_logger = logging.getLogger("agent_data_oracle.http")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "<unmatched>"


async def _form_fields(request: Request) -> Mapping[str, str]:
    body = await request.body()
    if len(body) > 8_192:
        return {}
    values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: entries[0] for key, entries in values.items() if entries}


def create_app(
    *,
    database_url: str | None = None,
    auth_secret: bytes | None = None,
    email_provider: EmailProvider | None = None,
    clock: Callable[[], datetime] = utc_now,
    public_origin: str | None = None,
    secure_cookies: bool | None = None,
    founder_emails: frozenset[str] | None = None,
) -> FastAPI:
    database = Database(database_url or database_url_from_environment())
    human_access = HumanAccess(
        database=database,
        secret=auth_secret or auth_secret_from_environment(),
        email_provider=(
            email_provider
            if email_provider is not None
            else email_provider_from_environment()
        ),
        clock=clock,
        founder_emails=founder_emails or founder_emails_from_environment(),
    )
    use_secure_cookies = (
        secure_cookies
        if secure_cookies is not None
        else secure_cookies_from_environment()
    )
    sign_in_origin = (
        validated_public_origin(public_origin, require_https=use_secure_cookies)
        if public_origin is not None
        else public_origin_from_environment()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await database.close()

    app = FastAPI(title="Agent Data Oracle", lifespan=lifespan)
    if public_origin is not None or secure_cookies_from_environment():
        trusted_hostname = urlsplit(sign_in_origin).hostname
        if trusted_hostname is None:
            raise ValueError("PUBLIC_ORIGIN must include a hostname")
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[trusted_hostname],
        )
    app.state.database = database
    app.state.human_access = human_access

    def response_with_csrf(
        request: Request,
        template_name: str,
        context: dict[str, object] | None = None,
        *,
        status_code: int = 200,
    ) -> Response:
        csrf_token = human_access.issue_csrf_token()
        response = templates.TemplateResponse(
            request,
            template_name,
            {"csrf_token": csrf_token, **(context or {})},
            status_code=status_code,
        )
        response.set_cookie(
            "ado_csrf",
            csrf_token,
            secure=use_secure_cookies,
            httponly=True,
            samesite="lax",
            max_age=900,
        )
        return response

    async def authenticated_operator(
        request: Request,
    ) -> AuthenticatedOperator | None:
        return await human_access.authenticated_operator(
            request.cookies.get("ado_session")
        )

    def csrf_is_valid(request: Request, fields: Mapping[str, str]) -> bool:
        return human_access.csrf_token_is_valid(
            request.cookies.get("ado_csrf"), fields.get("csrf_token")
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/sign-in", response_class=HTMLResponse)
    async def sign_in(request: Request) -> Response:
        return response_with_csrf(request, "sign_in.html")

    @app.post("/auth/sign-in", response_class=HTMLResponse, status_code=202)
    async def request_sign_in(request: Request) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        network_identity = (
            request.client.host if request.client is not None else "unknown"
        )
        await human_access.request_sign_in(
            email=fields.get("email", ""),
            network_identity=network_identity,
            base_url=sign_in_origin,
        )
        return templates.TemplateResponse(
            request, "sign_in_requested.html", status_code=202
        )

    @app.get("/auth/verify", response_class=HTMLResponse)
    async def confirm_sign_in(request: Request, token: str = "") -> Response:
        return response_with_csrf(request, "verify.html", {"token": token})

    @app.post("/auth/verify", response_class=HTMLResponse)
    async def verify_sign_in(request: Request) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        grant = await human_access.consume_sign_in_token(fields.get("token", ""))
        if grant is None:
            return templates.TemplateResponse(
                request, "invalid_link.html", status_code=400
            )
        response = RedirectResponse(grant.destination, status_code=303)
        response.set_cookie(
            "ado_session",
            grant.token,
            secure=use_secure_cookies,
            httponly=True,
            samesite="lax",
            max_age=43_200,
        )
        csrf_token = human_access.issue_csrf_token()
        response.set_cookie(
            "ado_csrf",
            csrf_token,
            secure=use_secure_cookies,
            httponly=True,
            samesite="lax",
            max_age=900,
        )
        return response

    @app.get("/declare", response_class=HTMLResponse)
    async def declaration(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is not None:
            return RedirectResponse("/app", status_code=303)
        if not human_access.reauthentication_is_current(operator):
            return RedirectResponse("/sign-in", status_code=303)
        return response_with_csrf(request, "declare.html", {"error": False})

    @app.post("/declare", response_class=HTMLResponse)
    async def record_declaration(request: Request) -> Response:
        fields = await _form_fields(request)
        session_token = request.cookies.get("ado_session")
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        operator = await authenticated_operator(request)
        sells_value = fields.get("sells_into_us")
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is not None:
            return RedirectResponse("/app", status_code=303)
        if not human_access.reauthentication_is_current(operator):
            return RedirectResponse("/sign-in", status_code=303)
        if session_token is None or sells_value not in {"yes", "no"}:
            return response_with_csrf(request, "declare.html", {"error": True})
        recorded = await human_access.record_declaration(
            session_token=session_token,
            operator_type=fields.get("operator_type", ""),
            sells_into_us=sells_value == "yes",
        )
        if not recorded:
            return response_with_csrf(request, "declare.html", {"error": True})
        return RedirectResponse("/app", status_code=303)

    @app.get("/app", response_class=HTMLResponse)
    async def application_shell(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        return response_with_csrf(request, "app.html", {"operator": operator})

    @app.get("/founder")
    async def founder_controls(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None or not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if not await human_access.founder_factor_exists(operator.operator_id):
            return RedirectResponse("/founder/totp/enroll", status_code=303)
        if operator.founder_second_factor_at is None:
            return RedirectResponse("/founder/totp", status_code=303)
        return templates.TemplateResponse(request, "founder.html")

    @app.get("/founder/totp/enroll")
    async def founder_totp_enrollment(request: Request) -> Response:
        session_token = request.cookies.get("ado_session")
        if session_token is None:
            return RedirectResponse("/sign-in", status_code=303)
        secret = await human_access.totp_enrollment_secret(session_token)
        if secret is None:
            return RedirectResponse("/sign-in", status_code=303)
        return response_with_csrf(
            request,
            "totp_enroll.html",
            {"error": False, "totp_secret": secret},
        )

    @app.post("/founder/totp/enroll")
    async def confirm_founder_totp_enrollment(request: Request) -> Response:
        fields = await _form_fields(request)
        session_token = request.cookies.get("ado_session")
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if session_token is None:
            return RedirectResponse("/sign-in", status_code=303)
        secret = await human_access.totp_enrollment_secret(session_token)
        if secret is None:
            return RedirectResponse("/sign-in", status_code=303)
        recovery_codes = await human_access.confirm_totp_enrollment(
            session_token=session_token, code=fields.get("code", "")
        )
        if recovery_codes is None:
            return response_with_csrf(
                request,
                "totp_enroll.html",
                {"error": True, "totp_secret": secret},
                status_code=403,
            )
        return templates.TemplateResponse(
            request,
            "recovery_codes.html",
            {"recovery_codes": recovery_codes},
        )

    @app.get("/founder/totp")
    async def founder_totp_challenge(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if (
            operator is None
            or not operator.is_founder
            or not await human_access.founder_factor_exists(operator.operator_id)
        ):
            return HTMLResponse("Not found", status_code=404)
        return response_with_csrf(request, "totp_challenge.html", {"error": False})

    @app.post("/founder/totp")
    async def verify_founder_totp(request: Request) -> Response:
        fields = await _form_fields(request)
        session_token = request.cookies.get("ado_session")
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if session_token is None:
            return HTMLResponse("Not found", status_code=404)
        result = await human_access.verify_founder_second_factor(
            session_token=session_token,
            credential=fields.get("credential", ""),
        )
        if result is not SecondFactorResult.VERIFIED:
            response = response_with_csrf(
                request,
                "totp_challenge.html",
                {
                    "error": result is SecondFactorResult.INVALID,
                    "rate_limited": result is SecondFactorResult.RATE_LIMITED,
                },
                status_code=(429 if result is SecondFactorResult.RATE_LIMITED else 403),
            )
            if result is SecondFactorResult.RATE_LIMITED:
                response.headers["Retry-After"] = "900"
            return response
        return RedirectResponse("/founder", status_code=303)

    @app.post("/auth/sign-out")
    async def sign_out(request: Request) -> Response:
        fields = await _form_fields(request)
        session_token = request.cookies.get("ado_session")
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if session_token is not None:
            await human_access.revoke_session(session_token)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("ado_session")
        response.delete_cookie("ado_csrf")
        return response

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

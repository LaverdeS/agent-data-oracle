import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from agent_data_oracle.agent_access import (
    ALLOWED_AGENT_SCOPES,
    AgentAccess,
    AgentAuthenticationError,
    AgentKeyLimitError,
    AgentPrincipal,
    AgentRateLimitError,
    AgentScopeError,
)
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
from agent_data_oracle.evidence_queue import (
    AuditDecision,
    EvaluationReviews,
    EvidenceQueueContract,
    EvidenceQueues,
    GloballyPausedError,
    IdempotencyConflictError,
    ReviewOutcome,
    SourceUnavailableError,
    SubmissionError,
    serialize_evidence_contract,
    submitted_identifiers_from_form,
    submitted_identifiers_from_json,
)
from agent_data_oracle.observability import request_log_fields

request_logger = logging.getLogger("agent_data_oracle.http")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
api_guide_path = Path(__file__).parents[2] / "docs" / "api-v1.md"


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


async def _form_values(request: Request) -> tuple[dict[str, list[str]], bool]:
    body = await request.body()
    if len(body) > 8_192:
        return {}, False
    return (
        parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True),
        True,
    )


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
    evidence_queues = EvidenceQueues(database, clock=clock)
    agent_access = AgentAccess(database, clock=clock)
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

    app = FastAPI(
        title="Agent Data Oracle API",
        version="v1",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
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
    app.state.evidence_queues = evidence_queues
    app.state.agent_access = agent_access

    def response_with_csrf(
        request: Request,
        template_name: str,
        context: dict[str, object] | None = None,
        *,
        status_code: int = 200,
        reuse_current_token: bool = False,
    ) -> Response:
        csrf_token = (
            request.cookies.get("ado_csrf") if reuse_current_token else None
        ) or human_access.issue_csrf_token()
        response = templates.TemplateResponse(
            request,
            template_name,
            {"csrf_token": csrf_token, **(context or {})},
            status_code=status_code,
        )
        if request.cookies.get("ado_csrf") != csrf_token:
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

    def problem(
        *, status_code: int, code: str, title: str, retry_after: int | None = None
    ) -> JSONResponse:
        response = JSONResponse(
            {
                "code": code,
                "status": status_code,
                "title": title,
                "type": f"https://agent-data-oracle.invalid/problems/{code}",
            },
            status_code=status_code,
            media_type="application/problem+json",
        )
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def released_evidence_response(
        *,
        evaluation_id: UUID,
        contract: EvidenceQueueContract,
        reviews: EvaluationReviews,
        status_code: int = 200,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "contract_version": "v1",
                "evaluation_id": str(evaluation_id),
                "evidence": serialize_evidence_contract(contract),
                "reviews": {
                    "agent_review_reports": [
                        {
                            "outcome": report.outcome,
                            "reported_at": report.reported_at.isoformat(),
                            "report_type": "agent_review",
                        }
                        for report in reviews.agent_review_reports
                    ],
                    "current_human_acknowledgement": (
                        None
                        if reviews.current_human_acknowledgement is None
                        else {
                            "acknowledged_at": (
                                reviews.current_human_acknowledgement.acknowledged_at.isoformat()
                            ),
                            "outcome": reviews.current_human_acknowledgement.outcome,
                            "report_type": "human_acknowledgement",
                        }
                    ),
                    "human_acknowledgement_history": [
                        {
                            "acknowledged_at": (
                                acknowledgement.acknowledged_at.isoformat()
                            ),
                            "outcome": acknowledgement.outcome,
                            "report_type": "human_acknowledgement",
                            "supersedes_previous": acknowledgement.supersedes_previous,
                        }
                        for acknowledgement in reviews.human_acknowledgement_history
                    ],
                },
                "status": "released",
            },
            status_code=status_code,
        )

    async def agent_for_scope(
        request: Request, required_scope: str
    ) -> AgentPrincipal | JSONResponse:
        authorization = request.headers.get("authorization", "")
        scheme, _, bearer_secret = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not bearer_secret:
            return problem(
                status_code=401,
                code="authentication_required",
                title="A bearer agent key is required.",
            )
        try:
            return await agent_access.authenticate(
                bearer_secret=bearer_secret, required_scope=required_scope
            )
        except AgentAuthenticationError:
            return problem(
                status_code=401,
                code="authentication_required",
                title="The bearer agent key is invalid or revoked.",
            )
        except AgentScopeError:
            return problem(
                status_code=403,
                code="insufficient_scope",
                title="This agent key does not have the required scope.",
            )
        except AgentRateLimitError:
            return problem(
                status_code=429,
                code="rate_limited",
                title="This agent key is temporarily rate limited.",
                retry_after=60,
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
        evaluations = await evidence_queues.list_released(
            operator_id=operator.operator_id
        )
        return response_with_csrf(
            request,
            "app.html",
            {"evaluations": evaluations, "operator": operator},
        )

    async def agent_keys_page(
        request: Request,
        *,
        created_secret: str | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        operator = await authenticated_operator(request)
        if operator is None or operator.operator_type is None:
            return RedirectResponse("/sign-in", status_code=303)
        return response_with_csrf(
            request,
            "agent_keys.html",
            {
                "allowed_scopes": tuple(sorted(ALLOWED_AGENT_SCOPES)),
                "created_secret": created_secret,
                "error": error,
                "keys": await agent_access.list_keys(operator_id=operator.operator_id),
            },
            status_code=status_code,
        )

    @app.get("/agent-keys", response_class=HTMLResponse)
    async def agent_keys(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None or operator.operator_type is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not human_access.reauthentication_is_current(operator):
            return RedirectResponse("/sign-in", status_code=303)
        return await agent_keys_page(request)

    @app.post("/agent-keys", response_class=HTMLResponse)
    async def create_agent_key(request: Request) -> Response:
        values, body_is_within_limit = await _form_values(request)
        fields = {key: entries[0] for key, entries in values.items() if entries}
        operator = await authenticated_operator(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if operator is None or operator.operator_type is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not body_is_within_limit or not human_access.reauthentication_is_current(
            operator
        ):
            return RedirectResponse("/sign-in", status_code=303)
        try:
            created = await agent_access.create_key(
                operator_id=operator.operator_id,
                scopes=frozenset(values.get("scopes", [])),
            )
        except (AgentKeyLimitError, ValueError) as error:
            return await agent_keys_page(request, error=str(error), status_code=400)
        return await agent_keys_page(request, created_secret=created.secret)

    @app.post("/agent-keys/{agent_key_id}/revoke", response_class=HTMLResponse)
    async def revoke_agent_key(request: Request, agent_key_id: str) -> Response:
        fields = await _form_fields(request)
        operator = await authenticated_operator(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if operator is None or operator.operator_type is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not human_access.reauthentication_is_current(operator):
            return RedirectResponse("/sign-in", status_code=303)
        try:
            key_id = UUID(agent_key_id)
        except ValueError:
            return HTMLResponse("Not found", status_code=404)
        if not await agent_access.revoke_key(
            operator_id=operator.operator_id, agent_key_id=key_id
        ):
            return HTMLResponse("Not found", status_code=404)
        return RedirectResponse("/agent-keys", status_code=303)

    @app.get("/docs/api-v1.md", include_in_schema=False)
    async def agent_api_guide() -> Response:
        guide = api_guide_path.read_text(encoding="utf-8")
        return Response(guide, media_type="text/markdown")

    @app.post("/api/v1/queues", status_code=201)
    async def api_submit_evidence_queue(request: Request) -> Response:
        principal = await agent_for_scope(request, "queues:submit")
        if isinstance(principal, JSONResponse):
            return principal
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            return problem(
                status_code=400,
                code="validation_failed",
                title="A JSON evidence-queue request is required.",
            )
        body = await request.body()
        if len(body) > 8_192:
            return problem(
                status_code=400,
                code="validation_failed",
                title="The evidence-queue request is too large.",
            )
        try:
            identifiers = submitted_identifiers_from_json(json.loads(body))
            evaluation = await evidence_queues.submit_evaluation(
                operator_id=principal.operator_id,
                idempotency_key=request.headers.get("idempotency-key", ""),
                identifiers=identifiers,
            )
        except (json.JSONDecodeError, SubmissionError):
            return problem(
                status_code=400,
                code="validation_failed",
                title="Submit one to 50 explicit typed identifier rows.",
            )
        except IdempotencyConflictError:
            return problem(
                status_code=409,
                code="idempotency_conflict",
                title="This idempotency key was used for different input.",
            )
        except GloballyPausedError:
            return problem(
                status_code=503,
                code="global_pause",
                title="New evidence queues are paused.",
            )
        except SourceUnavailableError:
            return problem(
                status_code=503,
                code="source_unavailable",
                title="A completed CPSC source revision is unavailable.",
            )
        except SQLAlchemyError:
            return problem(
                status_code=503,
                code="infrastructure_failure",
                title="Evidence evaluation is temporarily unavailable.",
            )
        queue = await evidence_queues.operator_queue(
            operator_id=principal.operator_id, evaluation_id=evaluation.evaluation_id
        )
        if queue is None or queue.contract is None:
            return JSONResponse(
                {
                    "contract_version": "v1",
                    "evaluation_id": str(evaluation.evaluation_id),
                    "status": "pending_founder_audit",
                },
                status_code=202,
            )
        reviews = await evidence_queues.review_history(
            operator_id=principal.operator_id, evaluation_id=evaluation.evaluation_id
        )
        return released_evidence_response(
            evaluation_id=evaluation.evaluation_id,
            contract=queue.contract,
            reviews=reviews,
            status_code=201,
        )

    async def api_released_evidence(
        request: Request,
        evaluation_id: str,
        required_scope: str,
        *,
        record_retrieval: bool = False,
    ) -> Response:
        principal = await agent_for_scope(request, required_scope)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            parsed_evaluation_id = UUID(evaluation_id)
        except ValueError:
            return problem(
                status_code=404, code="resource_not_found", title="Resource not found."
            )
        contract = await evidence_queues.released_contract(
            operator_id=principal.operator_id, evaluation_id=parsed_evaluation_id
        )
        if contract is None:
            return problem(
                status_code=404, code="resource_not_found", title="Resource not found."
            )
        if record_retrieval:
            recorded = await evidence_queues.record_source_evidence_retrieval(
                operator_id=principal.operator_id,
                agent_key_id=principal.agent_key_id,
                evaluation_id=parsed_evaluation_id,
            )
            if recorded is None:
                return problem(
                    status_code=404,
                    code="resource_not_found",
                    title="Resource not found.",
                )
        reviews = await evidence_queues.review_history(
            operator_id=principal.operator_id, evaluation_id=parsed_evaluation_id
        )
        return released_evidence_response(
            evaluation_id=parsed_evaluation_id,
            contract=contract,
            reviews=reviews,
        )

    @app.get("/api/v1/queues/{evaluation_id}")
    async def api_get_evidence_queue(request: Request, evaluation_id: str) -> Response:
        return await api_released_evidence(request, evaluation_id, "queues:read")

    @app.get("/api/v1/queues/{evaluation_id}/evidence")
    async def api_get_source_evidence(request: Request, evaluation_id: str) -> Response:
        return await api_released_evidence(
            request, evaluation_id, "evidence:read", record_retrieval=True
        )

    @app.post("/api/v1/queues/{evaluation_id}/reviews", status_code=201)
    async def api_report_agent_review(request: Request, evaluation_id: str) -> Response:
        principal = await agent_for_scope(request, "reviews:report-agent")
        if isinstance(principal, JSONResponse):
            return principal
        if (
            request.headers.get("content-type", "").split(";", maxsplit=1)[0]
            != "application/json"
        ):
            return problem(
                status_code=400,
                code="validation_failed",
                title="A JSON agent review report is required.",
            )
        try:
            payload = json.loads(await request.body())
            if (
                not isinstance(payload, dict)
                or set(payload) != {"outcome"}
                or not isinstance(payload["outcome"], str)
            ):
                raise ValueError
            outcome = ReviewOutcome(payload["outcome"])
            parsed_evaluation_id = UUID(evaluation_id)
        except (ValueError, TypeError, json.JSONDecodeError):
            return problem(
                status_code=400,
                code="validation_failed",
                title="Submit one supported agent review outcome.",
            )
        report = await evidence_queues.record_agent_review(
            operator_id=principal.operator_id,
            agent_key_id=principal.agent_key_id,
            evaluation_id=parsed_evaluation_id,
            outcome=outcome,
        )
        if report is None:
            return problem(
                status_code=404, code="resource_not_found", title="Resource not found."
            )
        return JSONResponse(
            {
                "outcome": report.outcome,
                "report_type": "agent_review",
                "status": "recorded",
            },
            status_code=201,
        )

    def new_queue_form(
        request: Request, *, error: str | None = None, status_code: int = 200
    ) -> Response:
        idempotency_key = secrets.token_urlsafe(24)
        response = response_with_csrf(
            request,
            "queues_new.html",
            {"error": error, "idempotency_key": idempotency_key},
            status_code=status_code,
        )
        response.headers["X-Idempotency-Key"] = idempotency_key
        return response

    @app.get("/queues/new", response_class=HTMLResponse)
    async def new_evidence_queue(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        return new_queue_form(request)

    @app.post("/queues", response_class=HTMLResponse)
    async def submit_evidence_queue(request: Request) -> Response:
        values, body_is_within_limit = await _form_values(request)
        fields = {key: entries[0] for key, entries in values.items() if entries}
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        try:
            identifiers = submitted_identifiers_from_form(
                values, body_is_within_limit=body_is_within_limit
            )
            evaluation = await evidence_queues.submit_evaluation(
                operator_id=operator.operator_id,
                idempotency_key=fields.get("idempotency_key", ""),
                identifiers=identifiers,
            )
        except SubmissionError as error:
            return new_queue_form(request, error=str(error), status_code=400)
        except IdempotencyConflictError:
            return new_queue_form(
                request,
                error="This submission token was already used for different input.",
                status_code=409,
            )
        except GloballyPausedError:
            return HTMLResponse("New evidence queues are paused.", status_code=503)
        except (SourceUnavailableError, SQLAlchemyError):
            return HTMLResponse(
                "Evidence evaluation is temporarily unavailable.", status_code=503
            )
        return RedirectResponse(f"/queues/{evaluation.evaluation_id}", status_code=303)

    @app.get("/queues/{evaluation_id}", response_class=HTMLResponse)
    async def evidence_queue(request: Request, evaluation_id: str) -> Response:
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        try:
            parsed_evaluation_id = UUID(evaluation_id)
        except ValueError:
            return HTMLResponse("Not found", status_code=404)
        queue = await evidence_queues.operator_queue(
            operator_id=operator.operator_id, evaluation_id=parsed_evaluation_id
        )
        if queue is None:
            return HTMLResponse("Not found", status_code=404)
        if queue.is_pending_founder_audit:
            return templates.TemplateResponse(request, "queue_pending.html")
        if queue.contract is None:
            return HTMLResponse("Not found", status_code=404)
        reviews = await evidence_queues.review_history(
            operator_id=operator.operator_id, evaluation_id=parsed_evaluation_id
        )
        return response_with_csrf(
            request,
            "queue.html",
            {
                "evidence": serialize_evidence_contract(queue.contract),
                "evaluation_id": parsed_evaluation_id,
                "reviews": reviews,
            },
            reuse_current_token=True,
        )

    @app.get("/queues/{evaluation_id}/evidence/{evidence_row_id}")
    async def open_source_evidence(
        request: Request, evaluation_id: str, evidence_row_id: str
    ) -> Response:
        operator = await authenticated_operator(request)
        if operator is None or operator.operator_type is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            parsed_evaluation_id = UUID(evaluation_id)
            parsed_evidence_row_id = UUID(evidence_row_id)
        except ValueError:
            return HTMLResponse("Not found", status_code=404)
        retrieval = await evidence_queues.record_source_evidence_retrieval(
            operator_id=operator.operator_id,
            evaluation_id=parsed_evaluation_id,
            evidence_row_id=parsed_evidence_row_id,
        )
        if retrieval is None or retrieval.official_url is None:
            return HTMLResponse("Not found", status_code=404)
        return RedirectResponse(retrieval.official_url, status_code=303)

    @app.post("/queues/{evaluation_id}/acknowledgements")
    async def record_human_acknowledgement(
        request: Request, evaluation_id: str
    ) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        operator = await authenticated_operator(request)
        if operator is None or operator.operator_type is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            parsed_evaluation_id = UUID(evaluation_id)
            outcome = ReviewOutcome(fields.get("outcome", ""))
        except ValueError:
            return HTMLResponse("Invalid review acknowledgement", status_code=400)
        acknowledgement = await evidence_queues.record_human_acknowledgement(
            operator_id=operator.operator_id,
            evaluation_id=parsed_evaluation_id,
            outcome=outcome,
        )
        if acknowledgement is None:
            return HTMLResponse("Not found", status_code=404)
        return RedirectResponse(f"/queues/{parsed_evaluation_id}", status_code=303)

    @app.get("/founder")
    async def founder_controls(request: Request) -> Response:
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        if not await human_access.founder_factor_exists(operator.operator_id):
            return RedirectResponse("/founder/totp/enroll", status_code=303)
        if operator.founder_second_factor_at is None:
            return RedirectResponse("/founder/totp", status_code=303)
        pending_audits = await evidence_queues.pending_audits()
        pause = await evidence_queues.global_pause()
        return response_with_csrf(
            request,
            "founder.html",
            {"pending_audits": pending_audits, "pause": pause},
        )

    async def totp_verified_founder(
        request: Request,
    ) -> AuthenticatedOperator | None:
        operator = await authenticated_operator(request)
        if (
            operator is None
            or not operator.is_founder
            or operator.operator_type is None
            or operator.founder_second_factor_at is None
        ):
            return None
        return operator

    @app.get("/founder/audits/{evaluation_id}", response_class=HTMLResponse)
    async def inspect_founder_audit(request: Request, evaluation_id: str) -> Response:
        if await totp_verified_founder(request) is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            parsed_evaluation_id = UUID(evaluation_id)
        except ValueError:
            return HTMLResponse("Not found", status_code=404)
        queue = await evidence_queues.pending_audit_contract(
            evaluation_id=parsed_evaluation_id
        )
        if queue is None:
            return HTMLResponse("Not found", status_code=404)
        return response_with_csrf(
            request,
            "queue.html",
            {
                "audit": True,
                "evidence": serialize_evidence_contract(queue),
                "evaluation_id": evaluation_id,
            },
        )

    async def record_audit(
        request: Request, evaluation_id: str, decision: AuditDecision
    ) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        founder = await totp_verified_founder(request)
        if founder is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            parsed_evaluation_id = UUID(evaluation_id)
            recorded = await evidence_queues.record_founder_audit(
                evaluation_id=parsed_evaluation_id,
                founder_id=founder.operator_id,
                decision=decision,
                reason_category=fields.get("reason_category"),
            )
        except (ValueError, TypeError):
            return HTMLResponse("Invalid founder audit request", status_code=400)
        if not recorded:
            return HTMLResponse("Not found", status_code=404)
        return RedirectResponse("/founder", status_code=303)

    @app.post("/founder/audits/{evaluation_id}/approve")
    async def approve_founder_audit(request: Request, evaluation_id: str) -> Response:
        return await record_audit(request, evaluation_id, AuditDecision.APPROVED)

    @app.post("/founder/audits/{evaluation_id}/reject")
    async def reject_founder_audit(request: Request, evaluation_id: str) -> Response:
        return await record_audit(request, evaluation_id, AuditDecision.REJECTED)

    @app.post("/founder/pause")
    async def pause_new_work(request: Request) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        founder = await totp_verified_founder(request)
        if founder is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            await evidence_queues.activate_manual_pause(
                founder_id=founder.operator_id,
                reason_category=fields.get("reason_category", ""),
            )
        except ValueError:
            return HTMLResponse("A pause reason category is required.", status_code=400)
        return RedirectResponse("/founder", status_code=303)

    @app.post("/founder/pause/resolve")
    async def resolve_new_work_pause(request: Request) -> Response:
        fields = await _form_fields(request)
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        founder = await totp_verified_founder(request)
        if founder is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            await evidence_queues.resolve_pause(
                founder_id=founder.operator_id,
                resolution_note=fields.get("resolution_note", ""),
            )
        except ValueError:
            return HTMLResponse("A pause resolution note is required.", status_code=400)
        return RedirectResponse("/founder", status_code=303)

    @app.get("/founder/totp/enroll")
    async def founder_totp_enrollment(request: Request) -> Response:
        session_token = request.cookies.get("ado_session")
        if session_token is None:
            return RedirectResponse("/sign-in", status_code=303)
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
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
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
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
        if operator is None or not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
        if not await human_access.founder_factor_exists(operator.operator_id):
            return RedirectResponse("/founder/totp/enroll", status_code=303)
        return response_with_csrf(request, "totp_challenge.html", {"error": False})

    @app.post("/founder/totp")
    async def verify_founder_totp(request: Request) -> Response:
        fields = await _form_fields(request)
        session_token = request.cookies.get("ado_session")
        if not csrf_is_valid(request, fields):
            return HTMLResponse("Invalid request token", status_code=403)
        if session_token is None:
            return HTMLResponse("Not found", status_code=404)
        operator = await authenticated_operator(request)
        if operator is None:
            return RedirectResponse("/sign-in", status_code=303)
        if not operator.is_founder:
            return HTMLResponse("Not found", status_code=404)
        if operator.operator_type is None:
            return RedirectResponse("/declare", status_code=303)
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

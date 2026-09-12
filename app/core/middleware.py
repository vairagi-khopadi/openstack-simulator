"""Cross-cutting HTTP concerns: app factory, microversion headers, token resolution
and the global failure-injection hook."""
from __future__ import annotations

import asyncio
import random
import time
from html import escape
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Union, get_args, get_origin

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import API_VERSIONS, now_utc, service_url, settings
from app.core.database import SessionLocal
from app.core.microversion import NegotiationError, negotiate
from app.core.pagination import PaginationError
from app.models.failure import FailureInjection
from app.models.identity import Project, Token, User

# --------------------------------------------------------------------------------------
# Service-shaped error bodies
# --------------------------------------------------------------------------------------

_NOVA_KEYS: dict[int, str] = {
    400: "badRequest",
    401: "unauthorized",
    403: "forbidden",
    404: "itemNotFound",
    405: "badMethod",
    409: "conflictingRequest",
    413: "overLimit",
    429: "overLimit",
    500: "computeFault",
    501: "notImplemented",
    503: "serviceUnavailable",
    504: "gatewayTimeout",
}

_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Request Entity Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def _nova_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    key = _NOVA_KEYS.get(status, "computeFault")
    body: dict[str, Any] = {key: {"message": message, "code": status}}
    if extra.get("retry_after"):
        body[key]["retryAfter"] = extra["retry_after"]
    return body


def _neutron_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "NeutronError": {
            "type": extra.get("type", title.replace(" ", "")),
            "message": message,
            "detail": extra.get("detail", ""),
        }
    }


def _keystone_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": status, "title": title, "message": message}}


def _placement_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "errors": [
            {
                "status": status,
                "title": title,
                "detail": message,
                "code": extra.get("code", "placement.undefined_code"),
                "request_id": extra.get("request_id", ""),
            }
        ]
    }


def _octavia_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "faultcode": "Client" if status < 500 else "Server",
        "faultstring": message,
        "debuginfo": None,
    }


def _glance_style(status: int, message: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {"message": message, "code": status, "title": title}


# Swift does not serve JSON errors. swob.Response renders a canned HTML body from the
# status alone -- the reason text is fixed per code and the caller's message never
# reaches the wire. These are swift/common/swob.py's RESPONSE_REASONS.
_SWIFT_REASONS: dict[int, tuple[str, str]] = {
    400: (
        "Bad Request",
        "The server could not comply with the request since it is either malformed "
        "or otherwise incorrect.",
    ),
    401: (
        "Unauthorized",
        "This server could not verify that you are authorized to access the document "
        "you requested.",
    ),
    403: ("Forbidden", "Access was denied to this resource."),
    404: ("Not Found", "The resource could not be found."),
    405: ("Method Not Allowed", "The method is not allowed for this resource."),
    408: (
        "Request Timeout",
        "The server has waited too long for the request to be sent by the client.",
    ),
    409: ("Conflict", "There was a conflict when trying to complete your request."),
    411: ("Length Required", "Content-Length header required."),
    412: ("Precondition Failed", "A precondition for this request was not met."),
    413: (
        "Request Entity Too Large",
        "The body of your request was too large for this server.",
    ),
    416: ("Requested Range Not Satisfiable", "The Range requested is not available."),
    422: ("Unprocessable Entity", "Unable to process the contained instructions"),
    429: ("Too Many Requests", "The client has sent too many requests to the server."),
    500: (
        "Internal Error",
        "The server has either erred or is incapable of performing the requested "
        "operation.",
    ),
    501: ("Not Implemented", "The requested method is not implemented by this server."),
    503: (
        "Service Unavailable",
        "The server is currently unavailable. Please try again at a later time.",
    ),
}


def _swift_style(status: int, message: str, title: str, extra: dict[str, Any]) -> str:
    reason, explanation = _SWIFT_REASONS.get(status, (title, message))
    return f"<html><h1>{escape(reason)}</h1><p>{escape(explanation)}</p></html>"


# Each service speaks its own error dialect. Adding one means adding an entry here,
# not editing a growing if/elif chain.
ERROR_STYLES: dict[str, Callable[[int, str, str, dict[str, Any]], Any]] = {
    "nova": _nova_style,
    "cinder": _nova_style,
    "neutron": _neutron_style,
    "keystone": _keystone_style,
    "placement": _placement_style,
    "octavia": _octavia_style,
    "glance": _glance_style,
    "swift": _swift_style,
}


@dataclass(slots=True)
class ErrorPayload:
    """A rendered error body and the media type its service serves it as."""

    content: Any
    media_type: str = "application/json"
    detail: str = ""

    @property
    def is_json(self) -> bool:
        return self.media_type == "application/json"


# Everything but Swift serves its errors as JSON.
MEDIA_TYPES: dict[str, str] = {"swift": "text/html; charset=UTF-8"}


def error_payload(service: str, status: int, message: str, **extra: Any) -> ErrorPayload:
    """Render an error in the dialect the given service actually puts on the wire."""
    title = _TITLES.get(status, "Error")
    if status == 401 and service != "swift":
        # Every service but Swift runs behind keystonemiddleware, which rejects an
        # unauthenticated request before it reaches the service's own WSGI app -- so
        # the 401 on the wire is Keystone's, whichever service was addressed.
        style = _keystone_style
    else:
        style = ERROR_STYLES.get(service, _keystone_style)
    media_type = MEDIA_TYPES.get(service, "application/json")
    # A non-JSON dialect has nowhere to put the caller's message (Swift's HTML is canned
    # per status), so carry it on a header of our own rather than lose it.
    return ErrorPayload(
        style(status, message, title, extra),
        media_type,
        detail="" if media_type == "application/json" else message,
    )


def error_body(service: str, status: int, message: str, **extra: Any) -> Any:
    """The error body alone, without the media type it is served as."""
    return error_payload(service, status, message, **extra).content


def error_response(
    service: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> Response:
    """A ready-to-return response carrying a service-shaped error."""
    return render_error(error_payload(service, status, message, **extra), status, headers)


def render_error(
    payload: ErrorPayload, status: int, headers: dict[str, str] | None = None
) -> Response:
    sent = dict(headers or {})
    if payload.detail:
        sent.setdefault("X-OpenStack-Simulator-Detail", payload.detail)
    if payload.is_json:
        return JSONResponse(payload.content, status_code=status, headers=sent)
    return Response(
        payload.content, status_code=status, media_type=payload.media_type, headers=sent
    )


def fault(
    service: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    """Build an HTTPException whose detail is already a service-shaped payload."""
    return HTTPException(
        status_code=status,
        detail=error_payload(service, status, message, **extra),
        headers=headers,
    )


def unauthenticated(service: str, request: Request) -> HTTPException:
    """The 401 a real deployment answers a missing or expired token with.

    Swift authenticates requests itself and challenges with its own realm; everything
    else is fronted by keystonemiddleware, which points the client at Keystone.
    """
    if service == "swift":
        parts = request.url.path.split("/")
        challenge = f'Swift realm="{parts[2] if len(parts) > 2 else "unknown"}"'
    else:
        challenge = f'Keystone uri="{service_url("keystone")}"'
    return fault(
        service,
        401,
        "The request you have made requires authentication.",
        headers={"WWW-Authenticate": challenge},
    )


def body_object(service: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Unwrap the single top-level object OpenStack bodies wrap their fields in."""
    value = payload.get(key)
    if not isinstance(value, dict):
        raise fault(service, 400, f"Request body must contain a '{key}' object.")
    return value


# --------------------------------------------------------------------------------------
# Request body base model
# --------------------------------------------------------------------------------------


def _accepts_none(annotation: Any) -> bool:
    if annotation is None or annotation is type(None) or annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        return any(_accepts_none(arg) for arg in get_args(annotation))
    return False


class OSPayload(BaseModel):
    """Base for request bodies.

    OpenStack clients happily send ``"availability_zone": null`` for options the user
    never set. Pydantic would reject those against a non-optional field, so nulls are
    dropped here and the field's own default applies instead. Unknown keys are kept:
    the real APIs carry a long tail of extensions and vendor attributes.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_meaningless_nulls(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        nullable: set[str] = set()
        for name, info in cls.model_fields.items():
            if _accepts_none(info.annotation):
                nullable.add(name)
                if info.alias:
                    nullable.add(info.alias)
        return {
            key: value
            for key, value in data.items()
            if value is not None or key in nullable or key not in cls.model_fields
        }


# --------------------------------------------------------------------------------------
# Auth context / token resolution
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AuthContext:
    token_id: str
    user_id: str
    user_name: str
    project_id: str
    project_name: str
    roles: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


async def resolve_token(token_id: str) -> AuthContext | None:
    """Look a bearer token up and hydrate the project/user it is scoped to."""
    if not token_id:
        return None
    async with SessionLocal() as session:
        token = await session.get(Token, token_id)
        if token is None or token.revoked or token.expires_at <= now_utc():
            return None
        user = await session.get(User, token.user_id)
        project = await session.get(Project, token.project_id) if token.project_id else None
        if user is None or project is None:
            return None
        return AuthContext(
            token_id=token.id,
            user_id=user.id,
            user_name=user.name,
            project_id=project.id,
            project_name=project.name,
            roles=list(token.roles or []),
        )


def auth_dependency(service: str) -> Callable[[Request], Awaitable[AuthContext]]:
    """Build a FastAPI dependency that enforces (or fakes) X-Auth-Token for a service."""

    async def _dependency(request: Request) -> AuthContext:
        token_id = request.headers.get("X-Auth-Token", "")
        ctx = await resolve_token(token_id)
        if ctx is not None:
            request.state.auth = ctx
            return ctx
        if settings.require_auth:
            raise unauthenticated(service, request)
        ctx = await _anonymous_context()
        request.state.auth = ctx
        return ctx

    return _dependency


async def _anonymous_context() -> AuthContext:
    """Fallback identity used when OPENSTACK_SIMULATOR_REQUIRE_AUTH=0 (handy for curl-driven demos)."""
    async with SessionLocal() as session:
        project = (
            await session.execute(
                select(Project).where(Project.name == settings.admin_project)
            )
        ).scalar_one_or_none()
        user = (
            await session.execute(select(User).where(User.name == settings.admin_user))
        ).scalar_one_or_none()
    return AuthContext(
        token_id="anonymous",
        user_id=user.id if user else "anonymous",
        user_name=user.name if user else "anonymous",
        project_id=project.id if project else "anonymous",
        project_name=project.name if project else "anonymous",
        roles=["admin", "member", "reader"],
    )


# --------------------------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 1.0
_scenario_cache: dict[str, Any] = {"at": 0.0, "rules": []}


def invalidate_scenario_cache() -> None:
    _scenario_cache["at"] = 0.0


async def active_rules() -> list[dict[str, Any]]:
    """Active injections, cached for a second so hot paths don't hammer SQLite."""
    now = time.monotonic()
    if now - float(_scenario_cache["at"]) < _CACHE_TTL_SECONDS:
        return list(_scenario_cache["rules"])
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(FailureInjection).where(
                        FailureInjection.active.is_(True),
                        FailureInjection.expires_at > now_utc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        rules = [
            {
                "id": r.id,
                "service": r.service,
                "action": r.action,
                "path_contains": r.path_contains,
                "method": r.method,
                "probability": r.probability,
                "latency_ms": r.latency_ms,
                "message": r.message,
                "params": dict(r.params or {}),
            }
            for r in rows
        ]
    _scenario_cache["at"] = now
    _scenario_cache["rules"] = rules
    return list(rules)


async def _record_hit(rule_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(FailureInjection)
            .where(FailureInjection.id == rule_id)
            .values(hits=FailureInjection.hits + 1)
        )
        await session.commit()


def _matches(rule: dict[str, Any], service: str, request: Request) -> bool:
    if rule["service"] not in (service, "all"):
        return False
    if rule["method"] and rule["method"].upper() != request.method.upper():
        return False
    if rule["path_contains"] and rule["path_contains"] not in request.url.path:
        return False
    return random.random() <= float(rule["probability"])


async def apply_failure(service: str, request: Request) -> Response | None:
    """Return a synthetic failure response when a scenario matches this request."""
    for rule in await active_rules():
        if not _matches(rule, service, request):
            continue
        action = rule["action"]
        message = rule["message"]
        await _record_hit(rule["id"])
        if action == "latency":
            await asyncio.sleep(max(rule["latency_ms"], 0) / 1000.0)
            return None  # delay only, let the real handler run
        if action == "timeout":
            await asyncio.sleep(max(rule["latency_ms"] or 30000, 0) / 1000.0)
            return _fail_response(service, 504, message or "Gateway timeout (injected).")
        if action == "rate_limit":
            retry_after = int(rule["params"].get("retry_after", 5))
            return _fail_response(
                service,
                429,
                message or "Rate limit exceeded (injected).",
                headers={"Retry-After": str(retry_after)},
                retry_after=retry_after,
            )
        if action == "quota_exhausted":
            resource = rule["params"].get("resource", "cores")
            return _fail_response(
                service,
                403,
                message or f"Quota exceeded for {resource} (injected).",
            )
        if action == "503_error":
            return _fail_response(
                service, 503, message or "Service Unavailable (injected)."
            )
        # default: 500_error
        return _fail_response(
            service, 500, message or "Unexpected API Error (injected)."
        )
    return None


def _fail_response(
    service: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> Response:
    response = error_response(service, status, message, **extra)
    response.headers["X-OpenStack-Simulator-Injected"] = "true"
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


# --------------------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------------------


def version_headers(service: str, served: str | None = None) -> dict[str, str]:
    """Report the version this response was rendered at, plus the advertised range.

    ``served`` is the negotiated version; it defaults to the maximum only for callers
    outside a request, since a real service reports what it actually served.
    """
    entry = API_VERSIONS.get(service)
    if entry is None:
        return {}
    name, minimum, maximum = entry
    current = served or maximum
    headers = {
        "OpenStack-API-Version": f"{name} {current}",
        f"X-OpenStack-{name.capitalize()}-API-Version": current,
        f"X-OpenStack-{name.capitalize()}-API-Minimum-Version": minimum,
        f"X-OpenStack-{name.capitalize()}-API-Maximum-Version": maximum,
        "Vary": "OpenStack-API-Version",
    }
    if service == "nova":
        headers["X-OpenStack-Nova-API-Version"] = current
        headers["X-OpenStack-Nova-API-Minimum-Version"] = minimum
        headers["X-OpenStack-Nova-API-Maximum-Version"] = maximum
    return headers


def create_service_app(service: str, title: str, description: str = "") -> FastAPI:
    """Build a FastAPI app pre-wired with scenario hooks, version headers and errors."""
    # Swagger UI at /docs, ReDoc at /redoc, schema at /openapi.json. The schema is built
    # lazily on first request and then cached, so this costs nothing at startup.
    app = FastAPI(
        title=title,
        description=description
        or (
            f"{title}\n\nSimulated endpoints — no OpenStack deployment is behind them. "
            "Request bodies follow the real service's wire format; see "
            "https://docs.openstack.org/api-ref/ for the authoritative reference."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.service = service

    @app.middleware("http")
    async def _decorate(request: Request, call_next: Callable[..., Any]) -> Response:
        request.state.service = service
        # Negotiate before the handler runs: the version decides what the handler
        # renders, and an unserviceable one is refused instead of being served as
        # something else.
        try:
            negotiated = negotiate(service, request.headers)
        except NegotiationError as exc:
            response = error_response(service, exc.status, exc.message)
            for key, value in version_headers(service).items():
                response.headers[key] = value
            return response
        request.state.microversion = negotiated

        response = await call_next(request)
        for key, value in version_headers(
            service, str(negotiated) if negotiated else None
        ).items():
            response.headers[key] = value
        response.headers.setdefault(
            "x-openstack-request-id", f"req-{random.getrandbits(64):016x}"
        )
        # No real deployment sends this. It is here so a client that reaches an endpoint
        # unexpectedly can tell at a glance that it is talking to the simulator, and to
        # which build of it.
        response.headers["X-OpenStack-Simulator-Version"] = __version__
        return response

    @app.middleware("http")
    async def _scenarios(request: Request, call_next: Callable[..., Any]) -> Response:
        injected = await apply_failure(service, request)
        if injected is not None:
            return injected
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail
        payload = (
            detail
            if isinstance(detail, ErrorPayload)
            else error_payload(service, exc.status_code, str(detail))
        )
        return render_error(payload, exc.status_code, exc.headers)

    @app.exception_handler(PaginationError)
    async def _bad_marker(request: Request, exc: PaginationError) -> Response:
        # Every service answers a marker it cannot find with a 400 rather than an empty
        # page -- an empty page would look like the end of the collection.
        return error_response(service, 400, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", ())[1:]) or "body"
        message = f"Invalid input for field '{location}': {first.get('msg', 'invalid')}"
        return error_response(service, 400, message)

    return app


def require(service: str) -> Any:
    """Shorthand: ``auth: AuthContext = Depends(require("nova"))``."""
    return Depends(auth_dependency(service))

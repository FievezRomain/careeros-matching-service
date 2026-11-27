"""Versioned FastAPI scoring boundary and composition root."""

import json
import logging
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

import jwt
from fastapi import FastAPI, HTTPException, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import OpenIdConnect
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import AnyHttpUrl, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.base import RequestResponseEndpoint

from careeros_matching.application.scoring import Actor, Denied, ScoreMatch, SnapshotNotFound
from careeros_matching.domain.scoring import BaselineScorer, Contract, MatchInput, MatchResult


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)
    oidc_issuer: AnyHttpUrl
    oidc_audience: str = Field(min_length=1)
    oidc_jwks_url: AnyHttpUrl
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("oidc_issuer", "oidc_jwks_url")
    @classmethod
    def trusted_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("OIDC URLs cannot include credentials, queries or fragments")
        if url.scheme != "https" and url.host not in {"localhost", "127.0.0.1", "[::1]"}:
            raise ValueError("OIDC uses HTTPS outside local development")
        return url


class Problem(Contract):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    correlation_id: UUID


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Actor: ...


class OidcTokenVerifier:
    def __init__(self, jwks: jwt.PyJWKClient, issuer: str, audience: str) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> Actor:
        try:
            claims = jwt.decode(
                token,
                self._jwks.get_signing_key_from_jwt(token),
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "tenant_id"]},
            )
            scope = claims.get("scope", "")
            return Actor.model_validate(
                {
                    "account_id": claims["sub"],
                    "tenant_id": claims["tenant_id"],
                    "scopes": scope.split() if isinstance(scope, str) else [],
                }
            )
        except (jwt.PyJWTError, ValidationError):
            raise ValueError("invalid bearer token") from None


_PROBLEMS = {
    401: ("UNAUTHENTICATED", "A valid bearer token is required."),
    403: ("FORBIDDEN", "The matching:score scope is required."),
    404: ("SNAPSHOT_NOT_FOUND", "A requested snapshot is unavailable to the caller."),
    422: ("VALIDATION_FAILED", "Invalid match input."),
}


def create_app(settings: Settings | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    config = settings or Settings()
    issuer = str(config.oidc_issuer).rstrip("/")
    token_verifier = verifier or OidcTokenVerifier(
        jwt.PyJWKClient(str(config.oidc_jwks_url), timeout=5), issuer, config.oidc_audience
    )
    oidc = OpenIdConnect(
        openIdConnectUrl=f"{issuer}/.well-known/openid-configuration", auto_error=False
    )
    scoring = ScoreMatch(BaselineScorer())
    logger = logging.getLogger("careeros.matching.http")
    logger.setLevel(config.log_level.upper())
    app = FastAPI(title="CareerOS Matching Service", version="1.0.0")

    @app.middleware("http")
    async def correlation(request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            correlation_id = UUID(request.headers.get("X-Correlation-ID", ""))
        except ValueError:
            correlation_id = uuid4()
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = str(correlation_id)
        logger.info(
            json.dumps(
                {
                    "event": "request_completed",
                    "correlation_id": str(correlation_id),
                    "status": response.status_code,
                }
            )
        )
        return response

    @app.exception_handler(HTTPException)
    @app.exception_handler(RequestValidationError)
    async def problem(request: Request, error: Exception) -> JSONResponse:
        status = error.status_code if isinstance(error, HTTPException) else 422
        code, detail = _PROBLEMS.get(
            status, ("REQUEST_FAILED", "The request could not be completed.")
        )
        payload = Problem(
            type=f"urn:careeros:problem:{code.lower()}",
            title=code.replace("_", " ").title(),
            status=status,
            detail=detail,
            instance=f"urn:uuid:{uuid4()}",
            code=code,
            correlation_id=request.state.correlation_id,
        )
        return JSONResponse(
            payload.model_dump(mode="json", by_alias=True),
            status_code=status,
            media_type="application/problem+json",
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    def authenticate(
        token: Annotated[str | None, Security(oidc, scopes=["matching:score"])],
    ) -> Actor:
        scheme, credentials = get_authorization_scheme_param(token or "")
        if scheme.lower() != "bearer" or not credentials:
            raise HTTPException(401)
        try:
            return token_verifier.verify(credentials)
        except ValueError:
            raise HTTPException(401) from None

    @app.post(
        "/v1/internal/matches/score",
        response_model=MatchResult,
        responses={
            status: {
                "description": detail,
                "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
            }
            for status, (_, detail) in _PROBLEMS.items()
        },
    )
    def score(pair: MatchInput, actor: Annotated[Actor, Security(authenticate)]) -> MatchResult:
        try:
            return scoring.execute(actor, pair)
        except Denied:
            raise HTTPException(403) from None
        except SnapshotNotFound:
            raise HTTPException(404) from None

    return app

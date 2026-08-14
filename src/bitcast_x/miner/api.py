"""Authenticated platform API alongside the signed validator protocol."""

from collections.abc import Awaitable, Callable
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bitcast_x.errors import BitcastXError
from bitcast_x.miner.control import MinerControlService


class ClaimRequest(BaseModel):
    """Creator input required to make a pre-publication claim."""

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=128)
    creator_x_id: str = Field(pattern=r"^[0-9]+$")
    draft: str = Field(min_length=1, max_length=20_000)


class SubmissionRequest(BaseModel):
    """Published tweet mapping submitted to the miner protocol."""

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=128)
    tweet_id: str = Field(pattern=r"^[0-9]+$")
    claim_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("claim_id", mode="before")
    @classmethod
    def empty_claim_is_none(cls, value: object) -> object:
        """Allow exclusive campaigns to omit a claim."""

        return None if value == "" else value


def create_control_app(
    service_provider: Callable[[], MinerControlService],
    protocol_app: FastAPI,
    internal_api_token: str,
) -> FastAPI:
    """Create a generic control API without changing validator btauth routes."""

    if len(internal_api_token) < 32:
        raise ValueError("miner API token must contain at least 32 characters")

    app = FastAPI(title="Bitcast X miner API", docs_url=None, redoc_url=None)

    def get_service() -> MinerControlService:
        return service_provider()

    Service = Annotated[MinerControlService, Depends(get_service)]

    @app.middleware("http")
    async def authenticate_control_api(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Fail closed for control routes without affecting validator btauth."""

        if request.url.path.startswith("/api/"):
            authorization = request.headers.get("authorization", "")
            scheme, _, candidate = authorization.partition(" ")
            authorized = scheme.lower() == "bearer" and compare_digest(
                candidate,
                internal_api_token,
            )
            if not authorized:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid or missing internal API credentials"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.exception_handler(BitcastXError)
    async def protocol_error(_request: Request, error: BitcastXError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.get("/api/campaigns")
    async def campaigns(current: Service) -> list[dict[str, object]]:
        return await current.campaigns()

    @app.get("/api/qualification")
    async def qualification(current: Service) -> dict[str, object]:
        return await current.qualification()

    @app.post("/api/claims")
    async def create_claim(body: ClaimRequest, current: Service) -> dict[str, str]:
        return await current.create_claim(body.campaign_id, body.creator_x_id, body.draft)

    @app.get("/api/claims/{claim_id}")
    async def claim_status(claim_id: str, current: Service) -> dict[str, str]:
        result = current.claim_status(claim_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="claim not found")
        return result

    @app.post("/api/submissions")
    async def submit_tweet(body: SubmissionRequest, current: Service) -> dict[str, str]:
        return await current.submit_tweet(body.campaign_id, body.tweet_id, body.claim_id)

    @app.get("/api/submissions")
    async def submissions(current: Service) -> list[dict[str, object]]:
        return await current.submissions()

    @app.get("/api/submissions/{submission_id}")
    async def submission_status(submission_id: str, current: Service) -> dict[str, str]:
        result = current.submission_status(submission_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="submission not found")
        return result

    # Validator-facing health and versioned batch routes remain signed and public.
    app.mount("/", protocol_app)
    return app

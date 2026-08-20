"""Authenticated v1 application API alongside the signed validator protocol."""

from collections.abc import Awaitable, Callable
from hmac import compare_digest
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bitcast_x.errors import BitcastXError
from bitcast_x.miner.control import MinerControlService

EcosystemFilter = Annotated[list[str] | None, Query()]


class ClaimRequest(BaseModel):
    """Creator input required to make a pre-publication claim."""

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=128)
    creator_x_id: str = Field(pattern=r"^[0-9]+$")
    draft: str = Field(min_length=1, max_length=20_000)
    external_id: str | None = Field(default=None, min_length=1, max_length=256)


class SubmissionRequest(BaseModel):
    """Published tweet mapping submitted to the miner protocol."""

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=128)
    tweet_id: str = Field(pattern=r"^[0-9]+$")
    claim_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    creator_x_id: str = Field(pattern=r"^[0-9]+$")
    external_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("claim_id", mode="before")
    @classmethod
    def empty_claim_is_none(cls, value: object) -> object:
        return None if value == "" else value


def _collection(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None, "has_more": False}


def _error(code: str, message: str, *, retryable: bool = False) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def create_control_app(
    service_provider: Callable[[], MinerControlService],
    protocol_app: FastAPI,
    internal_api_token: str,
) -> FastAPI:
    """Create the generic application API without changing validator btauth routes."""

    if len(internal_api_token) < 64:
        raise ValueError("miner API token must contain at least 256 bits of entropy")

    app = FastAPI(
        title="Bitcast X miner application API",
        version="1.0.0",
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
    )

    def get_service() -> MinerControlService:
        return service_provider()

    Service = Annotated[MinerControlService, Depends(get_service)]

    @app.middleware("http")
    async def authenticate_control_api(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Fail closed for application routes without affecting validator btauth."""

        if request.url.path.startswith("/api/v1/"):
            authorization = request.headers.get("authorization", "")
            scheme, _, candidate = authorization.partition(" ")
            authorized = scheme.lower() == "bearer" and compare_digest(
                candidate,
                internal_api_token,
            )
            if not authorized:
                return JSONResponse(
                    status_code=401,
                    content=_error("invalid_authentication", "Invalid or missing credentials."),
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BitcastXError)
    async def protocol_error(_request: Request, error: BitcastXError) -> JSONResponse:
        message = str(error)
        code = "invalid_request"
        if "idempotency key" in message:
            code = "idempotency_conflict"
        elif "miner is not qualified" in message:
            code = "miner_not_qualified"
        elif "not eligible" in message:
            code = "creator_not_eligible"
        elif "not safe" in message:
            code = "claim_not_safe_to_post"
        elif "ecosystem" in message:
            code = "ecosystem_not_enabled"
        elif "capacity" in message:
            code = "queue_capacity_exhausted"
        status_code = 400
        if code == "idempotency_conflict":
            status_code = 409
        elif code == "miner_not_qualified":
            status_code = 403
        return JSONResponse(status_code=status_code, content=_error(code, message))

    @app.get("/api/v1/qualification")
    async def qualification(current: Service) -> dict[str, object]:
        return await current.qualification()

    @app.get("/api/v1/ecosystems")
    async def ecosystems(current: Service) -> dict[str, Any]:
        return _collection(await current.ecosystems())

    @app.get("/api/v1/campaigns")
    async def campaigns(
        current: Service,
        ecosystem_id: EcosystemFilter = None,
    ) -> dict[str, Any]:
        return _collection(await current.campaigns(tuple(ecosystem_id or ())))

    @app.get("/api/v1/campaigns/{campaign_id}")
    async def campaign(campaign_id: str, current: Service) -> dict[str, Any]:
        result = await current.campaign(campaign_id)
        if result is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return result

    @app.get("/api/v1/campaigns/{campaign_id}/eligibility/{creator_x_id}")
    async def eligibility(
        campaign_id: str,
        creator_x_id: str,
        current: Service,
    ) -> dict[str, Any]:
        return await current.eligibility(campaign_id, creator_x_id)

    @app.get("/api/v1/campaigns/{campaign_id}/tweets")
    async def campaign_tweets(
        campaign_id: str,
        current: Service,
        ecosystem_id: EcosystemFilter = None,
    ) -> dict[str, Any]:
        return await current.campaign_tweets(campaign_id, tuple(ecosystem_id or ()))

    @app.post("/api/v1/claims")
    async def create_claim(
        body: ClaimRequest,
        current: Service,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=8, max_length=256),
        ],
    ) -> dict[str, Any]:
        return await current.create_claim(
            body.campaign_id,
            body.creator_x_id,
            body.draft,
            idempotency_key=idempotency_key,
            external_id=body.external_id,
        )

    @app.get("/api/v1/claims")
    async def claims(
        current: Service,
        campaign_id: str | None = None,
        creator_x_id: str | None = None,
        external_id: str | None = None,
        ecosystem_id: EcosystemFilter = None,
    ) -> dict[str, Any]:
        return _collection(
            current.claims(
                campaign_id=campaign_id,
                creator_x_id=creator_x_id,
                external_id=external_id,
                ecosystem_ids=tuple(ecosystem_id or ()),
            )
        )

    @app.get("/api/v1/claims/{claim_id}")
    async def claim_status(claim_id: str, current: Service) -> dict[str, Any]:
        result = current.claim_status(claim_id)
        if result is None:
            raise HTTPException(status_code=404, detail="claim not found")
        return result

    @app.post("/api/v1/submissions")
    async def submit_tweet(
        body: SubmissionRequest,
        current: Service,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=8, max_length=256),
        ],
    ) -> dict[str, Any]:
        return await current.submit_tweet(
            body.campaign_id,
            body.tweet_id,
            body.claim_id,
            body.creator_x_id,
            idempotency_key=idempotency_key,
            external_id=body.external_id,
        )

    @app.get("/api/v1/submissions")
    async def submissions(
        current: Service,
        campaign_id: str | None = None,
        creator_x_id: str | None = None,
        tweet_id: str | None = None,
        external_id: str | None = None,
        ecosystem_id: EcosystemFilter = None,
    ) -> dict[str, Any]:
        return _collection(
            await current.submissions(
                campaign_id=campaign_id,
                creator_x_id=creator_x_id,
                tweet_id=tweet_id,
                external_id=external_id,
                ecosystem_ids=tuple(ecosystem_id or ()),
            )
        )

    @app.get("/api/v1/submissions/{submission_id}")
    async def submission_status(submission_id: str, current: Service) -> dict[str, Any]:
        result = await current.submission_status(submission_id)
        if result is None:
            raise HTTPException(status_code=404, detail="submission not found")
        return result

    app.mount("/", protocol_app)
    return app

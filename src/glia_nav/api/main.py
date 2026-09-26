import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from datetime import datetime, timedelta
from datetime import time as clock_time
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import boto3
import jwt
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from glia_nav.api import search as hybrid
from glia_nav.config import Settings, get_settings
from glia_nav.ingest.eligibility import Eligibility
from glia_nav.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("glia_nav.api")

app = FastAPI(title="glia-nav API", version="0.1.0")


class FrontendOriginCORS(CORSMiddleware):
    """Allows the one frontend origin, which FrontendStack publishes to SSM.

    Search goes through Amplify's /api/* rewrite and stays same-origin. /chat cannot:
    that proxy cuts requests off at 30 s and an agent turn can run longer, so the
    browser calls this service directly. BackendStack cannot reference FrontendStack,
    so the origin is read on first use, as the runtime ARN is.
    """

    def is_allowed_origin(self, origin: str) -> bool:
        settings = get_settings()
        return origin == frontend_origin(settings.aws_region, settings.frontend_origin_parameter)


app.add_middleware(
    FrontendOriginCORS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
bearer = HTTPBearer()


@lru_cache
def jwks_client(region: str, user_pool_id: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
    )


def reject(reason: str) -> HTTPException:
    logger.warning("token rejected", extra={"reason": reason})
    return HTTPException(http_status.HTTP_401_UNAUTHORIZED, "invalid token")


def verified_claims(token: str, settings: Settings) -> dict:
    """Signature, issuer, and expiry, checked against the pool's JWKS. Callers check the
    rest: audience lives in `aud` on ID tokens but in `client_id` on access tokens."""
    issuer = (
        f"https://cognito-idp.{settings.aws_region}.amazonaws.com/{settings.cognito_user_pool_id}"
    )
    try:
        signing_key = jwks_client(
            settings.aws_region, settings.cognito_user_pool_id
        ).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "iss", "token_use"]},
        )
    except (jwt.PyJWTError, jwt.exceptions.PyJWKClientError) as exc:
        raise reject(type(exc).__name__) from exc


def user_claims(claims: dict, settings: Settings) -> dict:
    # An access token carries the same signature but not the identity claims we want.
    if claims.get("token_use") != "id":
        raise reject("wrong_token_use")
    if claims.get("aud") != settings.cognito_client_id:
        raise reject("wrong_audience")
    return claims


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """A signed-in user: a Cognito ID token issued to the web client."""
    return user_claims(verified_claims(credentials.credentials, settings), settings)


@lru_cache
def rds_data_client(region: str):
    return boto3.client("rds-data", region_name=region)


@lru_cache
def bedrock_client(region: str):
    return boto3.client("bedrock-runtime", region_name=region)


@lru_cache
def agentcore_client(region: str):
    # A turn with tool calls can outrun boto's 60 s read timeout. No retries: a retried
    # invocation runs the whole turn, and its tokens, a second time.
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(read_timeout=110, retries={"total_max_attempts": 1}),
    )


@lru_cache
def agent_runtime_arn(region: str, parameter: str) -> str:
    return boto3.client("ssm", region_name=region).get_parameter(Name=parameter)["Parameter"][
        "Value"
    ]


@lru_cache
def frontend_origin(region: str, parameter: str) -> str:
    return boto3.client("ssm", region_name=region).get_parameter(Name=parameter)["Parameter"][
        "Value"
    ]


# Aurora takes up to a minute to resume, and App Runner cuts a request off at 120 s.
WAKE_ATTEMPTS = 12
WAKE_DELAY_S = 5


def wake_database(settings: Settings) -> None:
    """Wait out an auto-paused cluster before the agent runs.

    Every call fails while the cluster resumes, so the agent's search tool would get a
    503 on a cold start and tell the user to try again. Waiting here costs only the
    first message after an idle spell.
    """
    rds = rds_data_client(settings.aws_region)
    for _ in range(WAKE_ATTEMPTS):
        try:
            rds.execute_statement(
                resourceArn=settings.database_cluster_arn,
                secretArn=settings.database_secret_arn,
                database=settings.database_name,
                sql="SELECT 1",
            )
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "DatabaseResumingException":
                # Anything else is for the agent's health tool to report, not a reason
                # to refuse the message.
                logger.warning(
                    "chat database check failed",
                    extra={"error_code": exc.response["Error"]["Code"]},
                )
                return
            time.sleep(WAKE_DELAY_S)


# The public demo has no sign-in, so these two limits are what stand between a visitor
# and the Bedrock bill. The daily cap bounds total spend; the per-address cap keeps one
# visitor from spending the whole day's allowance.
CHAT_PER_ADDRESS_PER_HOUR = 10
_chat_times: dict[str, deque] = defaultdict(deque)
_chat_times_lock = threading.Lock()

# The day the limit counts in, so it resets at midnight US Eastern.
EASTERN = ZoneInfo("America/New_York")
TODAY = "(now() AT TIME ZONE 'America/New_York')::date"
TOKENS_TODAY = f"SELECT coalesce(max(tokens), 0) AS tokens FROM daily_usage WHERE day = {TODAY}"
RECORD_TURN = f"""
INSERT INTO daily_usage (day, tokens, turns) VALUES ({TODAY}, :tokens, 1)
ON CONFLICT (day) DO UPDATE SET
    tokens = daily_usage.tokens + EXCLUDED.tokens,
    turns = daily_usage.turns + 1,
    updated_at = now()
"""

DAILY_LIMIT_MESSAGE = (
    "The demo has used today's chat allowance. It resets at midnight US Eastern; search "
    "still works in the meantime."
)
ADDRESS_LIMIT_MESSAGE = "That's the hourly chat limit for the demo. Try again in a little while."


def next_reset(now: datetime | None = None) -> datetime:
    """The next midnight US Eastern, when today's allowance starts over."""
    today = (now or datetime.now(EASTERN)).astimezone(EASTERN).date()
    return datetime.combine(today + timedelta(days=1), clock_time(), tzinfo=EASTERN)


def client_address(request: Request) -> str:
    """The caller's address. /chat is called on App Runner directly, whose front end
    appends the address it saw to X-Forwarded-For, so the last entry is the one a caller
    cannot set."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else "unknown"


def within_address_limit(address: str) -> bool:
    now = time.monotonic()
    with _chat_times_lock:
        times = _chat_times[address]
        while times and now - times[0] > 3600:
            times.popleft()
        if len(times) >= CHAT_PER_ADDRESS_PER_HOUR:
            return False
        times.append(now)
        return True


class Health(BaseModel):
    status: str


class Me(BaseModel):
    subject: str
    email: str | None = None


class Site(BaseModel):
    facility: str | None
    city: str | None
    state: str | None
    country: str | None
    status: str | None


class TrialHit(BaseModel):
    nct_id: str
    title: str
    overall_status: str
    phases: list[str]
    conditions: list[str]
    sponsor: str | None
    min_age_years: float | None
    max_age_years: float | None
    last_update_posted: str
    site_count: int
    sites: list[Site]
    # Read from the criteria by a model; None until the trial has been extracted.
    eligibility: Eligibility | None = None
    score: float


class PaperHit(BaseModel):
    pmid: str
    title: str
    journal: str | None
    pub_date: str | None
    pub_types: list[str]
    doi: str | None
    snippet: str | None
    nct_ids: list[str]
    score: float


class SearchResults(BaseModel):
    query: str
    trials: list[TrialHit]
    papers: list[PaperHit]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # Generated by the browser per conversation; a new one starts a new conversation.
    conversation_id: UUID


class Usage(BaseModel):
    """Today's chat allowance, for the page to show."""

    limit: int
    used: int
    remaining: int
    resets_at: datetime


def usage(used: int, settings: Settings) -> Usage:
    return Usage(
        limit=settings.daily_token_limit,
        used=used,
        remaining=max(settings.daily_token_limit - used, 0),
        resets_at=next_reset(),
    )


class ChatReply(BaseModel):
    answer: str
    used_tool: bool
    conversation_id: UUID
    usage: Usage


class DbHealth(BaseModel):
    status: str
    database: str
    detail: str | None = None


@app.get("/health")
def health() -> Health:
    return Health(status="ok")


@app.get("/health/db")
def health_db(settings: Annotated[Settings, Depends(get_settings)]) -> DbHealth:
    try:
        rds_data_client(settings.aws_region).execute_statement(
            resourceArn=settings.database_cluster_arn,
            secretArn=settings.database_secret_arn,
            database=settings.database_name,
            sql="SELECT 1",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        # The cluster auto-pauses at 0 ACU; the first call after idle wakes it and fails.
        status = "resuming" if code == "DatabaseResumingException" else "unavailable"
        logger.warning("db health check failed", extra={"error_code": code, "db_status": status})
        return JSONResponse(
            status_code=503,
            content=DbHealth(
                status=status, database=settings.database_name, detail=code
            ).model_dump(),
        )

    logger.info("db health check ok", extra={"database": settings.database_name})
    return DbHealth(status="ok", database=settings.database_name)


@app.get("/me")
def me(claims: Annotated[dict, Depends(current_user)]) -> Me:
    return Me(subject=claims["sub"], email=claims.get("email"))


@app.get("/search")
def search(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(min_length=2, max_length=500)],
    kind: Literal["all", "trials", "papers"] = "all",
    recruiting: bool = False,
    phase: Literal["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"] | None = None,
    age: Annotated[float | None, Query(ge=0, le=120)] = None,
    country: Annotated[str | None, Query(max_length=100)] = None,
    state: Annotated[str | None, Query(max_length=100)] = None,
    city: Annotated[str | None, Query(max_length=100)] = None,
    idh: Literal["wildtype", "mutant"] | None = None,
    mgmt: Literal["methylated", "unmethylated"] | None = None,
    setting: Literal["newly_diagnosed", "recurrent"] | None = None,
    recurrence: Annotated[int | None, Query(ge=1, le=10)] = None,
    prior_bevacizumab: bool | None = None,
    kps: Annotated[int | None, Query(ge=0, le=100)] = None,
    from_year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchResults:
    """Trials take the trial filters; papers take from_year. Each list is ranked on its own."""
    rds = rds_data_client(settings.aws_region)
    vector = hybrid.query_vector(bedrock_client(settings.aws_region), settings, q)
    try:
        trials = (
            hybrid.search_trials(
                rds,
                settings,
                q,
                vector,
                limit,
                recruiting=recruiting,
                phase=phase,
                age=age,
                country=country,
                state=state,
                city=city,
                idh=idh,
                mgmt=mgmt,
                setting=setting,
                recurrence=recurrence,
                prior_bevacizumab=prior_bevacizumab,
                kps=kps,
            )
            if kind in ("all", "trials")
            else []
        )
        papers = (
            hybrid.search_papers(rds, settings, q, vector, limit, from_year)
            if kind in ("all", "papers")
            else []
        )
    except rds.exceptions.DatabaseResumingException:
        # Same contract as /health/db: a paused cluster is a retryable 503, not an error.
        logger.info("search while database resuming")
        return JSONResponse(status_code=503, content={"status": "resuming"})

    logger.info(
        "search",
        extra={"kind": kind, "trial_hits": len(trials), "paper_hits": len(papers)},
    )
    return SearchResults(query=q, trials=trials, papers=papers)


@app.get("/usage")
def usage_today(settings: Annotated[Settings, Depends(get_settings)]) -> Usage:
    """Today's chat allowance. The page calls it when the chat tab opens."""
    wake_database(settings)
    try:
        used = hybrid.run(rds_data_client(settings.aws_region), settings, TOKENS_TODAY, {})[0][
            "tokens"
        ]
    except ClientError as exc:
        logger.warning("usage check failed", extra={"error_code": exc.response["Error"]["Code"]})
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "try again shortly") from exc
    return usage(used, settings)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def runtime_events(body) -> Iterator[dict]:
    """The runtime's server-sent events, each a JSON object on one data: line.

    Read a byte at a time: iter_lines' default 1024-byte chunks wait for a full kilobyte,
    so a turn's short progress lines arrived together with the answer. Measured against
    the deployed runtime on 2026-09-25: all three events at 6.3 s at 1024, the first at
    3.1 s at 1. A turn is a few kilobytes, so the extra reads cost nothing that matters.
    """
    for line in body.iter_lines(chunk_size=1):
        if line.startswith(b"data:"):
            yield json.loads(line[5:])


@app.post("/chat")
def chat(
    request: ChatRequest,
    http_request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    """One turn of a conversation with the navigator agent. Public: no sign-in.

    The limits are checked before anything streams, so they answer 429 as ordinary errors.
    The turn then streams as server-sent events: {"type": "progress", "text"} lines while
    the agent works, and a last event that is either {"type": "answer", ...ChatReply} or
    {"type": "error", "detail"}.

    The runtime keeps each session's history in its own microVM, so a conversation is a
    session, named by the random UUID the browser generated for it.
    """
    if not within_address_limit(client_address(http_request)):
        logger.info("chat address limit")
        raise HTTPException(http_status.HTTP_429_TOO_MANY_REQUESTS, ADDRESS_LIMIT_MESSAGE)
    wake_database(settings)
    rds = rds_data_client(settings.aws_region)
    try:
        used = hybrid.run(rds, settings, TOKENS_TODAY, {})[0]["tokens"]
    except ClientError as exc:
        logger.warning(
            "chat usage check failed", extra={"error_code": exc.response["Error"]["Code"]}
        )
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "try again shortly") from exc
    if used >= settings.daily_token_limit:
        logger.info("chat daily limit", extra={"tokens_today": used})
        raise HTTPException(http_status.HTTP_429_TOO_MANY_REQUESTS, DAILY_LIMIT_MESSAGE)

    session = f"public-{request.conversation_id}"
    try:
        response = agentcore_client(settings.aws_region).invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn(
                settings.aws_region, settings.agent_runtime_parameter
            ),
            runtimeSessionId=session,
            payload=json.dumps({"prompt": request.message}).encode(),
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        logger.warning("chat failed", extra={"error_code": code})
        raise HTTPException(http_status.HTTP_502_BAD_GATEWAY, "the agent is unavailable") from exc

    def stream() -> Iterator[str]:
        try:
            for event in runtime_events(response["response"]):
                if event.get("type") == "progress":
                    yield sse(event)
                    continue
                if event.get("type") != "reply":
                    # The agent's own error event, or the SDK's when the turn raised.
                    logger.warning(
                        "chat rejected by agent", extra={"agent_error": event.get("error")}
                    )
                    yield sse({"type": "error", "detail": "the agent could not answer"})
                    return
                tokens = event.get("tokens", 0)
                try:
                    hybrid.run(rds, settings, RECORD_TURN, {"tokens": tokens})
                except ClientError as exc:
                    # The answer is already paid for; losing the count is better than losing it.
                    logger.warning(
                        "chat usage record failed",
                        extra={"error_code": exc.response["Error"]["Code"]},
                    )
                # The message and answer are not logged: they can describe someone's diagnosis.
                logger.info("chat", extra={"used_tool": event["used_tool"], "tokens": tokens})
                reply = ChatReply(
                    answer=event["answer"],
                    used_tool=event["used_tool"],
                    conversation_id=request.conversation_id,
                    usage=usage(used + tokens, settings),
                )
                yield sse({"type": "answer", **reply.model_dump(mode="json")})
                return
            yield sse({"type": "error", "detail": "the agent stopped without answering"})
        except (ClientError, ValueError) as exc:
            logger.warning("chat stream failed", extra={"error": type(exc).__name__})
            yield sse({"type": "error", "detail": "the agent could not answer"})

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )

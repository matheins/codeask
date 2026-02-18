import logging
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from src.config import get_settings
from src.repo import clone_or_pull, start_periodic_sync
from src.agent import ask

log = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key")


def verify_api_key(key: str = Security(api_key_header)) -> str:
    if key != get_settings().api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


def _validate_startup():
    """Check required env vars and exit early with a clear message if missing."""
    settings = get_settings()
    missing = []
    if not settings.api_key:
        missing.append("API_KEY")
    if missing:
        print(f"[startup] ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_startup()

    result = clone_or_pull()
    print(f"[startup] {result}")

    start_periodic_sync()

    settings = get_settings()
    if settings.slack_bot_token and settings.slack_app_token:
        from src.slack_bot import start_in_background
        start_in_background()
        print("[startup] Slack bot started (Socket Mode)")
    else:
        print("[startup] Slack tokens not configured, bot disabled")

    yield


app = FastAPI(title="CodeAsk", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    files_consulted: list[str]


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(verify_api_key)])
async def ask_endpoint(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    result = await ask(req.question)
    return result


class SyncResponse(BaseModel):
    status: str


@app.post("/sync", response_model=SyncResponse, dependencies=[Depends(verify_api_key)])
async def sync_endpoint():
    try:
        result = clone_or_pull()
        return {"status": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}

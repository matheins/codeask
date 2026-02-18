import asyncio
import logging
import sys
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)

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
        log.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_startup()

    result = clone_or_pull()
    log.info(result)

    start_periodic_sync()

    settings = get_settings()

    # Initialize MCP if configured
    mcp_manager = None
    if settings.mcp_servers_config:
        from src.mcp_client import MCPManager

        mcp_manager = MCPManager()
        await mcp_manager.connect_all(settings.mcp_servers_config)
        log.info("MCP manager initialized")

    app.state.mcp_manager = mcp_manager

    if settings.slack_bot_token and settings.slack_app_token:
        from src.slack_bot import start_in_background

        loop = asyncio.get_running_loop()
        start_in_background(mcp_manager=mcp_manager, loop=loop)
        log.info("Slack bot started (Socket Mode)")
    else:
        log.info("Slack tokens not configured, bot disabled")

    yield

    # Shutdown MCP
    if mcp_manager:
        await mcp_manager.shutdown()
        log.info("MCP manager shut down")


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
    result = await ask(req.question, mcp_manager=app.state.mcp_manager)
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

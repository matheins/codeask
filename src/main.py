import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from src.config import get_settings
from src.conversation_manager import ConversationManager
from src.mcp_client import MCPManager  # used directly in lifespan
from src.repo import clone_or_pull, start_periodic_sync

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

    settings = get_settings()

    # Initialize MCP
    mcp_manager = MCPManager()
    await mcp_manager.connect_all(
        clone_dir=settings.clone_dir,
        extra_config_path=settings.mcp_servers_config,
        database_url=settings.database_url,
        db_max_rows=settings.db_max_rows,
        db_query_timeout=settings.db_query_timeout,
    )
    log.info(
        "MCP manager initialized (database: %s)",
        "enabled" if mcp_manager.has_database() else "disabled",
    )

    # Pre-compute repo overview for the agent
    await mcp_manager.compute_overview()

    # Start periodic sync (recomputes overview after each pull)
    loop = asyncio.get_running_loop()
    start_periodic_sync(mcp_manager=mcp_manager, loop=loop)

    # Initialize ConversationManager
    conversation_manager = ConversationManager(
        mcp_manager=mcp_manager,
        max_concurrency=settings.max_concurrency,
        conversation_ttl=settings.conversation_ttl,
        max_history_messages=settings.max_history_messages,
        response_cache_ttl=settings.response_cache_ttl,
    )
    app.state.mcp_manager = mcp_manager
    app.state.conversation_manager = conversation_manager

    if settings.slack_bot_token and settings.slack_app_token:
        from src.slack_bot import start_in_background

        start_in_background(conversation_manager=conversation_manager, loop=loop)
        log.info("Slack bot started (Socket Mode)")
    else:
        log.info("Slack tokens not configured, bot disabled")

    yield

    # Shutdown MCP
    await mcp_manager.shutdown()
    log.info("MCP manager shut down")


app = FastAPI(title="CodeAsk", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class AskResponse(BaseModel):
    answer: str


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(verify_api_key)])
async def ask_endpoint(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    result = await app.state.conversation_manager.ask(
        req.question, conversation_id=req.conversation_id,
    )
    return result


@app.post("/ask/stream", dependencies=[Depends(verify_api_key)])
async def ask_stream_endpoint(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_step(category: str):
        await queue.put({"type": "step", "label": category})

    async def on_text_chunk(text: str):
        await queue.put({"type": "text", "content": text})

    async def generate():
        async def _run():
            try:
                result = await app.state.conversation_manager.ask(
                    req.question,
                    conversation_id=req.conversation_id,
                    on_text_chunk=on_text_chunk,
                    on_step=on_step,
                )
                await queue.put(None)  # signal end of stream
                return result
            except Exception:
                await queue.put(None)
                raise

        task = asyncio.create_task(_run())

        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

        try:
            result = await task
            yield f"data: {json.dumps({'type': 'done', **result})}\n\n"
        except Exception as e:
            log.exception("Error during streaming ask")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


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

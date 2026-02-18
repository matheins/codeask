from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import TYPE_CHECKING

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.agent import ask
from src.config import get_settings

if TYPE_CHECKING:
    from src.mcp_client import MCPManager

log = logging.getLogger(__name__)


def start_in_background(
    *,
    mcp_manager: MCPManager | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> threading.Thread:
    """Start the Slack bot's Socket Mode handler in a background daemon thread."""
    settings = get_settings()
    app = App(token=settings.slack_bot_token)

    @app.event("app_mention")
    def handle_mention(event, client):
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        raw_text = event.get("text", "")

        # Strip the <@BOT_ID> mention prefix to get the actual question
        question = re.sub(r"<@[A-Z0-9]+>\s*", "", raw_text).strip()
        if not question:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Please include a question after mentioning me.",
            )
            return

        # Post a "thinking" placeholder
        thinking = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: Thinking...",
        )

        try:
            coro = ask(question, mcp_manager=mcp_manager)

            # If we have the main event loop, schedule the coroutine there
            # so MCP sessions (which live on that loop) are accessible.
            if loop is not None:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                result = future.result(timeout=120)
            else:
                result = asyncio.run(coro)

            text = result["answer"]

            # Slack has a 4000-char limit per message
            if len(text) > 3900:
                text = text[:3900] + "\n\n…(truncated)"

            client.chat_update(
                channel=channel,
                ts=thinking["ts"],
                text=text,
            )
        except Exception:
            log.exception("[slack] Error answering question")
            client.chat_update(
                channel=channel,
                ts=thinking["ts"],
                text=":x: Sorry, something went wrong while answering your question.",
            )

    handler = SocketModeHandler(app, settings.slack_app_token)

    def _run():
        log.info("[slack] Starting Socket Mode handler")
        handler.start()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t

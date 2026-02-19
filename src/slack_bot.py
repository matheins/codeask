from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import TYPE_CHECKING

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.config import get_settings

if TYPE_CHECKING:
    from src.conversation_manager import ConversationManager

log = logging.getLogger(__name__)

# Minimum interval between Slack message updates (seconds)
_UPDATE_THROTTLE = 1.5


def _markdown_to_slack(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn format."""
    # Remove markdown headers (## Heading -> *Heading*)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Bold: **text** -> *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Links: [text](url) -> <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Horizontal rules -> empty line
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    # Bullet points: - item -> • item
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)
    return text


def start_in_background(
    *,
    conversation_manager: ConversationManager,
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

        # Use thread_ts as conversation_id for follow-up context
        conversation_id = f"slack:{channel}:{thread_ts}"

        # Streaming buffer and throttle state
        buf = []
        last_update = [0.0]  # mutable for closure

        async def on_text_chunk(text: str):
            buf.append(text)
            now = time.monotonic()
            if now - last_update[0] < _UPDATE_THROTTLE:
                return
            last_update[0] = now
            partial = _markdown_to_slack("".join(buf))
            if len(partial) > 3900:
                partial = partial[:3900] + "\n\n…(streaming)"
            try:
                client.chat_update(
                    channel=channel,
                    ts=thinking["ts"],
                    text=partial,
                )
            except Exception:
                pass  # skip silently; final update will send the complete answer

        try:
            coro = conversation_manager.ask(
                question,
                conversation_id=conversation_id,
                on_text_chunk=on_text_chunk,
            )

            # Schedule the coroutine on the main event loop so MCP sessions
            # (which live on that loop) are accessible.
            if loop is not None:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                result = future.result(timeout=300)
            else:
                result = asyncio.run(coro)

            text = _markdown_to_slack(result["answer"])

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

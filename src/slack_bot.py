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

    # Resolve bot user ID so we can filter our own messages from thread replies
    try:
        bot_user_id = app.client.auth_test()["user_id"]
    except Exception:
        log.warning("[slack] Could not resolve bot user ID; thread context disabled")
        bot_user_id = None

    def _get_missed_thread_messages(client, channel, thread_ts, current_ts):
        """Return human messages posted after the last bot reply in the thread."""
        if not bot_user_id:
            return []
        try:
            result = client.conversations_replies(channel=channel, ts=thread_ts)
        except Exception:
            log.debug("Failed to fetch thread replies for context")
            return []

        replies = result.get("messages", [])

        # Find the last message from our bot
        last_bot_ts = None
        for msg in replies:
            if msg.get("user") == bot_user_id:
                last_bot_ts = msg["ts"]

        # Collect human messages after that point, excluding the current mention
        missed = []
        for msg in replies:
            if msg.get("user") == bot_user_id:
                continue
            if msg["ts"] == current_ts:
                continue
            if last_bot_ts and msg["ts"] <= last_bot_ts:
                continue
            text = re.sub(r"<@[A-Z0-9]+>\s*", "", msg.get("text", "")).strip()
            if text:
                missed.append(text)

        return missed

    @app.event("app_mention")
    def handle_mention(event, client):
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        raw_text = event.get("text", "")

        # Strip the <@BOT_ID> mention prefix to get the actual question
        question = re.sub(r"<@[A-Z0-9]+>\s*", "", raw_text).strip()
        is_in_thread = "thread_ts" in event

        # Prepend any thread messages the bot missed (sent without mentioning it)
        missed = _get_missed_thread_messages(client, channel, thread_ts, event["ts"])

        if not question and not missed:
            if is_in_thread:
                # No text and no missed messages — fetch full thread context
                # so the agent can see what was discussed.
                try:
                    result = client.conversations_replies(
                        channel=channel, ts=thread_ts,
                    )
                    thread_msgs = []
                    for msg in result.get("messages", []):
                        if bot_user_id and msg.get("user") == bot_user_id:
                            continue
                        if msg["ts"] == event["ts"]:
                            continue
                        text = re.sub(
                            r"<@[A-Z0-9]+>\s*", "", msg.get("text", ""),
                        ).strip()
                        if text:
                            thread_msgs.append(text)
                except Exception:
                    thread_msgs = []

                if thread_msgs:
                    context = "\n".join(f"- {m}" for m in thread_msgs)
                    question = (
                        f"[Thread context:]\n{context}"
                        f"\n\nThe user re-mentioned you without adding a new"
                        f" question. Please respond based on the thread"
                        f" context above."
                    )
                else:
                    question = (
                        "The user mentioned you in this thread without a"
                        " specific question. Please review the conversation"
                        " history and provide a helpful follow-up or ask if"
                        " they need clarification."
                    )
            else:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Please include a question after mentioning me.",
                )
                return

        if missed:
            context = "\n".join(f"- {m}" for m in missed)
            if question:
                question = (
                    f"[Messages in this thread since last response:]\n{context}"
                    f"\n\n[New question:]\n{question}"
                )
            else:
                question = (
                    f"[Messages in this thread since last response:]\n{context}"
                    f"\n\nThe user mentioned you without adding a question."
                    f" Please respond to the messages above."
                )

        # Post a "thinking" placeholder
        thinking = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: Thinking...",
        )

        # Use thread_ts as conversation_id for follow-up context
        conversation_id = f"slack:{channel}:{thread_ts}"

        # Emoji mapping for step categories
        step_emojis = {
            "Searching": ":mag:",
            "Reading": ":books:",
            "Analyzing": ":gear:",
            "Exploring": ":compass:",
        }

        # Streaming buffer and throttle state
        buf = []
        last_update = [0.0]  # mutable for closure
        step_count = [0]  # mutable for closure

        async def on_step(category: str):
            step_count[0] += 1
            emoji = step_emojis.get(category, ":compass:")
            step_text = f"{emoji} {category}..."
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: client.chat_update(
                        channel=channel,
                        ts=thinking["ts"],
                        text=step_text,
                    ),
                )
            except Exception:
                pass  # skip silently; final update will send the complete answer

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
                # Run sync Slack SDK call in a thread pool to avoid blocking
                # the event loop
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: client.chat_update(
                        channel=channel,
                        ts=thinking["ts"],
                        text=partial,
                    ),
                )
            except Exception:
                pass  # skip silently; final update will send the complete answer

        try:
            coro = conversation_manager.ask(
                question,
                conversation_id=conversation_id,
                on_text_chunk=on_text_chunk,
                on_step=on_step,
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

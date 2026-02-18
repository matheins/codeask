import asyncio
import logging
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.agent import ask
from src.config import get_settings

log = logging.getLogger(__name__)


def start_in_background() -> threading.Thread:
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
            result = asyncio.run(ask(question))
            answer = result["answer"]
            files = result.get("files_consulted", [])

            text = answer
            if files:
                file_list = "\n".join(f"- `{f}`" for f in files)
                text += f"\n\n*Files consulted:*\n{file_list}"

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

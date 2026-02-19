from __future__ import annotations

import asyncio
import logging
import time

from src.agent import ask, OnTextChunk, OnStep
from src.mcp_client import MCPManager

log = logging.getLogger(__name__)


class ConversationManager:
    """Channel-agnostic conversation history, concurrency control, and agent invocation."""

    def __init__(
        self,
        mcp_manager: MCPManager,
        max_concurrency: int = 2,
        conversation_ttl: int = 3600,
        max_history_messages: int = 20,
    ) -> None:
        self._mcp_manager = mcp_manager
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._conversation_ttl = conversation_ttl
        self._max_history_messages = max_history_messages
        # conversation_id -> message history
        self._histories: dict[str, list[dict]] = {}
        # conversation_id -> last access timestamp
        self._last_access: dict[str, float] = {}

    def _cleanup_expired(self) -> None:
        """Remove conversations that haven't been accessed within the TTL."""
        now = time.monotonic()
        expired = [
            cid for cid, ts in self._last_access.items()
            if now - ts > self._conversation_ttl
        ]
        for cid in expired:
            del self._histories[cid]
            del self._last_access[cid]
        if expired:
            log.info("Cleaned up %d expired conversations", len(expired))

    async def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        on_text_chunk: OnTextChunk | None = None,
        on_step: OnStep | None = None,
    ) -> dict:
        """Ask a question, optionally continuing an existing conversation.

        Args:
            question: The user's question.
            conversation_id: If provided, reuses message history for follow-ups.
            on_text_chunk: Optional async callback for streaming text deltas.
            on_step: Optional async callback for discovery step labels.

        Returns:
            {"answer": str}
        """
        # Lazy cleanup on every call
        self._cleanup_expired()

        # Build or retrieve message history
        if conversation_id and conversation_id in self._histories:
            messages = self._histories[conversation_id]
            messages.append({"role": "user", "content": question})
        else:
            messages = [{"role": "user", "content": question}]

        if conversation_id:
            self._histories[conversation_id] = messages
            self._last_access[conversation_id] = time.monotonic()

        # Truncate history to avoid exceeding context window.
        # Keep the first user message (for context) and the most recent messages.
        if len(messages) > self._max_history_messages:
            trimmed = len(messages) - self._max_history_messages
            trimmed += trimmed % 2  # round up to even to preserve assistant/user pairs
            messages[:] = messages[:1] + messages[1 + trimmed:]
            log.info("Trimmed %d old messages from conversation %s",
                     trimmed, conversation_id)

        log.info("Question (conversation=%s, history=%d msgs): %s",
                 conversation_id or "none", len(messages), question)

        async with self._semaphore:
            result = await ask(
                messages,
                mcp_manager=self._mcp_manager,
                on_text_chunk=on_text_chunk,
                on_step=on_step,
            )

        # Update last access after the agent run
        if conversation_id:
            self._last_access[conversation_id] = time.monotonic()

        return result

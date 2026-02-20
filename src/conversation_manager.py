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
        response_cache_ttl: int = 86400,
    ) -> None:
        self._mcp_manager = mcp_manager
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._conversation_ttl = conversation_ttl
        self._max_history_messages = max_history_messages
        self._response_cache_ttl = response_cache_ttl
        # conversation_id -> message history
        self._histories: dict[str, list[dict]] = {}
        # conversation_id -> last access timestamp
        self._last_access: dict[str, float] = {}
        # normalized question -> (answer, timestamp) for standalone questions
        self._response_cache: dict[str, tuple[str, float]] = {}

    def _cleanup_expired(self) -> None:
        """Remove conversations and cached responses that have exceeded their TTL."""
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

        expired_cache = [
            key for key, (_, ts) in self._response_cache.items()
            if now - ts > self._response_cache_ttl
        ]
        for key in expired_cache:
            del self._response_cache[key]
        if expired_cache:
            log.info("Cleaned up %d expired cache entries", len(expired_cache))

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
        is_followup = conversation_id and conversation_id in self._histories
        if is_followup:
            messages = self._histories[conversation_id]
            messages.append({"role": "user", "content": question})
        else:
            messages = [{"role": "user", "content": question}]

        if conversation_id:
            self._histories[conversation_id] = messages
            self._last_access[conversation_id] = time.monotonic()

        # Check response cache for standalone (non-follow-up) questions
        cache_key = question.strip().lower()
        if not is_followup and cache_key in self._response_cache:
            cached_answer, ts = self._response_cache[cache_key]
            if time.monotonic() - ts <= self._response_cache_ttl:
                log.info("Cache hit for question: %s", question)
                return {"answer": cached_answer}

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

        # Cache the response for standalone questions
        if not is_followup:
            self._response_cache[cache_key] = (result["answer"], time.monotonic())

        # Update last access after the agent run
        if conversation_id:
            self._last_access[conversation_id] = time.monotonic()

        return result

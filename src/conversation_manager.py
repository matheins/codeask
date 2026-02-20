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

    @staticmethod
    def _has_tool_use(message: dict) -> bool:
        """Check if an assistant message contains tool_use blocks."""
        content = message.get("content")
        if not isinstance(content, list):
            return False
        return any(
            (isinstance(b, dict) and b.get("type") == "tool_use")
            or (hasattr(b, "type") and b.type == "tool_use")
            for b in content
        )

    @staticmethod
    def _validate_history(messages: list[dict]) -> bool:
        """Check that messages strictly alternate user/assistant starting with user."""
        if not messages:
            return True
        if messages[0].get("role") != "user":
            return False
        for i in range(1, len(messages)):
            expected = "assistant" if i % 2 == 1 else "user"
            if messages[i].get("role") != expected:
                return False
        return True

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

        # Check response cache BEFORE touching conversation history.
        # This prevents storing incomplete histories for cache-hit questions.
        is_followup = conversation_id and conversation_id in self._histories
        cache_key = question.strip().lower()
        if not is_followup and cache_key in self._response_cache:
            cached_answer, ts = self._response_cache[cache_key]
            if time.monotonic() - ts <= self._response_cache_ttl:
                log.info("Cache hit for question: %s", question)
                return {"answer": cached_answer}

        # Build or retrieve message history
        if is_followup:
            messages = self._histories[conversation_id]
            messages.append({"role": "user", "content": question})
        else:
            messages = [{"role": "user", "content": question}]

        if conversation_id:
            self._histories[conversation_id] = messages
            self._last_access[conversation_id] = time.monotonic()

        # Validate history alternation before sending to the API.
        # If corrupted (e.g. from a prior error), reset to just the current question.
        if not self._validate_history(messages):
            log.warning("Corrupted history for conversation %s (%d msgs), resetting",
                        conversation_id, len(messages))
            messages.clear()
            messages.append({"role": "user", "content": question})

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
            try:
                result = await ask(
                    messages,
                    mcp_manager=self._mcp_manager,
                    on_text_chunk=on_text_chunk,
                    on_step=on_step,
                )
            except Exception:
                # The agent loop modifies messages in-place. If it crashes
                # mid-step the list may end with a user tool_results message
                # (no matching assistant response). Strip trailing user
                # messages so the next request doesn't send malformed history.
                while len(messages) > 1 and messages[-1].get("role") == "user":
                    messages.pop()
                # Also strip any trailing assistant message with tool_use blocks
                # whose tool_results were just popped — dangling tool_use blocks
                # would cause the API to reject the next request.
                if (
                    len(messages) > 1
                    and messages[-1].get("role") == "assistant"
                    and self._has_tool_use(messages[-1])
                ):
                    messages.pop()
                raise

        # Cache the response for standalone questions
        if not is_followup:
            self._response_cache[cache_key] = (result["answer"], time.monotonic())

        # Update last access after the agent run
        if conversation_id:
            self._last_access[conversation_id] = time.monotonic()

        return result

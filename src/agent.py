from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import anthropic

from src.config import get_settings
from src.mcp_client import MCPManager

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a product expert. Your job is to answer questions about a software \
product by exploring its codebase using the provided tools. \
Your audience is non-technical — explain things in plain language without \
jargon, code snippets, or implementation details. Focus on what the product \
does and how it works from a user perspective, not how it's built.

IMPORTANT: You have a LIMITED number of tool calls. Be efficient. \
Once you have read enough code to answer the question, STOP using tools and \
give your final answer immediately. Do not try to be exhaustive.

Rules:
- Ground your answer in actual code you have read.
- If you cannot find enough information, say so honestly.
- Keep your final answer concise — short paragraphs or bullet points.
- Your response MUST be under 3000 characters. Be direct.
- Do NOT include any preamble, intro sentences, or meta-commentary like \
"Let me explain" or "Based on my analysis". Jump straight into the answer.
- Do NOT use markdown headers (##), horizontal rules (---), or other rich formatting. \
Use plain text with simple bullet points (•) for lists.
- Do NOT reference file paths or line numbers unless the user specifically asked about code location.

Strategy — use these Serena code intelligence tools:
1. Start with list_dir (recursive=true) or find_file to locate relevant areas.
2. Use get_symbols_overview to understand what a file or module contains \
(classes, functions, types) — much richer than reading raw source.
3. Use find_symbol to locate definitions by name.
4. Use find_referencing_symbols to trace usage and call sites.
5. Use read_file or search_for_pattern only when you need the exact source.
6. Read 2-4 key files max, then synthesize your answer. Do NOT keep exploring \
once you have enough context.
"""

MAX_RETRIES = 5

# Type alias for the streaming text callback
OnTextChunk = Callable[[str], Awaitable[None]]

# Type alias for discovery step callback (receives a category label)
OnStep = Callable[[str], Awaitable[None]]

# Maps short tool names (after last __) to generic category labels
_TOOL_CATEGORIES: dict[str, str] = {
    "read_file": "Reading",
    "list_dir": "Searching",
    "find_file": "Searching",
    "search_for_pattern": "Searching",
    "get_symbols_overview": "Analyzing",
    "find_symbol": "Analyzing",
    "find_referencing_symbols": "Analyzing",
}


def _tool_category(namespaced_name: str) -> str:
    """Extract a safe, generic category label from a namespaced tool name."""
    short = namespaced_name.rsplit("__", 1)[-1]
    return _TOOL_CATEGORIES.get(short, "Exploring")


def _seconds_until_reset(rfc3339: str) -> float:
    """Parse an RFC 3339 reset timestamp into seconds to wait from now."""
    from datetime import datetime, timezone
    try:
        reset_at = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
        return max((reset_at - datetime.now(timezone.utc)).total_seconds(), 0)
    except (ValueError, TypeError):
        return 0


async def _check_rate_limits(headers) -> None:
    """Proactively sleep if rate limits are nearly exhausted."""
    max_wait = 0.0
    for kind in ("input-tokens", "requests"):
        remaining = headers.get(f"anthropic-ratelimit-{kind}-remaining")
        reset = headers.get(f"anthropic-ratelimit-{kind}-reset")
        limit = headers.get(f"anthropic-ratelimit-{kind}-limit")
        if remaining is not None and reset:
            rem = int(remaining)
            lim = int(limit) if limit else 1
            threshold = int(lim * 0.2) if "tokens" in kind else 0
            if rem <= threshold:
                wait = _seconds_until_reset(reset)
                if wait > max_wait:
                    max_wait = wait
                    log.info("Rate limit %s: %d/%d remaining, reset in %.1fs",
                             kind, rem, lim, wait)
    if max_wait > 0:
        await asyncio.sleep(max_wait)


async def _stream_with_retry(client, model, messages, *, tools, system):
    """Stream a response with rate-limit retry logic.

    Returns (message, text_chunks) where text_chunks is a list of text deltas
    collected during streaming.
    """
    for attempt in range(MAX_RETRIES):
        try:
            text_chunks: list[str] = []
            async with client.messages.stream(
                model=model,
                max_tokens=4096,
                system=system,
                tools=tools,
                messages=messages,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        text_chunks.append(event.delta.text)

                message = await stream.get_final_message()
                headers = dict(stream.response.headers)

            await _check_rate_limits(headers)
            return message, text_chunks

        except anthropic.RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            retry_after = getattr(e.response, "headers", {}).get("retry-after")
            wait = int(retry_after) if retry_after else 60
            log.warning("Rate limited (429), retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, MAX_RETRIES)
            await asyncio.sleep(wait)


async def ask(
    messages: list[dict],
    *,
    mcp_manager: MCPManager,
    on_text_chunk: OnTextChunk | None = None,
    on_step: OnStep | None = None,
) -> dict:
    """Run the agentic loop.

    Args:
        messages: Conversation history. The caller manages this list;
                  new assistant/tool messages are appended in-place.
        mcp_manager: MCP tool dispatch.
        on_text_chunk: Optional async callback invoked with each text delta
                       during streaming. Only fires for the final answer turn.
        on_step: Optional async callback invoked with a category label before
                 each tool call during discovery turns.

    Returns:
        {"answer": str, "files_consulted": list[str]}
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
    max_iterations = settings.max_iterations

    all_tools = mcp_manager.get_tool_schemas()
    system = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]

    files_consulted: set[str] = set()

    for step in range(max_iterations):
        remaining = max_iterations - step
        log.info("Step %d/%d", step + 1, max_iterations)
        response, text_chunks = await _stream_with_retry(
            client, settings.model, messages, tools=all_tools, system=system,
        )

        # If Claude is done (no more tool calls), flush buffered text and return
        if response.stop_reason == "end_turn":
            if on_text_chunk:
                for chunk in text_chunks:
                    await on_text_chunk(chunk)

            answer = "\n".join(
                block.text for block in response.content if block.type == "text"
            )
            log.info("Done in %d steps, %d files consulted, answer length: %d chars",
                     step + 1, len(files_consulted), len(answer))
            messages.append({"role": "assistant", "content": response.content})
            return {"answer": answer, "files_consulted": sorted(files_consulted)}

        # Process tool calls — emit step events before each tool execution
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                log.info("Tool call: %s(%s)", block.name, {k: v for k, v in block.input.items() if k != "content"})

                if on_step:
                    await on_step(_tool_category(block.name))

                result = await mcp_manager.call_tool(block.name, block.input)

                # Track files read via MCP
                if block.name.endswith("__read_file"):
                    path = block.input.get("path") or block.input.get("file_path")
                    if path:
                        files_consulted.add(path)

                log.info("Tool result: %s (%d chars)", block.name, len(result) if isinstance(result, str) else 0)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

        messages.append({"role": "assistant", "content": response.content})

        # Append warning to the last tool result when running low on steps
        if remaining <= 3 and tool_results:
            tool_results[-1]["content"] += (
                f"\n\n[SYSTEM: You have {remaining - 1} tool steps remaining. "
                "Give your final answer NOW based on what you have found so far.]"
            )

        messages.append({"role": "user", "content": tool_results})

    log.warning("Reached max iterations (%d) without final answer", max_iterations)
    return {
        "answer": "Reached the maximum number of exploration steps. Here is what I found so far.",
        "files_consulted": sorted(files_consulted),
    }

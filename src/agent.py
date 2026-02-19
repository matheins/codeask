from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

from src.config import get_settings
from src.tools import TOOL_SCHEMAS, get_tool_handlers

if TYPE_CHECKING:
    from src.mcp_client import MCPManager

log = logging.getLogger(__name__)

SYSTEM_PROMPT_BASE = """\
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
"""

BUILTIN_STRATEGY = """
Strategy:
1. Start with file_tree to get an overview (max_depth=2).
2. Use find_files to locate files by name pattern when you know what you're looking for \
(e.g. find_files with '**/*.controller.ts' or '**/auth*').
3. Use search_code to find specific symbols or patterns — narrow your search with \
file_glob and keep max_results low to reduce noise.
4. Read only the key files (2-4 max) that directly answer the question.
5. Synthesize your findings into a clear answer. Do NOT keep exploring once you \
have enough context.
"""

MCP_STRATEGY = """
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


def _seconds_until_reset(rfc3339: str) -> float:
    """Parse an RFC 3339 reset timestamp into seconds to wait from now."""
    from datetime import datetime, timezone
    try:
        reset_at = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
        return max((reset_at - datetime.now(timezone.utc)).total_seconds(), 0)
    except (ValueError, TypeError):
        return 0


async def _call_with_retry(client, model, messages, *, tools, system):
    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.with_raw_response.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=tools,
                messages=messages,
            )
            headers = response.headers
            message = response.parse()

            # Proactively wait if input tokens or requests are nearly exhausted
            # Headers: anthropic-ratelimit-{input-tokens,requests}-{remaining,reset,limit}
            max_wait = 0.0
            for kind in ("input-tokens", "requests"):
                remaining = headers.get(f"anthropic-ratelimit-{kind}-remaining")
                reset = headers.get(f"anthropic-ratelimit-{kind}-reset")
                limit = headers.get(f"anthropic-ratelimit-{kind}-limit")
                if remaining is not None and reset:
                    rem = int(remaining)
                    lim = int(limit) if limit else 1
                    # For tokens: wait if <20% remaining. For requests: wait if 0 remaining.
                    threshold = int(lim * 0.2) if "tokens" in kind else 0
                    if rem <= threshold:
                        wait = _seconds_until_reset(reset)
                        if wait > max_wait:
                            max_wait = wait
                            log.info("Rate limit %s: %d/%d remaining, reset in %.1fs",
                                     kind, rem, lim, wait)

            if max_wait > 0:
                await asyncio.sleep(max_wait)

            return message
        except anthropic.RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            retry_after = getattr(e.response, "headers", {}).get("retry-after")
            wait = int(retry_after) if retry_after else 60
            log.warning("Rate limited (429), retrying in %ds (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
            await asyncio.sleep(wait)


async def ask(question: str, *, mcp_manager: MCPManager | None = None) -> dict:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
    max_iterations = settings.max_iterations
    base_dir = Path(settings.clone_dir).resolve()
    tool_handlers = get_tool_handlers(base_dir)

    # Build tool list: use MCP tools when available, otherwise fall back to built-ins
    mcp_tools = mcp_manager.get_tool_schemas() if mcp_manager else []
    all_tools = mcp_tools if mcp_tools else list(TOOL_SCHEMAS)

    # Build system prompt with the right strategy for available tools
    system = SYSTEM_PROMPT_BASE + (MCP_STRATEGY if mcp_tools else BUILTIN_STRATEGY)

    log.info("Question: %s", question)
    messages = [{"role": "user", "content": question}]
    files_consulted: set[str] = set()

    for step in range(max_iterations):
        remaining = max_iterations - step
        log.info("Step %d/%d", step + 1, max_iterations)
        response = await _call_with_retry(
            client, settings.model, messages, tools=all_tools, system=system
        )

        # If Claude is done (no more tool calls), extract the final answer
        if response.stop_reason == "end_turn":
            answer = "\n".join(
                block.text for block in response.content if block.type == "text"
            )
            log.info("Done in %d steps, %d files consulted, answer length: %d chars",
                     step + 1, len(files_consulted), len(answer))
            return {"answer": answer, "files_consulted": sorted(files_consulted)}

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                log.info("Tool call: %s(%s)", block.name, {k: v for k, v in block.input.items() if k != "content"})

                # Route to MCP or built-in handler
                if mcp_manager and mcp_manager.is_mcp_tool(block.name):
                    result = await mcp_manager.call_tool(block.name, block.input)
                else:
                    handler = tool_handlers.get(block.name)
                    if handler:
                        result = handler(block.input)
                        if block.name == "read_file":
                            files_consulted.add(block.input["path"])
                    else:
                        result = f"Unknown tool: {block.name}"
                        log.warning("Unknown tool: %s", block.name)

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

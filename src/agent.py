from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import anthropic

from src.config import get_settings
from src.tools import TOOL_SCHEMAS, TOOL_HANDLERS

if TYPE_CHECKING:
    from src.mcp_client import MCPManager

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a product code oracle. Your job is to answer questions about a software \
product by exploring its codebase using the provided tools.

Strategy:
1. Start with file_tree to get an overview (max_depth=2).
2. Use find_files to locate files by name pattern when you know what you're looking for \
(e.g. find_files with '**/*.controller.ts' or '**/auth*').
3. Use search_code to find specific symbols or patterns — narrow your search with \
file_glob and keep max_results low to reduce noise.
4. Read only the key files (2-4 max) that directly answer the question.
5. Synthesize your findings into a clear answer. Do NOT keep exploring once you \
have enough context.

IMPORTANT: You have a LIMITED number of tool calls. Be efficient. \
Once you have read enough code to answer the question, STOP using tools and \
give your final answer immediately. Do not try to be exhaustive.

Rules:
- Ground your answer in actual code you have read.
- Reference specific file paths and line numbers when relevant.
- If you cannot find enough information, say so honestly.
- Keep your final answer concise — aim for SHORT paragraphs or bullet points.
- Your response MUST be under 3000 characters. Be direct and avoid lengthy explanations.
"""

MCP_ADDENDUM = """
Additional tools from external code intelligence servers are available. \
Prefer semantic tools (symbol lookup, find references, go to definition) over grep \
for precision queries like "what calls X?" or "where is Y defined?". \
These tools understand code structure and give more accurate results than text search.
"""

MAX_RETRIES = 5


async def _call_with_retry(client, model, messages, *, tools, system):
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=tools,
                messages=messages,
            )
        except anthropic.RateLimitError:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 15 * (attempt + 1)
            print(f"[agent] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
            await asyncio.sleep(wait)


async def ask(question: str, *, mcp_manager: MCPManager | None = None) -> dict:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    max_iterations = settings.max_iterations

    # Build tool list: built-in + MCP (if available)
    all_tools = list(TOOL_SCHEMAS)
    if mcp_manager:
        all_tools.extend(mcp_manager.get_tool_schemas())

    # Build system prompt
    system = SYSTEM_PROMPT
    if mcp_manager and mcp_manager.get_tool_schemas():
        system += MCP_ADDENDUM

    messages = [{"role": "user", "content": question}]
    files_consulted: set[str] = set()

    for step in range(max_iterations):
        remaining = max_iterations - step
        print(f"[agent] Step {step + 1}/{max_iterations}")
        response = await _call_with_retry(
            client, settings.model, messages, tools=all_tools, system=system
        )

        # If Claude is done (no more tool calls), extract the final answer
        if response.stop_reason == "end_turn":
            answer = "\n".join(
                block.text for block in response.content if block.type == "text"
            )
            return {"answer": answer, "files_consulted": sorted(files_consulted)}

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Route to MCP or built-in handler
                if mcp_manager and mcp_manager.is_mcp_tool(block.name):
                    result = await mcp_manager.call_tool(block.name, block.input)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    if handler:
                        result = handler(block.input)
                        if block.name == "read_file":
                            files_consulted.add(block.input["path"])
                    else:
                        result = f"Unknown tool: {block.name}"
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

    return {
        "answer": "Reached the maximum number of exploration steps. Here is what I found so far.",
        "files_consulted": sorted(files_consulted),
    }

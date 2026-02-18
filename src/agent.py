import asyncio
import logging

import anthropic

from src.config import get_settings
from src.tools import TOOL_SCHEMAS, TOOL_HANDLERS

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a product code oracle. Your job is to answer questions about a software \
product by exploring its codebase using the provided tools.

Strategy:
1. Start with file_tree to get an overview (max_depth=2).
2. Use search_code to find the most relevant files — don't read everything.
3. Read only the key files (2-4 max) that directly answer the question.
4. Synthesize your findings into a clear answer. Do NOT keep exploring once you \
have enough context.

IMPORTANT: You have a LIMITED number of tool calls. Be efficient. \
Once you have read enough code to answer the question, STOP using tools and \
give your final answer immediately. Do not try to be exhaustive.

Rules:
- Ground your answer in actual code you have read.
- Reference specific file paths and line numbers when relevant.
- If you cannot find enough information, say so honestly.
- Keep your final answer concise but thorough.
"""

MAX_ITERATIONS = 10
MAX_RETRIES = 5


async def _call_with_retry(client, model, messages):
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 15 * (attempt + 1)
            print(f"[agent] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
            await asyncio.sleep(wait)


async def ask(question: str) -> dict:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = [{"role": "user", "content": question}]
    files_consulted: set[str] = set()

    for step in range(MAX_ITERATIONS):
        remaining = MAX_ITERATIONS - step
        print(f"[agent] Step {step + 1}/{MAX_ITERATIONS}")
        response = await _call_with_retry(client, settings.model, messages)

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

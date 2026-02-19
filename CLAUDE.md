# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CodeAsk — a FastAPI service that answers product questions about a GitHub repository using an Anthropic Claude agentic loop with Serena MCP code-intelligence tools. Exposes an HTTP API (`POST /ask`) and an optional Slack bot (Socket Mode).

## Commands

```bash
# Install
pip install .

# Run locally
uvicorn src.main:app --reload --port 8000

# Docker
docker build -t codeask .
docker run --env-file .env -p 8000:8000 codeask

# Verify Python files compile (no test suite exists)
python3 -m py_compile src/main.py src/agent.py src/mcp_client.py src/slack_bot.py src/config.py src/repo.py
```

## Architecture

### Request Flow

HTTP `POST /ask` or Slack `@mention` → `agent.ask()` → agentic loop (up to `max_iterations` rounds) → MCP tool calls via `MCPManager` → Serena code intelligence tools → plain-text answer returned.

### Key Files

| File | Role |
|---|---|
| `src/main.py` | FastAPI app, lifespan (startup/shutdown), HTTP endpoints, API key auth |
| `src/agent.py` | Anthropic agentic loop, system prompt, rate-limit retry with raw response headers |
| `src/mcp_client.py` | MCP server lifecycle, tool namespacing, built-in Serena + optional extras |
| `src/slack_bot.py` | Slack Socket Mode bot, `app_mention` handler, thread context fetching |
| `src/config.py` | `pydantic-settings` BaseSettings, accessed via `@lru_cache get_settings()` |
| `src/repo.py` | Git clone/pull, periodic background sync with `repo_lock` threading.Lock |

### Startup Sequence (lifespan in main.py)

1. Validate required env vars (`API_KEY`)
2. Clone or pull the target GitHub repo into `CLONE_DIR`
3. Start periodic sync daemon thread (`SYNC_INTERVAL` seconds)
4. Connect Serena MCP server (built-in, fatal on failure) + any extra MCP servers from `MCP_SERVERS_CONFIG`
5. Optionally start Slack bot if both Slack tokens are set

### MCP Tool Namespacing

Tools are namespaced as `mcp__{server_name}__{tool_name}` (e.g. `mcp__serena__read_file`). The `_tool_map` dict maps these back to `(server_name, original_name)` for dispatch. Serena meta/setup tools listed in `_HIDDEN_TOOLS` are filtered out of schemas sent to the agent.

### Serena Is Built-In

Serena's MCP config is hardcoded in `mcp_client.py` — it launches via `uvx --from git+https://github.com/oraios/serena`. `MCP_SERVERS_CONFIG` is optional and only for *additional* MCP servers. Extra server failures are non-fatal (logged and skipped); Serena failure is fatal.

### Extended Thinking (`agent.py`)

Enabled by default. When `ENABLE_THINKING=true` (the default), the agentic loop passes `thinking={"type": "enabled", "budget_tokens": N}` to the Anthropic API. `max_tokens` is set to `thinking_budget + 4096` to cover both thinking and output. Thinking blocks are preserved in conversation history but excluded from the final answer (filtered by `block.type == "text"`).

### Rate-Limit Retry (`agent.py`)

Uses `client.messages.with_raw_response.create()` to read `anthropic-ratelimit-*` headers. Proactively sleeps when input tokens remaining < 20% or requests remaining hits 0. Retries 429s up to 5 times using `retry-after` header.

### Slack Bot Threading

The Slack bot runs in a daemon thread but schedules `ask()` coroutines on the main asyncio event loop via `run_coroutine_threadsafe` so MCP sessions (which live on that loop) are accessible.

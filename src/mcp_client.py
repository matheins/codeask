from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

# Serena meta/setup tools that should not be exposed to the agent
_HIDDEN_TOOLS = {
    "initial_instructions",
    "onboarding",
    "check_onboarding_performed",
    "prepare_for_new_conversation",
    "switch_modes",
    "get_current_config",
    "activate_project",
    "write_memory",
    "read_memory",
    "list_memories",
    "delete_memory",
    "edit_memory",
}

_SERENA_SERVER_NAME = "serena"
_SERENA_COMMAND = "uvx"
_SERENA_ARGS_PREFIX = [
    "--from", "git+https://github.com/oraios/serena",
    "serena", "start-mcp-server",
    "--project",
]


class MCPManager:
    """Manages connections to one or more MCP servers."""

    def __init__(self) -> None:
        self._exit_stack = AsyncExitStack()
        # server_name -> ClientSession
        self._sessions: dict[str, ClientSession] = {}
        # namespaced tool name -> (server_name, original_tool_name)
        self._tool_map: dict[str, tuple[str, str]] = {}
        # cached Anthropic-format tool schemas
        self._tool_schemas: list[dict] = []

    async def connect_all(
        self, clone_dir: str, extra_config_path: str | None = None
    ) -> None:
        """Connect to built-in Serena server and any extra MCP servers."""
        await self._exit_stack.__aenter__()

        # 1. Built-in Serena — failure is fatal
        serena_cfg = {
            "command": _SERENA_COMMAND,
            "args": _SERENA_ARGS_PREFIX + [str(Path(clone_dir).resolve())],
        }
        await self._connect_server(_SERENA_SERVER_NAME, serena_cfg)

        # 2. Optional extra servers from config file
        if extra_config_path:
            path = Path(extra_config_path)
            if path.is_file():
                config = json.loads(path.read_text())
                servers = config.get("mcpServers", {})
                for server_name, server_cfg in servers.items():
                    try:
                        await self._connect_server(server_name, server_cfg)
                    except Exception:
                        log.exception(
                            "[mcp] Failed to connect to extra server '%s'",
                            server_name,
                        )
            else:
                log.warning(
                    "[mcp] Extra config file not found: %s", extra_config_path
                )

    async def _connect_server(self, name: str, cfg: dict) -> None:
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
        )

        devnull = open(os.devnull, "w")
        self._exit_stack.callback(devnull.close)
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        read_stream, write_stream = stdio_transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions[name] = session

        # Discover tools and build schemas (skip meta/setup tools)
        result = await session.list_tools()
        exposed = 0
        for tool in result.tools:
            namespaced = f"mcp__{name}__{tool.name}"
            self._tool_map[namespaced] = (name, tool.name)

            if tool.name in _HIDDEN_TOOLS:
                continue

            schema = {
                "name": namespaced,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            self._tool_schemas.append(schema)
            exposed += 1

        log.info(
            "[mcp] Connected to '%s': %d tools discovered, %d exposed to agent",
            name,
            len(result.tools),
            exposed,
        )

        # Mark last tool schema for prompt caching (reduces cost on follow-up turns)
        if self._tool_schemas:
            self._tool_schemas[-1]["cache_control"] = {"type": "ephemeral"}

        # Run onboarding if the server supports it (e.g. Serena)
        tool_names = {t.name for t in result.tools}
        if "onboarding" in tool_names:
            try:
                log.info("[mcp] Running onboarding for '%s'...", name)
                await session.call_tool("onboarding", {})
                log.info("[mcp] Onboarding completed for '%s'", name)
            except Exception:
                log.exception("[mcp] Onboarding failed for '%s'", name)

    def get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_map

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Dispatch a tool call to the correct MCP server session."""
        server_name, original_name = self._tool_map[name]
        session = self._sessions[server_name]
        try:
            result = await session.call_tool(original_name, arguments)
            # Combine all text content from the result
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(empty response)"
        except Exception as e:
            log.exception("[mcp] Error calling tool '%s' on server '%s'", original_name, server_name)
            return f"MCP tool error: {e}"

    async def shutdown(self) -> None:
        """Close all MCP server connections."""
        try:
            await self._exit_stack.aclose()
        except Exception:
            log.exception("[mcp] Error during shutdown")
        self._sessions.clear()
        self._tool_map.clear()
        self._tool_schemas.clear()

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)


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

    async def connect_all(self, config_path: str) -> None:
        """Read the config file and connect to all defined MCP servers."""
        path = Path(config_path)
        if not path.is_file():
            log.warning("[mcp] Config file not found: %s", config_path)
            return

        config = json.loads(path.read_text())
        servers = config.get("mcpServers", {})
        if not servers:
            log.warning("[mcp] No servers defined in %s", config_path)
            return

        await self._exit_stack.__aenter__()

        for server_name, server_cfg in servers.items():
            try:
                await self._connect_server(server_name, server_cfg)
            except Exception:
                log.exception("[mcp] Failed to connect to server '%s'", server_name)

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

        # Discover tools and build schemas
        result = await session.list_tools()
        for tool in result.tools:
            namespaced = f"mcp__{name}__{tool.name}"
            self._tool_map[namespaced] = (name, tool.name)

            schema = {
                "name": namespaced,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            self._tool_schemas.append(schema)

        log.info(
            "[mcp] Connected to '%s': %d tools discovered",
            name,
            len(result.tools),
        )

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

"""ToolClient (LLD 3.10): the agent's only path to the FastMCP Google tools.

Wraps `fastmcp.Client`. The target is the FastMCP server script (stdio), an HTTP URL
(`http://mcp:8001/mcp`), the string "inprocess" (server built in this process, still spoken to
over the MCP protocol), or a FastMCP object (tests). `fastmcp` is imported lazily.
"""

import asyncio
import json
import logging
import os
from typing import Any

from advisor_agent.domain.plan import ALL_TOOLS

log = logging.getLogger(__name__)


class ToolCallError(Exception):
    def __init__(self, tool: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.retryable = retryable


class UnexpectedToolError(RuntimeError):
    """The MCP server exposes a tool outside the allow-list (e.g. anything that sends email)."""


def _target_from_settings(settings: Any) -> Any:
    target: str = settings.mcp_server_target
    if target == "inprocess":
        from advisor_agent.mcp_server.server import build_server_from_settings

        return build_server_from_settings(settings)
    if target.endswith(".py"):
        from fastmcp.client.transports import PythonStdioTransport

        # Pass the environment explicitly: the MCP stdio client otherwise forwards only a
        # minimal safe set of variables and the server would miss its AGENT_GOOGLE_* settings.
        return PythonStdioTransport(target, env=dict(os.environ))
    return target


def _result_data(res: Any) -> dict[str, Any]:
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict):
        inner = structured.get("result")
        return inner if isinstance(inner, dict) and len(structured) == 1 else structured
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def _input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None)  # MCP SDK v2 name
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return dict(schema or {"type": "object", "properties": {}})


class ToolClient:
    def __init__(self, target: Any, *, call_timeout_s: float = 10.0) -> None:
        from fastmcp import Client

        self._client = Client(target)
        self._timeout_s = call_timeout_s
        self._declarations: list[dict[str, Any]] | None = None
        self._opened = False

    @classmethod
    def from_settings(cls, settings: Any) -> "ToolClient":
        return cls(_target_from_settings(settings), call_timeout_s=settings.mcp_call_timeout_s)

    async def open(self) -> None:
        """Keep one MCP session open for the app's lifetime (optional; calls also work without)."""
        if not self._opened:
            await self._client.__aenter__()
            self._opened = True

    async def aclose(self) -> None:
        if self._opened:
            self._opened = False
            await self._client.__aexit__(None, None, None)

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._client:
            tools = await self._client.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "parameters": _input_schema(t)}
            for t in tools
        ]

    async def declarations(self) -> list[dict[str, Any]]:
        """Tool discovery (cached). Fails if the server exposes anything off the allow-list."""
        if self._declarations is None:
            decls = await self.list_tools()
            unexpected = sorted(
                d["name"] for d in decls if d["name"] not in ALL_TOOLS or "send" in d["name"]
            )
            if unexpected:
                raise UnexpectedToolError(f"MCP server exposes unexpected tools: {unexpected}")
            self._declarations = decls
        return self._declarations

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        try:
            async with self._client:
                res = await asyncio.wait_for(
                    self._client.call_tool(tool, args), timeout=self._timeout_s
                )
        except ToolError as e:
            msg = str(e)
            raise ToolCallError(tool, msg, retryable="retryable:" in msg) from e
        except TimeoutError as e:
            raise ToolCallError(tool, "timeout", retryable=True) from e
        except Exception as e:  # transport / connection problems
            raise ToolCallError(tool, f"{type(e).__name__}: {e}", retryable=True) from e
        if getattr(res, "is_error", False):
            text = " ".join(getattr(b, "text", "") or "" for b in res.content or [])
            raise ToolCallError(tool, text, retryable="retryable:" in text)
        return _result_data(res)

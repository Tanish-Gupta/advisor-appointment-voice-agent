"""Gemini tool agent (LLD 3.10): Gemini *proposes* MCP tool calls; it never executes them.

Every proposal goes through the deterministic ToolGate (mcp_client/tool_gate.py) and only then
through the ToolClient. Automatic function calling is disabled, the function-calling mode is
ANY with `allowed_function_names`, and the model only sees PII-free FACTS.

Like GeminiNLU, the agent takes an injectable generate function, so CI fakes the *LLM* without
faking MCP. `google-genai` is imported lazily.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TOOL_SYSTEM = """\
You operate an advisor scheduling back office through tools.
Call ONLY the tools listed, and call each needed tool exactly once.
Use EXACTLY the values in FACTS (copy strings verbatim, including IST offsets); never invent \
codes, topics, times, statuses or recipients.
Never include personal data. Never give financial advice. Do not write prose."""

# (system, facts_json, declarations, allowed_names) -> [(tool_name, args), ...]
ToolGenerateFn = Callable[
    [str, str, list[dict[str, Any]], list[str]], Awaitable[list[tuple[str, dict[str, Any]]]]
]
DeclarationsFn = Callable[[], Awaitable[list[dict[str, Any]]]]


class ToolAgentUnavailable(Exception):
    """Gemini could not be reached or returned nothing usable; the caller uses the fallback."""


@dataclass(frozen=True)
class ProposedCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentStep:
    """One planning step: which tools may be used, the facts, and the calls the plan expects."""

    allowed_tools: tuple[str, ...]
    facts: dict[str, Any]
    expected: dict[str, dict[str, Any]]

    def facts_json(self) -> str:
        return json.dumps({"FACTS": self.facts}, ensure_ascii=False, sort_keys=True)

    def fallback_calls(self) -> list[ProposedCall]:
        return [ProposedCall(t, dict(a)) for t, a in self.expected.items()]


class GeminiToolAgent:
    def __init__(
        self,
        generate: ToolGenerateFn,
        declarations: DeclarationsFn,
        *,
        timeout_s: float = 6.0,
    ) -> None:
        self._generate = generate
        self._declarations = declarations
        self._timeout_s = timeout_s

    async def propose(self, step: AgentStep) -> list[ProposedCall]:
        try:
            decls = [d for d in await self._declarations() if d["name"] in step.allowed_tools]
            raw = await asyncio.wait_for(
                self._generate(TOOL_SYSTEM, step.facts_json(), decls, list(step.allowed_tools)),
                self._timeout_s,
            )
        except Exception as e:  # timeout, network, quota, SDK errors
            log.warning("gemini tool agent unavailable: %s", type(e).__name__)
            raise ToolAgentUnavailable(type(e).__name__) from e
        calls = [ProposedCall(str(name), dict(args or {})) for name, args in raw or []]
        if not calls:
            raise ToolAgentUnavailable("no_function_calls")
        return calls


def google_tool_generate(api_key: str, model: str) -> ToolGenerateFn:
    """ToolGenerateFn backed by the Google Gen AI SDK (raises ImportError if it is missing)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    async def generate(
        system: str, facts_json: str, decls: list[dict[str, Any]], allowed: list[str]
    ) -> list[tuple[str, dict[str, Any]]]:
        functions = [
            types.FunctionDeclaration(
                name=d["name"],
                description=d.get("description") or "",
                parameters_json_schema=d.get("parameters") or {"type": "object", "properties": {}},
            )
            for d in decls
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=functions)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=allowed
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.0,
        )
        resp = await client.aio.models.generate_content(
            model=model, contents=facts_json, config=config
        )
        return [(fc.name or "", dict(fc.args or {})) for fc in (resp.function_calls or [])]

    return generate

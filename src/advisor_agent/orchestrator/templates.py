"""Template rendering (LLD 2.9): whitelisted params only, no free-form LLM text."""

from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from advisor_agent.domain.timeutil import fmt_preference, fmt_slot
from advisor_agent.orchestrator.context import SessionContext

ALLOWED_PARAMS = frozenset(
    {"topic", "slot", "slot1", "slot2", "pref_label", "code", "secure_url", "ttl", "edu_links",
     "reason", "old_slot", "guide", "windows", "asked_day", "requested",
     "suggested"}
)  # fmt: skip


class _Strict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"template param {key!r} missing")


@lru_cache
def load_templates() -> dict[str, str]:
    raw = resources.files("advisor_agent.orchestrator").joinpath("templates.yaml").read_text()
    data = yaml.safe_load(raw)
    return {str(k): str(v) for k, v in data.items()}


def base_params(ctx: SessionContext) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if ctx.topic is not None:
        params["topic"] = ctx.topic.value
    if ctx.chosen_slot is not None:
        params["slot"] = fmt_slot(ctx.chosen_slot)
    for i, s in enumerate(ctx.offered_slots[:2], start=1):
        params[f"slot{i}"] = fmt_slot(s)
    if ctx.current_slot is not None:
        params["old_slot"] = fmt_slot(ctx.current_slot)
    if ctx.preference is not None:
        params["pref_label"] = fmt_preference(ctx.preference)
    if ctx.suggested_pref is not None:
        params["suggested"] = fmt_preference(ctx.suggested_pref)
    if ctx.booking_code:
        params["code"] = ctx.booking_code
    if ctx.secure_url:
        params["secure_url"] = ctx.secure_url
    return params


def render(template_ids: list[str], ctx: SessionContext, extra: dict[str, Any] | None = None
           ) -> list[str]:  # fmt: skip
    extra = extra or {}
    unknown = set(extra) - ALLOWED_PARAMS
    if unknown:
        raise ValueError(f"non-whitelisted template params: {sorted(unknown)}")
    params = _Strict({**base_params(ctx), **extra})
    templates = load_templates()
    return [templates[tid].format_map(params) for tid in template_ids]

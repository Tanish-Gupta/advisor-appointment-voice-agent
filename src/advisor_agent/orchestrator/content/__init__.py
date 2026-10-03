"""Static, reviewed content (LLD 5.2, 6.3): investor-education links and prep checklists.

Loaded from package YAML so the LLM can never invent either.
"""

from functools import lru_cache
from importlib import resources

import yaml

from advisor_agent.domain.models import Topic


def _load(name: str) -> object:
    raw = resources.files("advisor_agent.orchestrator.content").joinpath(name).read_text()
    return yaml.safe_load(raw)


@lru_cache
def prep_guides() -> dict[Topic, str]:
    data = _load("prep_guides.yaml")
    assert isinstance(data, dict)
    guides = {Topic(str(k)): str(v).strip() for k, v in data.items()}
    missing = set(Topic) - set(guides)
    if missing:
        raise ValueError(f"prep_guides.yaml is missing topics: {sorted(missing)}")
    return guides


@lru_cache
def edu_links() -> tuple[tuple[str, str], ...]:
    data = _load("edu_links.yaml")
    assert isinstance(data, list)
    return tuple((str(item["name"]), str(item["url"])) for item in data)


def edu_links_text() -> str:
    return "; ".join(f"{name}: {url}" for name, url in edu_links())

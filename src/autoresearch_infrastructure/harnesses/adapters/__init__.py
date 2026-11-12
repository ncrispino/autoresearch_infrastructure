"""One explicit registry of trusted, implemented solver adapters."""

from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

ADAPTERS = {adapter.name(): adapter for adapter in (CodexAdapter, ClaudeCodeAdapter)}


def get_adapter(name: str) -> type[HarnessAdapter]:
    try:
        return ADAPTERS[name]
    except (KeyError, TypeError):
        raise ValueError(f"Unsupported harness {name!r}; choose from {', '.join(ADAPTERS)}") from None

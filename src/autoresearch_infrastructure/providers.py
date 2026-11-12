"""Model provider endpoints for the harness solver CLIs."""

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """A provider's endpoint and the name of the environment variable that holds its key."""

    name: str
    base_url: str
    key_variable: str

    def key(self) -> str:
        key = os.environ.get(self.key_variable)
        if not key:
            raise RuntimeError(
                f"Add {self.key_variable} to the project .env or process environment before model calls"
            )
        return key

    def model(self, model: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", model):
            raise ValueError("Invalid model identifier")
        if self.name == "openrouter" and "/" not in model:
            if model.startswith("gpt-"):
                return "openai/" + model
            claude = re.fullmatch(r"(claude-(?:haiku|sonnet|opus)-[0-9]+)-([0-9]+)", model)
            if claude:
                return f"anthropic/{claude.group(1)}.{claude.group(2)}"
            raise ValueError("OpenRouter models require a publisher/model identifier")
        return model

    def metadata(self) -> dict:
        return {"provider": self.name, "base_url": self.base_url}


def provider_for(harness: str, name: str) -> Provider:
    """The provider settings for a harness, or an error when that pair is unsupported."""
    from .harnesses.adapters import get_adapter

    get_adapter(harness)
    endpoints = {
        ("codex", "openai"): ("https://api.openai.com/v1", "OPENAI_API_KEY"),
        ("codex", "openrouter"): ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        ("claude-code", "anthropic"): ("https://api.anthropic.com", "ANTHROPIC_API_KEY"),
        ("claude-code", "openrouter"): ("https://openrouter.ai/api", "OPENROUTER_API_KEY"),
    }
    try:
        url, variable = endpoints[(harness, name)]
    except (KeyError, TypeError):
        raise ValueError(f"Unsupported provider {name!r} for harness {harness!r}") from None
    return Provider(name, url, variable)


def codex_settings(provider: Provider) -> list[str]:
    """Extra Codex settings a provider needs. Only OpenRouter needs any."""
    if provider.name != "openrouter":
        return []
    return [
        'model_provider="openrouter"',
        'model_providers.openrouter.name="OpenRouter"',
        f'model_providers.openrouter.base_url="{provider.base_url}"',
        'model_providers.openrouter.env_key="OPENAI_API_KEY"',
        'model_providers.openrouter.wire_api="responses"',
        "model_providers.openrouter.request_max_retries=0",
        "model_providers.openrouter.stream_max_retries=0",
    ]

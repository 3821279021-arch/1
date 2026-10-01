"""Provider boundary shared by JSON and streaming routes.

Transport methods stay on the router for backwards-compatible injection in tests;
business code only sees adapters and the normalized LLMResult.
"""
from __future__ import annotations

from typing import Protocol, Any, AsyncIterator
from .tokens import estimate_tokens


def action_schema(action: str, options: list[int]) -> dict[str, Any] | None:
    target = {"anyOf": [{"type": "integer", "enum": options}, {"type": "null"}]}
    if not options:
        target = {"type": "null"}
    if action in {"vote", "wolf_kill", "seer_inspect"}:
        properties = {"target": target}
    elif action == "witch":
        properties = {"save": {"type": "boolean"}, "poison_target": target}
    else:
        return None
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


class ProviderAdapter(Protocol):
    async def generate(self, system: str, user: str) -> Any: ...
    def stream(self, system: str, user: str) -> AsyncIterator[str]: ...
    def estimate_tokens(self, model: str, text: str) -> int: ...
    def normalize_usage(self, body: dict[str, Any]) -> None: ...


class HTTPProviderAdapter:
    def __init__(self, provider: str, router):
        self.provider, self.router = provider, router

    async def generate(self, system: str, user: str):
        return await getattr(self.router, f"_{self.provider}")(system, user)

    def stream(self, system: str, user: str):
        return self.router._native_speech(self.provider, system, user)

    def estimate_tokens(self, model: str, text: str) -> int:
        return estimate_tokens(self.provider, model, text)

    def normalize_usage(self, body: dict[str, Any]) -> None:
        self.router._capture_usage(body, self.provider)

"""Provider boundary shared by JSON and streaming routes.

Transport methods stay on the router for backwards-compatible injection in tests;
business code only sees adapters and the normalized LLMResult.
"""
from __future__ import annotations

from typing import Protocol, Any, AsyncIterator
from .tokens import estimate_tokens
from urllib.parse import quote

from .credentials import Credential, model_headers, validate_model_options, check_public_endpoint, pin_request


class SecretStreamFilter:
    """Prevent an upstream from echoing the header key across SSE boundaries."""
    def __init__(self, secret: str):
        self.secret, self.pending = secret, ""

    def feed(self, chunk: str) -> str:
        self.pending = (self.pending + chunk).replace(self.secret, "••••••")
        keep = 0
        for length in range(min(len(self.secret) - 1, len(self.pending)), 0, -1):
            if self.pending.endswith(self.secret[:length]):
                keep = length
                break
        ready = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return ready

    def finish(self) -> str:
        ready, self.pending = self.pending.replace(self.secret, "••••••"), ""
        return ready


def redact_secret(value, secret: str):
    if isinstance(value, str):
        return value.replace(secret, "••••••")
    if isinstance(value, dict):
        return {redact_secret(key, secret): redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    return value


def action_schema(action: str, options: list[int]) -> dict[str, Any] | None:
    target = {"anyOf": [{"type": "integer", "enum": options}, {"type": "null"}]}
    if not options:
        target = {"type": "null"}
    if action in {"vote", "wolf_kill", "seer_inspect", "guard_protect", "hunter_shoot", "knight_duel", "duel", "wolf_king_shoot", "wolf_beauty_charm", "self_destruct"}:
        properties = {"target": target}
    elif action == "wolf_discuss":
        properties = {"text": {"type": "string"}}
    elif action == "optional_skill":
        properties = {"action": {"anyOf": [{"type": "string", "enum": ["knight_duel", "duel", "self_destruct"]}, {"type": "null"}]}, "target": target}
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
        return await getattr(self.router, f"_{self.provider.replace('-', '_')}")(system, user)

    def stream(self, system: str, user: str):
        return self.router._native_speech(self.provider, system, user)

    def estimate_tokens(self, model: str, text: str) -> int:
        return estimate_tokens(self.provider, model, text)

    def normalize_usage(self, body: dict[str, Any]) -> None:
        self.router._capture_usage(body, self.provider)


def provider_request(credential: Credential, model: str, system: str, user: str,
                     *, stream: bool = False, schema=None, max_tokens: int = 500,
                     parameters: dict | None = None):
    """Construct each provider's native API request without serializing credentials."""
    provider, base = credential.provider, credential.base_url
    parameters = validate_model_options(parameters)
    max_tokens = parameters.get("max_output_tokens", max_tokens)
    headers = {**model_headers(credential), "Content-Type": "application/json"}
    if provider == "openai":
        url = base + "/responses"
        payload = {"model": model, "instructions": system, "input": user, "max_output_tokens": max_tokens}
        if schema:
            payload["text"] = {"format": {"type": "json_schema", "name": "game_action", "strict": True, "schema": schema}}
    elif provider == "anthropic":
        url = base + "/messages"
        payload = {"model": model, "system": system, "messages": [{"role": "user", "content": user}], "max_tokens": max_tokens}
        if schema:
            payload["tools"] = [{"name": "game_action", "description": "Submit the legal game action", "input_schema": schema}]
            payload["tool_choice"] = {"type": "tool", "name": "game_action"}
    elif provider == "gemini":
        suffix = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        url = base + "/models/" + quote(model.removeprefix("models/"), safe="") + suffix
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": user}]}],
                   "generationConfig": {"maxOutputTokens": max_tokens}}
        if not stream:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            if schema:
                payload["generationConfig"]["responseJsonSchema"] = schema
    elif provider == "dashscope" and not base.endswith("/v1"):
        url = base + "/api/v1/services/aigc/text-generation/generation"
        payload = {"model": model, "input": {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                   "parameters": {"result_format": "message", "max_tokens": max_tokens, "enable_thinking": False}}
        if stream:
            headers["X-DashScope-SSE"] = "enable"
            payload["parameters"]["incremental_output"] = True
    else:
        url = base + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens}
        if not stream:
            payload["response_format"] = {"type": "json_object"}
        if provider == "dashscope":
            payload["enable_thinking"] = False
        if stream:
            payload["stream_options"] = {"include_usage": True}
    if stream and provider != "gemini" and not (provider == "dashscope" and not base.endswith("/v1")):
        payload["stream"] = True
    if "temperature" in parameters:
        temperature = parameters["temperature"]
        destination = payload["generationConfig"] if provider == "gemini" else payload.get("parameters", payload)
        destination["temperature"] = temperature
    effort = parameters.get("reasoning_effort")
    budget = parameters.get("thinking_budget")
    thinking = parameters.get("enable_thinking")
    if provider == "openai" and effort is not None:
        payload["reasoning"] = {"effort": effort}
    elif provider == "anthropic" and (budget is not None or effort is not None or thinking is not None):
        budget = budget if budget is not None else {"none": 0, "minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "xhigh": 16384}.get(effort, 2048)
        if thinking is False or budget == 0:
            payload["thinking"] = {"type": "disabled"}
        else:
            budget = max(1024, budget)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(payload["max_tokens"], budget + max_tokens)
            # Anthropic extended thinking supports automatic tool selection.
            if "tool_choice" in payload:
                payload["tool_choice"] = {"type": "auto"}
            payload.pop("temperature", None)
    elif provider == "gemini" and (budget is not None or effort is not None or thinking is not None):
        budget = budget if budget is not None else {"none": 0, "minimal": 128, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384}.get(effort, 1024)
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0 if thinking is False else budget, "includeThoughts": False}
    elif provider == "dashscope" and (thinking is not None or effort is not None):
        payload.get("parameters", payload)["enable_thinking"] = thinking if thinking is not None else effort != "none"
        if budget is not None:
            payload.get("parameters", payload)["thinking_budget"] = budget
    elif provider == "openai-compatible" and effort is not None:
        payload["reasoning_effort"] = effort
    return url, headers, payload


async def connection_probe(credential, model, client):
    url, headers, payload = provider_request(credential, model, "Reply with a JSON object.", 'Return {"ok":true}.', max_tokens=32)
    address = await check_public_endpoint(credential.base_url)
    url, headers, extensions = pin_request(url, headers, address)
    response = await client.post(url, headers=headers, json=payload, follow_redirects=False, extensions=extensions)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("error") or body.get("code"):
        raise ValueError("Provider rejected test")
    # A 200 error page or empty response must not count as verification.
    if not any(key in body for key in ("output", "content", "candidates", "choices")):
        raise ValueError("Invalid provider test response")

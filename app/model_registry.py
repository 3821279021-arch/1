"""A credential-free inventory of precise, independently selectable model IDs."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini", "anthropic": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-flash", "dashscope": "qwen-plus",
}
KEY_NAMES = {provider: f"{provider.upper()}_API_KEY" for provider in DEFAULT_MODELS}


class ModelAllocationError(ValueError):
    pass


@dataclass
class ModelEntry:
    provider: str
    model: str
    configured: bool
    enabled: bool = True
    healthy: bool = True
    capabilities: list[str] = field(default_factory=lambda: ["chat", "json", "stream"])
    unavailable_until: float = 0.0
    failure_reason: str | None = None
    public_healthy: bool | None = None
    public_unavailable_until: float = 0.0
    public_failure_reason: str | None = None

    def __post_init__(self):
        if self.public_healthy is None:
            self.public_healthy = self.healthy

    def refresh_health(self) -> None:
        if self.unavailable_until and time.monotonic() >= self.unavailable_until:
            self.healthy = True
            self.unavailable_until = 0

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def public(self) -> dict[str, Any]:
        # Public health changes only on public speech/voting or an explicit probe.
        # Skill and private-channel requests must not reveal their actor by timing.
        if self.public_unavailable_until and time.monotonic() >= self.public_unavailable_until:
            self.public_healthy = True
            self.public_unavailable_until = 0
        status = "configured" if self.configured else "unconfigured"
        if not self.enabled:
            status = "disabled"
        elif self.configured and not self.public_healthy:
            status = "temporarily_unavailable"
        return {"key": self.key, "provider": self.provider, "model": self.model,
                "configured": self.configured, "enabled": self.enabled, "healthy": self.public_healthy,
                "capabilities": list(self.capabilities), "status": status,
                "failure_reason": self.public_failure_reason}


class ModelRegistry:
    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self.defaults = {p: os.getenv(f"{p.upper()}_MODEL") or m for p, m in DEFAULT_MODELS.items()}
        self._entries: dict[str, ModelEntry] = {}
        self.cooldown = max(0, float(os.getenv("LLM_MODEL_COOLDOWN", "20")))
        if entries is None:
            for provider, default in self.defaults.items():
                models = [default]
                models.extend(x.strip() for x in os.getenv(f"{provider.upper()}_MODELS", "").split(",") if x.strip())
                for model in models:
                    self.register(provider, model)
            explicit = os.getenv("AI_MODEL_REGISTRY", "").strip()
            if explicit:
                decoded = json.loads(explicit)
                if not isinstance(decoded, list):
                    raise ValueError("AI_MODEL_REGISTRY must be a JSON array")
                entries = [{**entry, "configured": None} for entry in decoded]
        for entry in entries or []:
            self.register(entry["provider"], entry["model"], configured=entry.get("configured"),
                          enabled=entry.get("enabled", True), healthy=entry.get("healthy", True),
                          capabilities=entry.get("capabilities"))

    def register(self, provider: str, model: str, *, configured: bool | None = None,
                 enabled: bool = True, healthy: bool = True, capabilities: list[str] | None = None) -> ModelEntry:
        provider = provider.lower()
        if provider not in DEFAULT_MODELS or not isinstance(model, str) or not model or ":" in model:
            raise ValueError("Unknown provider or invalid actual model ID")
        # configured may be overridden in isolated tests; production registration still checks the environment.
        entry = ModelEntry(provider, model, bool(os.getenv(KEY_NAMES[provider])) if configured is None else configured,
                           enabled, healthy, list(capabilities) if capabilities is not None else ["chat", "json", "stream"])
        self._entries[entry.key] = entry
        return entry

    def resolve(self, value: str | None) -> str:
        value = value or "mock"
        if value == "mock" or value.startswith("mock:"):
            return "mock:rule-based-mock"
        if value in self.defaults:
            return f"{value}:{self.defaults[value]}"
        return value

    def get(self, value: str) -> ModelEntry | None:
        return self._entries.get(self.resolve(value))

    def entries(self) -> list[dict[str, Any]]:
        return [entry.public() for entry in self._entries.values()]

    def available(self, capability: str = "chat") -> list[ModelEntry]:
        result = []
        for entry in self._entries.values():
            entry.refresh_health()
            if entry.configured and entry.enabled and entry.healthy and capability in entry.capabilities:
                result.append(entry)
        return self._diversify(result)

    @staticmethod
    def _diversify(entries: list[ModelEntry]) -> list[ModelEntry]:
        buckets: dict[str, list[ModelEntry]] = {}
        for entry in entries:
            buckets.setdefault(entry.provider, []).append(entry)
        result = []
        while any(buckets.values()):
            for bucket in buckets.values():
                if bucket:
                    result.append(bucket.pop(0))
        return result

    def candidates(self, requested: str, capability: str) -> list[ModelEntry]:
        key = self.resolve(requested)
        if key.startswith("mock:"):
            return []
        primary = self.get(key)
        # An explicitly bound model gets one recovery opportunity even while marked unhealthy.
        pool = self.available(capability)
        if primary and primary.configured and primary.enabled and capability in primary.capabilities:
            return [primary, *(entry for entry in pool if entry.key != key)]
        return pool

    def failed(self, key: str, reason: str, *, disclose: bool = True) -> None:
        if entry := self.get(key):
            entry.healthy = False
            entry.failure_reason = reason
            entry.unavailable_until = time.monotonic() + self.cooldown
            if disclose:
                entry.public_healthy = False
                entry.public_failure_reason = reason
                entry.public_unavailable_until = entry.unavailable_until

    def succeeded(self, key: str, *, disclose: bool = True) -> None:
        if entry := self.get(key):
            entry.healthy = True
            entry.failure_reason = None
            entry.unavailable_until = 0
            if disclose:
                entry.public_healthy = True
                entry.public_failure_reason = None
                entry.public_unavailable_until = 0

    def allocate(self, presets: list[dict[str, Any]], unique: bool = True) -> dict[int, str]:
        """Reserve locked selections first, then spread auto seats over providers and models.

        Explicit Mock seats are a practice mode and do not count as real model identities.
        A non-locked provider preset is only a preference, never a silent locked duplicate.
        """
        assignments: dict[int, str] = {}
        occupied: set[str] = set()
        pool = [entry for entry in self.available("chat") if {"chat", "json", "stream"}.issubset(entry.capabilities)]
        pending = []
        for ordinal, preset in enumerate(presets, 1):
            seat = int(preset.get("seat_id", preset.get("id", ordinal)))
            selected = preset.get("model_key") or preset.get("provider") or "auto"
            if self.resolve(selected).startswith("mock:"):
                assignments[seat] = "mock:rule-based-mock"
                continue
            locked = bool(preset.get("model_locked", preset.get("locked", False)))
            if locked:
                key = self.resolve(selected)
                entry = next((entry for entry in pool if entry.key == key), None)
                if not entry:
                    raise ModelAllocationError(f"{seat} 号锁定模型未配置、被禁用、暂不可用或不支持 chat/json/stream")
                if unique and key in occupied:
                    raise ModelAllocationError("可用独立模型不足：锁定座位重复使用同一实际模型")
                assignments[seat] = key
                occupied.add(key)
            else:
                pending.append((seat, selected))
        for seat, preferred in pending:
            remaining = [entry for entry in pool if entry.key not in occupied]
            if not remaining and not unique:
                remaining = list(pool)
            if not remaining:
                raise ModelAllocationError(f"可用独立模型不足：需要 {len(presets)} 个 AI 座位，当前有 {len(pool)} 个支持 chat/json/stream 的健康真实模型。可切换到明确的兼容复用模式或使用 Mock 练习。")
            preferred_key = self.resolve(preferred)
            entry = next((entry for entry in remaining if entry.key == preferred_key), remaining[0])
            assignments[seat] = entry.key
            occupied.add(entry.key)
        return assignments

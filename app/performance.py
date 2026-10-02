"""AI capability profiles: tune soft model constraints without weakening game security.

Hard boundaries (information scope, legal actions, credentials, server-wide rate/cost
budgets) are intentionally outside this module. Profiles only control how much
context/output freedom an AI receives and how strongly the application shapes its
public expression.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PROFILE_KEYS = {"economy", "balanced", "unrestricted", "custom"}
HISTORY_MODES = {"compact", "hybrid", "full"}
COMPRESSION_MODES = {"aggressive", "adaptive", "minimal"}
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
TRANSPORT_MAX_SPEECH_CHARS = 4000
TRANSPORT_MAX_WOLF_CHAT_CHARS = 4000

PROFILES: dict[str, dict[str, Any]] = {
    "economy": {
        "label": "Economy",
        "prompt_token_limit": 3500,
        "history_mode": "compact",
        "recent_events_limit": 8,
        "private_notes_limit": 12,
        "wolf_chat_limit": 12,
        "memory_compression": "aggressive",
        "max_output_tokens": 512,
        "reasoning_effort": None,
        "thinking_budget": None,
        "enable_thinking": None,
        "speech_character_limit": 280,
        "wolf_discussion_character_limit": 240,
        "force_concise": True,
        "force_speech": True,
    },
    "balanced": {
        "label": "Balanced",
        "prompt_token_limit": 6000,
        "history_mode": "hybrid",
        "recent_events_limit": 12,
        "private_notes_limit": 20,
        "wolf_chat_limit": 20,
        "memory_compression": "adaptive",
        "max_output_tokens": 1200,
        "reasoning_effort": None,
        "thinking_budget": None,
        "enable_thinking": None,
        "speech_character_limit": 500,
        "wolf_discussion_character_limit": 500,
        "force_concise": False,
        "force_speech": True,
    },
    "unrestricted": {
        "label": "Unrestricted",
        "prompt_token_limit": 64000,
        "history_mode": "full",
        "recent_events_limit": None,
        "private_notes_limit": None,
        "wolf_chat_limit": None,
        "memory_compression": "minimal",
        "max_output_tokens": 16384,
        # Provider-specific reasoning knobs are intentionally left untouched.
        # Custom mode can force high/xhigh when the chosen model supports it.
        "reasoning_effort": None,
        "thinking_budget": None,
        "enable_thinking": None,
        "speech_character_limit": None,
        "wolf_discussion_character_limit": None,
        "force_concise": False,
        "force_speech": False,
    },
}

CUSTOM_DEFAULTS = deepcopy(PROFILES["balanced"])
CUSTOM_DEFAULTS.update(label="Custom")

CUSTOM_FIELDS = {
    "prompt_token_limit",
    "history_mode",
    "recent_events_limit",
    "private_notes_limit",
    "wolf_chat_limit",
    "memory_compression",
    "max_output_tokens",
    "reasoning_effort",
    "thinking_budget",
    "enable_thinking",
    "speech_character_limit",
    "wolf_discussion_character_limit",
    "force_concise",
    "force_speech",
}


def _optional_int(value: Any, name: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须介于 {minimum} 和 {maximum}，或设为 null")
    return value


def validate_performance(profile: str | None, custom: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    profile = profile or "balanced"
    if profile not in PROFILE_KEYS:
        raise ValueError("未知 AI Performance Profile")
    if custom is None:
        custom = {}
    if not isinstance(custom, dict) or set(custom) - CUSTOM_FIELDS:
        raise ValueError("AI 自定义性能参数包含未知字段")
    if profile != "custom":
        return profile, {}

    value = deepcopy(custom)
    if "prompt_token_limit" in value:
        raw = value["prompt_token_limit"]
        if type(raw) is not int or not 1800 <= raw <= 200000:
            raise ValueError("prompt_token_limit 必须介于 1800 和 200000")
    if "history_mode" in value and value["history_mode"] not in HISTORY_MODES:
        raise ValueError("history_mode 无效")
    for key in ("recent_events_limit", "private_notes_limit", "wolf_chat_limit"):
        if key in value:
            value[key] = _optional_int(value[key], key, 1, 5000)
    if "memory_compression" in value and value["memory_compression"] not in COMPRESSION_MODES:
        raise ValueError("memory_compression 无效")
    if "max_output_tokens" in value:
        value["max_output_tokens"] = _optional_int(value["max_output_tokens"], "max_output_tokens", 32, 65536)
    if "reasoning_effort" in value and value["reasoning_effort"] is not None:
        if value["reasoning_effort"] not in REASONING_EFFORTS:
            raise ValueError("reasoning_effort 无效")
    if "thinking_budget" in value:
        value["thinking_budget"] = _optional_int(value["thinking_budget"], "thinking_budget", 0, 131072)
    if (
        "enable_thinking" in value
        and value["enable_thinking"] is not None
        and type(value["enable_thinking"]) is not bool
    ):
        raise ValueError("enable_thinking 必须为布尔值或 null")
    for key, maximum in (
        ("speech_character_limit", TRANSPORT_MAX_SPEECH_CHARS),
        ("wolf_discussion_character_limit", TRANSPORT_MAX_WOLF_CHAT_CHARS),
    ):
        if key in value:
            value[key] = _optional_int(value[key], key, 1, maximum)
    for key in ("force_concise", "force_speech"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    return profile, value


def resolve_performance(profile: str | None, custom: dict[str, Any] | None = None) -> dict[str, Any]:
    profile, custom = validate_performance(profile, custom)
    base = deepcopy(CUSTOM_DEFAULTS if profile == "custom" else PROFILES[profile])
    if profile == "custom":
        base.update(custom)
    base["profile"] = profile
    # Full history is semantically stronger than a stale numeric slice.
    if base.get("history_mode") == "full":
        base["recent_events_limit"] = None
        base["private_notes_limit"] = None
        base["wolf_chat_limit"] = None
    return base


def profile_model_parameters(performance: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Translate a profile into provider parameters, with explicit seat overrides winning."""
    result = {}
    for key in ("max_output_tokens", "reasoning_effort", "thinking_budget", "enable_thinking"):
        value = performance.get(key)
        if value is not None:
            result[key] = value
    result.update(overrides or {})
    return result


def public_profiles() -> dict[str, dict[str, Any]]:
    return {key: deepcopy(value) for key, value in PROFILES.items()}

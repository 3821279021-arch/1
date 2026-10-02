"""Reject ambiguous configurations before starting paid model calls."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from app.credentials import normalize_base_url, validate_model_id, validate_model_options
from app.game import PERSONALITIES
from app.performance import validate_performance
from app.roles import validate_mode

PROVIDERS = {"mock", "openai", "anthropic", "gemini", "dashscope", "openai-compatible"}


def positive_int(value: Any, name: str, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [1, {maximum}]")
    return value


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Config must be an object")
    allowed = {
        "name",
        "games",
        "seed",
        "seat_policy",
        "role_policy",
        "paired_seeds",
        "ruleset",
        "agents",
        "lineups",
        "limits",
        "prices",
        "price_table_version",
        "ai_performance_profile",
        "ai_performance_custom",
    }
    if set(raw) - allowed:
        raise ValueError("Unknown config fields: " + ", ".join(sorted(set(raw) - allowed)))
    config = deepcopy(raw)
    config.setdefault("name", "arena")
    if not isinstance(config["name"], str) or not 1 <= len(config["name"]) <= 200:
        raise ValueError("name must be a nonempty string up to 200 characters")
    config["games"] = positive_int(config.get("games", 4), "games", 10000)
    config.setdefault("seed", 20261001)
    if type(config["seed"]) not in (int, str) or config["seed"] == "":
        raise ValueError("seed must be a nonempty string or integer")
    config.setdefault("seat_policy", "rotate")
    if config["seat_policy"] not in {"fixed", "rotate", "random_seeded"}:
        raise ValueError("Unknown seat_policy")
    config.setdefault("role_policy", "ruleset_seeded")
    if config["role_policy"] != "ruleset_seeded":
        raise ValueError("Only ruleset_seeded role policy is supported in V3.1")
    config.setdefault("paired_seeds", True)
    config.setdefault("ai_performance_profile", "balanced")
    config.setdefault("ai_performance_custom", {})
    profile, custom_performance = validate_performance(
        config["ai_performance_profile"], config["ai_performance_custom"]
    )
    config["ai_performance_profile"] = profile
    config["ai_performance_custom"] = custom_performance
    if type(config["paired_seeds"]) is not bool:
        raise ValueError("paired_seeds must be boolean")
    rules = config.get("ruleset", {})
    if not isinstance(rules, dict) or set(rules) - {"mode", "player_count", "roles", "board"}:
        raise ValueError("Invalid ruleset")
    mode = rules.get("mode") or {6: "quick6", 9: "standard9", 12: "standard12"}.get(
        rules.get("player_count", 6), "custom"
    )
    # Validate the actual engine's roster rather than maintain a parallel ruleset.
    mode, count, roles = validate_mode(mode, rules.get("player_count"), rules.get("roles"))
    if "player_count" in rules and rules["player_count"] != count:
        raise ValueError("ruleset mode and player_count disagree")
    config["ruleset"] = {"mode": mode, "player_count": count, "roles": roles}
    agents = config.get("agents")
    if not isinstance(agents, list) or not agents or any(not isinstance(a, dict) for a in agents):
        raise ValueError("At least one agent is required")
    for agent in agents:
        if set(agent) - {
            "id",
            "agent_id",
            "provider",
            "model",
            "parameters",
            "personality",
            "credential_env",
            "base_url",
        }:
            raise ValueError("Agents accept environment references, never inline credentials")
        agent["agent_id"] = agent.pop("id", agent.get("agent_id", ""))
        if not isinstance(agent["agent_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent["agent_id"]):
            raise ValueError("Agent id must contain 1–64 letters, digits, underscores or hyphens")
        if agent.get("provider") not in PROVIDERS:
            raise ValueError("Unsupported agent provider")
        agent["model"] = validate_model_id(agent.get("model"))
        agent["parameters"] = validate_model_options(agent.get("parameters", {}))
        if "seed" in agent["parameters"] and agent["provider"] in {"openai", "anthropic"}:
            raise ValueError("Sampling seed is unsupported by this provider endpoint")
        agent.setdefault("personality", "detective")
        if agent["personality"] not in PERSONALITIES:
            raise ValueError("Unknown personality")
        if agent["provider"] != "mock":
            ref = agent.get("credential_env")
            if ref is not None and (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref)):
                raise ValueError("Invalid credential_env")
            agent["base_url"] = normalize_base_url(agent["provider"], agent.get("base_url"))
    ids = [agent["agent_id"] for agent in agents]
    if len(set(ids)) != len(ids):
        raise ValueError("Agent ids must be unique")
    config.setdefault("lineups", [{"id": "default", "agents": ids}])
    lineups = config["lineups"]
    if not isinstance(lineups, list) or not lineups:
        raise ValueError("At least one lineup is required")
    for lineup in lineups:
        if not isinstance(lineup, dict) or set(lineup) != {"id", "agents"}:
            raise ValueError("A lineup contains id and agents")
        if not isinstance(lineup["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", lineup["id"]):
            raise ValueError("Invalid lineup id")
        members = lineup["agents"]
        if (
            not isinstance(members, list)
            or not members
            or len(members) > count
            or any(member not in ids for member in members)
        ):
            raise ValueError("Invalid lineup agents")
    if len({lineup["id"] for lineup in lineups}) != len(lineups):
        raise ValueError("Lineup ids must be unique")
    limits = config.setdefault("limits", {})
    if not isinstance(limits, dict) or set(limits) - {
        "max_concurrency",
        "max_retries",
        "max_total_tokens",
        "max_estimated_cost",
        "game_timeout_seconds",
        "max_steps",
        "fail_fast",
    }:
        raise ValueError("Invalid limits")
    defaults = {
        "max_concurrency": 1,
        "max_retries": 1,
        "max_total_tokens": 1000000,
        "game_timeout_seconds": 600,
        "max_steps": 2000,
        "fail_fast": False,
    }
    for key, value in defaults.items():
        limits.setdefault(key, value)
    if limits["max_concurrency"] != 1:
        raise ValueError("V3.1 runs serially; max_concurrency must be 1")
    if type(limits["max_retries"]) is not int or not 0 <= limits["max_retries"] <= 3:
        raise ValueError("max_retries must be in [0, 3]")
    for key in ("max_total_tokens", "game_timeout_seconds", "max_steps"):
        positive_int(limits[key], key)
    if type(limits["fail_fast"]) is not bool:
        raise ValueError("fail_fast must be boolean")
    prices = config.setdefault("prices", {})
    if not isinstance(prices, dict):
        raise ValueError("prices must be an object")
    for key, price in prices.items():
        if (
            not isinstance(price, dict)
            or set(price) != {"input", "output"}
            or any(type(p) not in (int, float) or not math.isfinite(p) or p < 0 for p in price.values())
        ):
            raise ValueError(f"Invalid price for {key}")
    config.setdefault("price_table_version", "unpriced" if not prices else "custom-v1")
    cap = limits.get("max_estimated_cost")
    if cap is not None:
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
            raise ValueError("max_estimated_cost must be positive and finite")
        if any(a["provider"] != "mock" and f"{a['provider']}:{a['model']}" not in prices for a in agents):
            raise ValueError("A cost cap requires prices for every paid model")
    return config

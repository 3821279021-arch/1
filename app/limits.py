"""Server-side request throttles and durable, conservative AI budget accounting.

Token reservations are committed before the HTTP request. Retries each require a
new ticket. A missing usage report, process crash, or cancellation consumes the
reservation rather than allowing a caller to bypass its game or daily budget.
Only identifiers and accounting totals are stored; prompts and credentials never
belong in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


class RateLimitError(Exception):
    status_code = 429
    status = 429

    def __init__(self, reason: str, message: str | None = None, retry_after: int = 60):
        self.reason = reason
        self.message = message or "请求过于频繁，请稍后再试。"
        self.retry_after = max(1, int(retry_after))
        super().__init__(self.message)


class BudgetExceeded(RateLimitError):
    def __init__(self, reason: str, message: str | None = None, retry_after: int = 60):
        super().__init__(reason, message or "AI 额度已达到上限，已切换为 Mock。", retry_after)


DEFAULTS = {
    "session_per_minute": 20,
    "room_per_hour": 10,
    "pet_per_minute": 12,
    "room_ai_per_minute": 180,
    "join_per_minute": 30,
    "chat_per_minute": 60,
    "action_per_minute": 90,
    "reconnect_per_minute": 30,
    "model_test_per_minute": 6,
    "recovery_per_minute": 10,
    "game_max_requests": 300,
    "game_token_budget": 250_000,
    "daily_token_budget": 2_000_000,
    "game_cost_budget": 0.0,
    "daily_cost_budget": 0.0,
    "max_active_rooms": 20,
    "input_price_per_million": 0.0,
    "output_price_per_million": 0.0,
}
ENV_NAMES = {
    "session_per_minute": "RATE_SESSION_PER_MINUTE",
    "room_per_hour": "RATE_ROOM_PER_HOUR",
    "pet_per_minute": "RATE_PET_PER_MINUTE",
    "room_ai_per_minute": "RATE_ROOM_AI_PER_MINUTE",
    "join_per_minute": "RATE_JOIN_PER_MINUTE",
    "chat_per_minute": "RATE_CHAT_PER_MINUTE",
    "action_per_minute": "RATE_ACTION_PER_MINUTE",
    "reconnect_per_minute": "RATE_RECONNECT_PER_MINUTE",
    "model_test_per_minute": "RATE_MODEL_TEST_PER_MINUTE",
    "recovery_per_minute": "RATE_RECOVERY_PER_MINUTE",
    "game_max_requests": "AI_GAME_MAX_REQUESTS",
    "game_token_budget": "AI_GAME_TOKEN_BUDGET",
    "daily_token_budget": "AI_DAILY_TOKEN_BUDGET",
    "game_cost_budget": "AI_GAME_COST_BUDGET",
    "daily_cost_budget": "AI_DAILY_COST_BUDGET",
    "max_active_rooms": "MAX_ACTIVE_ROOMS",
    "input_price_per_million": "AI_INPUT_PRICE_PER_MILLION",
    "output_price_per_million": "AI_OUTPUT_PRICE_PER_MILLION",
}


def _empty_usage() -> dict[str, Any]:
    return {
        "requests": 0,
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0.0,
        "reserved_tokens": 0,
        "reserved_cost": 0.0,
        "last_blocked": None,
    }


class Limits:
    """Shared across all rooms in the required single-process server.

    ``load`` returns a saved JSON-compatible dictionary; ``persist`` accepts the
    entire accounting state. Store callbacks must commit synchronously. A failed
    accounting store disables further real model calls while Mock remains usable.
    A zero rate or active-room limit disables that throttle. A zero request/token
    budget blocks real calls; a zero *cost* budget leaves cost limits disabled.
    """

    def __init__(
        self,
        load: Callable[[], dict[str, Any] | None] | None = None,
        persist: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.clock = clock
        self.persist = persist
        self.lock = threading.RLock()
        self.config = dict(DEFAULTS)
        for name, env_name in ENV_NAMES.items():
            raw = os.getenv(env_name)
            if raw is not None:
                value = float(raw) if isinstance(DEFAULTS[name], float) else int(raw)
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{env_name} must be a finite nonnegative number")
                self.config[name] = value
        self.config.update(config or {})
        for name, value in self.config.items():
            if name not in DEFAULTS or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid limit setting: {name}")
        try:
            self.prices = json.loads(os.getenv("AI_MODEL_PRICES", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("AI_MODEL_PRICES must be a JSON object") from exc
        if not isinstance(self.prices, dict):
            raise ValueError("AI_MODEL_PRICES must be a JSON object")
        for price in self.prices.values():
            if not isinstance(price, dict):
                raise ValueError("Each AI_MODEL_PRICES entry must contain input/output rates")
            for direction in ("input", "output"):
                value = float(price.get(direction, 0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Model prices must be finite nonnegative numbers")
        self._storage_ok = True
        self._state: dict[str, Any] = {"version": 1, "games": {}, "days": {}, "windows": {}, "pending": {}}
        if load:
            try:
                saved = load()
                if saved:
                    if not isinstance(saved, dict) or saved.get("version") != 1:
                        raise ValueError("Unsupported AI accounting state")
                    for key in ("games", "days", "windows", "pending"):
                        if not isinstance(saved.get(key), dict):
                            raise ValueError("Invalid AI accounting state")
                    self._state = json.loads(json.dumps(saved))
            except Exception:
                # A corrupt/unreadable store must not reset previously spent money.
                self._storage_ok = False
        if self._state["pending"]:
            for ticket in list(self._state["pending"].values()):
                self._settle(ticket, ticket["input_tokens"], ticket["output_tokens"])
            self._state["pending"].clear()
            self._save()

    def _day(self, timestamp: float | None = None) -> str:
        return datetime.fromtimestamp(self.clock() if timestamp is None else timestamp, timezone.utc).strftime(
            "%Y-%m-%d"
        )

    def _save(self) -> None:
        if self.persist and self._storage_ok:
            try:
                self.persist(self.export_state())
            except Exception:
                self._storage_ok = False

    def export_state(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._state))

    def check(self, kind: str, key: str) -> None:
        """Throttle API entry points before any third-party work is scheduled."""
        specs = {
            "session": ("session_per_minute", 60),
            "room": ("room_per_hour", 3600),
            "pet": ("pet_per_minute", 60),
            "room_ai": ("room_ai_per_minute", 60),
            **{
                name: (name + "_per_minute", 60)
                for name in ("join", "chat", "action", "reconnect", "model_test", "recovery")
            },
        }
        if kind not in specs:
            raise ValueError("Unknown rate-limit category")
        name, period = specs[kind]
        maximum = int(self.config[name])
        if not maximum:
            return
        with self.lock:
            now = self.clock()
            # Hash caller-provided keys so tokens/IPs cannot appear in the store.
            window_key = kind + ":" + hashlib.sha256(str(key).encode()).hexdigest()
            entry = self._state["windows"].setdefault(window_key, {"period": period, "timestamps": []})
            # Keep future timestamps if the wall clock moves backwards; an NTP
            # adjustment must not grant an extra burst of paid requests.
            stamps = deque(t for t in entry["timestamps"] if now - period < t)
            if len(stamps) >= maximum:
                raise RateLimitError(kind + "_rate", retry_after=math.ceil(stamps[0] + period - now))
            stamps.append(now)
            entry["timestamps"] = list(stamps)
            # Expired users must not accumulate indefinitely on a public server.
            for old_key, old in list(self._state["windows"].items()):
                if old_key != window_key and (not old["timestamps"] or old["timestamps"][-1] + old["period"] <= now):
                    self._state["windows"].pop(old_key, None)
            self._save()

    def check_active_rooms(self, count: int) -> None:
        maximum = int(self.config["max_active_rooms"])
        if maximum and count >= maximum:
            raise RateLimitError("active_rooms", "同时进行的房间已达到上限，请稍后再开局。")

    @staticmethod
    def _game_key(room_id: str, game_id: str) -> str:
        return str(room_id) + ":" + str(game_id)

    def _cost(self, model_key: str, input_tokens: int, output_tokens: int) -> float:
        in_price, out_price = self._price(model_key)
        return (input_tokens * in_price + output_tokens * out_price) / 1_000_000

    def _price(self, model_key: str) -> tuple[float, float]:
        price = self.prices.get(model_key, {})
        if not isinstance(price, dict):
            price = {}
        in_price = float(price.get("input", self.config["input_price_per_million"]))
        out_price = float(price.get("output", self.config["output_price_per_million"]))
        if not all(math.isfinite(p) and p >= 0 for p in (in_price, out_price)):
            raise ValueError("Model prices must be finite nonnegative numbers")
        return in_price, out_price

    def _blocked(self, game: dict[str, Any], day: dict[str, Any], reason: str) -> None:
        game["last_blocked"] = reason
        if reason.startswith("daily"):
            day["last_blocked"] = reason
        self._save()
        raise BudgetExceeded(reason)

    def before_call(self, meta: dict[str, Any]) -> str:
        """Reserve one real HTTP attempt and its maximum estimated token usage."""
        with self.lock:
            room_id = str(meta.get("room_id") or "unscoped")
            game_id = str(meta.get("game_id") or "unscoped")
            game_key = self._game_key(room_id, game_id)
            day_key = self._day()
            game = self._state["games"].setdefault(game_key, _empty_usage())
            day = self._state["days"].setdefault(day_key, _empty_usage())
            if not self._storage_ok:
                self._blocked(game, day, "budget_storage_unavailable")
            input_tokens = max(0, int(meta.get("estimated_input_tokens", 1000)))
            output_tokens = max(0, int(meta.get("max_output_tokens", 600)))
            tokens = input_tokens + output_tokens
            if tokens == 0:
                # Zero estimates would allow an arbitrary number of unmetered tokens.
                input_tokens, output_tokens, tokens = 1000, 600, 1600
            model_key = str(meta.get("model_key") or f"{meta.get('provider', '')}:{meta.get('model', '')}")
            cost = self._cost(model_key, input_tokens, output_tokens)
            if game["requests"] >= self.config["game_max_requests"]:
                self._blocked(game, day, "game_request_budget")
            for bucket, prefix, setting in ((game, "game", "game_token_budget"), (day, "daily", "daily_token_budget")):
                if bucket["tokens"] + bucket["reserved_tokens"] + tokens > self.config[setting]:
                    self._blocked(game, day, prefix + "_token_budget")
                budget = self.config[prefix + "_cost_budget"]
                if budget > 0 and bucket["estimated_cost"] + bucket["reserved_cost"] + cost > budget + 1e-12:
                    self._blocked(game, day, prefix + "_cost_budget")
            try:
                self.check("room_ai", room_id)
            except RateLimitError:
                self._blocked(game, day, "room_ai_rate")
            ticket_id = uuid.uuid4().hex
            ticket = {
                "game_key": game_key,
                "day_key": day_key,
                "room_id": room_id,
                "game_id": game_id,
                "agent_id": str(meta.get("agent_id") or ""),
                "category": str(meta.get("category") or "unknown"),
                "provider": str(meta.get("provider") or ""),
                "model_key": model_key,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tokens": tokens,
                "cost": cost,
                "input_price": self._price(model_key)[0],
                "output_price": self._price(model_key)[1],
            }
            for bucket in (game, day):
                bucket["requests"] += 1
                bucket["reserved_tokens"] += tokens
                bucket["reserved_cost"] += cost
            self._state["pending"][ticket_id] = ticket
            game["last_blocked"] = None
            day["last_blocked"] = None
            self._save()
            if not self._storage_ok:
                # Accounting failed before HTTP, so cancel its reservation locally.
                self._state["pending"].pop(ticket_id, None)
                for bucket in (game, day):
                    bucket["requests"] -= 1
                    bucket["reserved_tokens"] -= tokens
                    bucket["reserved_cost"] -= cost
                self._blocked(game, day, "budget_storage_unavailable")
            return ticket_id

    def _settle(self, ticket: dict[str, Any], input_tokens: int, output_tokens: int) -> None:
        cost = (
            (input_tokens * ticket["input_price"] + output_tokens * ticket["output_price"]) / 1_000_000
            if "input_price" in ticket
            else self._cost(ticket["model_key"], input_tokens, output_tokens)
        )
        game = self._state["games"].setdefault(ticket["game_key"], _empty_usage())
        day = self._state["days"].setdefault(ticket["day_key"], _empty_usage())
        for bucket in (game, day):
            bucket["reserved_tokens"] = max(0, bucket["reserved_tokens"] - ticket["tokens"])
            bucket["reserved_cost"] = max(0.0, bucket["reserved_cost"] - ticket["cost"])
            bucket["input_tokens"] += input_tokens
            bucket["output_tokens"] += output_tokens
            bucket["tokens"] += input_tokens + output_tokens
            bucket["estimated_cost"] += cost

    def after_call(self, ticket_id: str, record: dict[str, Any] | None = None) -> None:
        """Settle exactly once; unknown/duplicate tickets never refund usage."""
        with self.lock:
            ticket = self._state["pending"].pop(ticket_id, None)
            if ticket is None:
                return
            try:
                record = record if isinstance(record, dict) else {}
                usage = record.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_tokens = record.get("input_tokens", usage.get("input_tokens", usage.get("prompt_tokens")))
                output_tokens = record.get("output_tokens", usage.get("output_tokens", usage.get("completion_tokens")))
                total = record.get("total_tokens", usage.get("total_tokens"))
                # Google's camelCase counters are also accepted by the shared router.
                input_tokens = input_tokens if input_tokens is not None else usage.get("promptTokenCount")
                output_tokens = output_tokens if output_tokens is not None else usage.get("candidatesTokenCount")
                total = total if total is not None else usage.get("totalTokenCount")
                if input_tokens is None and output_tokens is None and total is None:
                    input_tokens, output_tokens = ticket["input_tokens"], ticket["output_tokens"]
                elif total is not None:
                    total = max(0, int(total))
                    if input_tokens is None and output_tokens is None:
                        # A total alone is charged at the more expensive direction.
                        in_price, out_price = (
                            (ticket["input_price"], ticket["output_price"])
                            if "input_price" in ticket
                            else self._price(ticket["model_key"])
                        )
                        input_tokens, output_tokens = (total, 0) if in_price >= out_price else (0, total)
                    elif input_tokens is None:
                        input_tokens = max(0, total - int(output_tokens))
                    elif output_tokens is None:
                        output_tokens = max(0, total - int(input_tokens))
                    elif int(input_tokens) + int(output_tokens) < total:
                        # Account for reasoning/cached tokens in provider total.
                        output_tokens = int(output_tokens) + total - int(input_tokens) - int(output_tokens)
                else:
                    input_tokens = ticket["input_tokens"] if input_tokens is None else input_tokens
                    output_tokens = ticket["output_tokens"] if output_tokens is None else output_tokens
                self._settle(ticket, max(0, int(input_tokens)), max(0, int(output_tokens)))
            except (TypeError, ValueError, OverflowError):
                self._settle(ticket, ticket["input_tokens"], ticket["output_tokens"])
            self._save()

    def snapshot(self, room_id: str, game_id: str) -> dict[str, Any]:
        """Safe host-only budget display, never identities, tickets, or prompts."""
        with self.lock:
            game = dict(self._state["games"].get(self._game_key(room_id, game_id), _empty_usage()))
            day = dict(self._state["days"].get(self._day(), _empty_usage()))
            reason = None
            if not self._storage_ok:
                reason = "budget_storage_unavailable"
            elif game["requests"] >= self.config["game_max_requests"]:
                reason = "game_request_budget"
            elif game["tokens"] + game["reserved_tokens"] >= self.config["game_token_budget"]:
                reason = "game_token_budget"
            elif day["tokens"] + day["reserved_tokens"] >= self.config["daily_token_budget"]:
                reason = "daily_token_budget"
            elif (
                self.config["game_cost_budget"] > 0
                and game["estimated_cost"] + game["reserved_cost"] >= self.config["game_cost_budget"]
            ):
                reason = "game_cost_budget"
            elif (
                self.config["daily_cost_budget"] > 0
                and day["estimated_cost"] + day["reserved_cost"] >= self.config["daily_cost_budget"]
            ):
                reason = "daily_cost_budget"
            else:
                rate_key = "room_ai:" + hashlib.sha256(str(room_id).encode()).hexdigest()
                rate = self._state["windows"].get(rate_key, {"timestamps": []})
                active = sum(timestamp > self.clock() - 60 for timestamp in rate["timestamps"])
                if self.config["room_ai_per_minute"] and active >= self.config["room_ai_per_minute"]:
                    reason = "room_ai_rate"
            return {
                "game": game,
                "daily": day,
                "daily_date": self._day(),
                "limits": {
                    k: self.config[k]
                    for k in (
                        "game_max_requests",
                        "game_token_budget",
                        "game_cost_budget",
                        "daily_token_budget",
                        "daily_cost_budget",
                        "max_active_rooms",
                    )
                },
                "real_calls_allowed": reason is None,
                "fallback_reason": reason or game["last_blocked"] or day["last_blocked"],
            }


# The router and room manager can use either descriptive name.
UsageGuard = Limits
